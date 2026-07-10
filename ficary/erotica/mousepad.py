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

The author-only filter still lets through the author's *own*
non-story posts. Live data says length alone can't sort those out —
observed threads have 138-word in-character interludes and 452-word
"thanks for reading" confessions — so the adapter labels rather than
deletes: a leading bold / ``# `` header line is lifted into the
chapter title (audible in the TOC, skippable by ear), and a post is
dropped only on two corroborating signals — it *opens* by quoting a
non-author post AND has fewer than :data:`SKIP_MAX_WORDS` of its own
words once quotes are stripped. That combination is a reply to a
commenter, nothing else. Dropped posts are logged and recorded in
``story.metadata["skipped_posts"]`` so nothing vanishes silently.
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

# Skip gate: an author post that opens by quoting someone else AND has
# fewer than this many of its own words is a reply to a commenter.
# Observed floor for story-shaped author posts is ~138 words; observed
# comment-replies run ~10-50.
SKIP_MAX_WORDS = 60

# A leading bold/# line longer than this is a bolded first sentence,
# not a chapter header — leave it in the body.
TITLE_MAX_CHARS = 80

# Tapatalk renders phpBB quotes as literal BBCode even in HTML mode:
# ``[quote uid=10796411 name="PretentiousOne" post=1312855]...[/quote]``
# (attribute set varies; ``uid=0`` appears for guest-rendered quotes).
QUOTE_BLOCK_RE = re.compile(
    r"\[quote\b([^\]]*)\]((?:(?!\[/?quote\b).)*)\[/quote\]",
    re.I | re.DOTALL,
)
QUOTE_LEAD_RE = re.compile(
    r"^(?:\s|<br\s*/?>)*\[quote\b([^\]]*)\]", re.I,
)
QUOTE_NAME_RE = re.compile(r'name="([^"]*)"', re.I)
QUOTE_UID_RE = re.compile(r"uid=(\d+)", re.I)

LEAD_TITLE_BOLD_RE = re.compile(
    r"^\s*<(b|strong)>(?P<t>[^<]{1,%d})</\1>\s*(?:<br\s*/?>\s*)+" % TITLE_MAX_CHARS,
    re.I,
)
LEAD_TITLE_HASH_RE = re.compile(
    r"^\s*#{1,6}\s*(?P<t>[^<\n]{1,%d}?)\s*(?:<br\s*/?>\s*)+" % TITLE_MAX_CHARS,
)


def _render_quotes(html: str) -> str:
    """Convert BBCode quote blocks to attributed ``<blockquote>``
    markup so exports don't ship raw ``[quote]`` tags. Innermost-first
    replacement handles nested quotes."""
    def _one(m: re.Match) -> str:
        name_m = QUOTE_NAME_RE.search(m.group(1))
        attrib = (
            f"<p><em>{name_m.group(1)} wrote:</em></p>" if name_m else ""
        )
        return f"<blockquote>{attrib}{m.group(2)}</blockquote>"

    while QUOTE_BLOCK_RE.search(html):
        html = QUOTE_BLOCK_RE.sub(_one, html)
    return html


def _strip_quotes(html: str) -> str:
    """Remove quote blocks entirely — used to count a post's *own*
    words for the skip gate."""
    while QUOTE_BLOCK_RE.search(html):
        html = QUOTE_BLOCK_RE.sub("", html)
    return html


def _is_comment_reply(html: str, author_id: str, author_name: str) -> bool:
    """Two-signal gate for "author replying to a commenter".

    Signal 1: the post *opens* with a quote of someone who isn't the
    author (matched by uid when present and non-zero, else by name —
    the attribute set varies between quotes). An author re-quoting
    their own story text never trips this.
    Signal 2: under :data:`SKIP_MAX_WORDS` words once every quote
    block is stripped.
    """
    lead = QUOTE_LEAD_RE.match(html)
    if not lead:
        return False
    attrs = lead.group(1)
    uid_m = QUOTE_UID_RE.search(attrs)
    if uid_m and uid_m.group(1) not in ("", "0"):
        if uid_m.group(1) == author_id:
            return False
    else:
        name_m = QUOTE_NAME_RE.search(attrs)
        if name_m and name_m.group(1).strip().lower() == author_name.strip().lower():
            return False
    own_text = BeautifulSoup(
        _strip_quotes(html), "lxml",
    ).get_text(" ", strip=True)
    return len(own_text.split()) < SKIP_MAX_WORDS


