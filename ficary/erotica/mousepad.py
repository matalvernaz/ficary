"""The Mousepad (tapatalk.com/groups/themousepad) — foot-fetish story forum.

Unlike every other erotica adapter, the source is a *forum*, not a
story archive: a story is a phpBB topic, and its chapters are the
posts the topic's author made in that topic. Reader comments are
interleaved between chapters in the same thread, so the download path
filters to posts whose ``post_author_id`` equals the thread's
``topic_author_id`` and drops everything else.

Transport is the Tapatalk mobiquo XML-RPC API (see
:mod:`ficary.erotica.tapatalk` for why the HTML site is unusable),
which means this scraper never touches :meth:`BaseScraper._fetch`.
It still uses the base class's delay machinery between thread-window
calls and its meta/chapter caches so cache_doctor and the library
tooling see the same shapes as every other site.

Known trade-off: an author's own conversational replies ("thanks,
more coming next week") pass the author-only filter and appear as
short chapters. Distinguishing them from a legitimately short chapter
needs judgement the adapter doesn't have; readers can skip them, and
re-downloads stay stable because chapter numbering follows the
author-post sequence.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper
from .tapatalk import (
    MOUSEPAD_GROUP,
    THREAD_WINDOW,
    decode_value,
    iso_datetime,
    mobiquo_call,
    topic_url,
)

logger = logging.getLogger(__name__)

MP_VIEWTOPIC_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/viewtopic\.php\?(?:[^#\s]*&)?t=(?P<id>\d+)",
    re.I,
)

# phpBB SEO permalinks: ``<slug>-t197281.html`` (``-s<offset>`` on
# paged views). Tapatalk renders these for every topic, so pasted
# links arrive in this shape at least as often as viewtopic.php.
MP_SLUG_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/[a-z0-9_-]+-t(?P<id>\d+)(?:-s\d+)?(?:\.html)?",
    re.I,
)

SUMMARY_MAX_CHARS = 300


class MousepadScraper(BaseScraper):
    """Scraper for The Mousepad story forums."""

    site_name = "mousepad"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the topic id (as a string of digits)."""
        text = str(url_or_id).strip()
        for pattern in (MP_VIEWTOPIC_URL_RE, MP_SLUG_URL_RE):
            m = pattern.search(text)
            if m:
                return m.group("id")
        if re.fullmatch(r"\d+", text):
            return text
        raise ValueError(
            f"Cannot parse Mousepad topic id from: {text!r}\n"
            "Expected e.g. https://www.tapatalk.com/groups/themousepad/"
            "viewtopic.php?t=197281 or a bare topic id."
        )

    @classmethod
    def cache_key_for_url(cls, url_or_id):
        return int(cls.parse_story_id(url_or_id))

    def _fetch_thread(self, topic_id: str) -> tuple[dict, list[dict]]:
        """Walk the thread's post windows and return
        ``(first_response, all_posts)``.

        Advances by however many posts each window actually returned
        (the server may cap below :data:`THREAD_WINDOW`); a window
        that returns nothing before ``total_post_num`` is reached ends
        the walk rather than looping forever.
        """
        first = mobiquo_call(
            "get_thread", topic_id, 0, THREAD_WINDOW - 1, True,
        )
        posts: list[dict] = list(first.get("posts") or [])
        total = int(first.get("total_post_num") or len(posts))
        while len(posts) < total:
            self._delay()
            window = mobiquo_call(
                "get_thread", topic_id,
                len(posts), len(posts) + THREAD_WINDOW - 1, True,
            )
            batch = window.get("posts") or []
            if not batch:
                logger.warning(
                    "Mousepad topic %s: server stopped at %d/%d posts",
                    topic_id, len(posts), total,
                )
                break
            posts.extend(batch)
        return first, posts

    @staticmethod
    def _author_posts(thread: dict, posts: list[dict]) -> list[dict]:
        """Cut the thread down to the story: posts by the topic author.

        Everything else in the thread is reader comments. Matching on
        the stable ``post_author_id`` (not the display name) survives
        username changes.
        """
        author_id = decode_value(thread.get("topic_author_id"))
        return [
            p for p in posts
            if decode_value(p.get("post_author_id")) == author_id
        ]

    @staticmethod
    def _summary_from_html(html: str) -> str:
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
        if len(text) <= SUMMARY_MAX_CHARS:
            return text
        return text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    def get_chapter_count(self, url_or_id):
        topic_id = self.parse_story_id(url_or_id)
        thread, posts = self._fetch_thread(topic_id)
        return len(self._author_posts(thread, posts))

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        topic_id = self.parse_story_id(url_or_id)
        story_id = int(topic_id)

        logger.info("Fetching Mousepad topic %s...", topic_id)
        thread, posts = self._fetch_thread(topic_id)
        story_posts = self._author_posts(thread, posts)
        if not story_posts:
            raise ValueError(
                f"Mousepad topic {topic_id} has no posts by its author — "
                "nothing to download."
            )

        title = decode_value(thread.get("topic_title")) or f"Topic {topic_id}"
        author = decode_value(thread.get("topic_author_name")) or "Unknown"
        first_html = decode_value(story_posts[0].get("post_content"))
        num_chapters = len(story_posts)

        meta = {
            "title": title,
            "author": author,
            "author_url": "",
            "summary": self._summary_from_html(first_html),
            "num_chapters": num_chapters,
            "chapter_titles": {},
            "extra": {
                "topic_id": topic_id,
                "forum": decode_value(thread.get("forum_name")),
                "updated": iso_datetime(story_posts[-1].get("post_time")),
                "total_posts": len(posts),
            },
        }
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=title,
            author=author,
            summary=meta["summary"],
            url=topic_url(topic_id),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story

        for i, post in enumerate(story_posts, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            ch = Chapter(
                number=i,
                title="",
                html=decode_value(post.get("post_content")),
            )
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch.title, False)

        return story