def _lift_title(html: str) -> tuple[str, str]:
    """Pull a leading header line out as the chapter title.

    Mousepad authors head their posts with a whole-line bold title
    (``<b>Author's Confession</b>``) or a markdown-style ``# Title``
    line. Lifting it lets the export's TOC read "Chapter 30. Author's
    Confession" — the label a listener needs to skip a note — and the
    line is removed from the body so it doesn't render twice. Returns
    ``("", html)`` untouched when no header is found.
    """
    for pattern in (LEAD_TITLE_BOLD_RE, LEAD_TITLE_HASH_RE):
        m = pattern.match(html)
        if m:
            # Odd spacing like ``# ## Title`` leaves hash residue in
            # the capture — scrub it so titles read clean.
            return m.group("t").strip().lstrip("#").strip(), html[m.end():]
    return "", html


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

    @classmethod
    def _prepare_chapters(
        cls, thread: dict, story_posts: list[dict],
    ) -> tuple[list[tuple[str, str]], list[dict]]:
        """Turn the author's posts into ``(title, html)`` chapters.

        Applies the comment-reply skip gate, lifts leading header
        lines into titles, and renders quote BBCode in what remains.
        Returns ``(chapters, skipped)`` where each skipped entry keeps
        enough of the post to identify it after the fact.
        """
        author_id = decode_value(thread.get("topic_author_id"))
        author_name = decode_value(thread.get("topic_author_name"))
        chapters: list[tuple[str, str]] = []
        skipped: list[dict] = []
        for p in story_posts:
            raw = decode_value(p.get("post_content"))
            if _is_comment_reply(raw, author_id, author_name):
                preview = BeautifulSoup(
                    _strip_quotes(raw), "lxml",
                ).get_text(" ", strip=True)[:80]
                logger.info(
                    "Mousepad: skipping author comment-reply post %s (%r)",
                    decode_value(p.get("post_id")), preview,
                )
                skipped.append({
                    "post_id": decode_value(p.get("post_id")),
                    "preview": preview,
                })
                continue
            title, body = _lift_title(raw)
            chapters.append((title, _render_quotes(body)))
        return chapters, skipped

    def get_chapter_count(self, url_or_id):
        topic_id = self.parse_story_id(url_or_id)
        thread, posts = self._fetch_thread(topic_id)
        chapters, _ = self._prepare_chapters(
            thread, self._author_posts(thread, posts),
        )
        return len(chapters)

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
        prepared, skipped = self._prepare_chapters(thread, story_posts)
        if not prepared:
            raise ValueError(
                f"Mousepad topic {topic_id} has no story posts by its "
                "author — nothing to download."
            )

        title = decode_value(thread.get("topic_title")) or f"Topic {topic_id}"
        author = decode_value(thread.get("topic_author_name")) or "Unknown"
        num_chapters = len(prepared)

        meta = {
            "title": title,
            "author": author,
            "author_url": "",
            "summary": self._summary_from_html(prepared[0][1]),
            "num_chapters": num_chapters,
            "chapter_titles": {
                str(n): t for n, (t, _) in enumerate(prepared, 1) if t
            },
            "extra": {
                "topic_id": topic_id,
                "forum": decode_value(thread.get("forum_name")),
                "updated": iso_datetime(story_posts[-1].get("post_time")),
                "total_posts": len(posts),
                "skipped_posts": skipped,
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

        for i, (ch_title, ch_html) in enumerate(prepared, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            ch = Chapter(number=i, title=ch_title, html=ch_html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch.title, False)

        return story
