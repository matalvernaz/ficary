"""Shared scraper for XenForo boards where a story is a thread.

The model (proven on Dark Wanderer, generalised in 2026-07 when
Chastity Mansion and TicklingForum were added): the thread starter's
posts are the chapters, in order; other members' replies are
comments and are dropped. Long threads paginate as
``<thread-url>page-N`` and every page is walked. Quoted blocks are
stripped from post bodies so a starter post that quotes a reader
doesn't drag the reply into the story.

Subclasses set:

* ``site_name`` — ficary's slug for the site.
* ``XF_BASE`` — board root, no trailing slash.
* ``XF_THREAD_PATH`` — thread path template under the base; boards
  without friendly URLs route through ``index.php?threads/...``.
* ``THREAD_URL_RE`` — regex with named groups ``slug`` and ``tid``
  accepting every pasted-URL shape the board emits.
* ``XF_TITLE_SUFFIX_RE`` — strips the board's name off ``<title>``
  (``og:title`` is tried first and is usually suffix-free).
"""

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)


class XenForoStoryScraper(BaseScraper):
    """Base class — see module docstring. Not registered itself."""

    XF_BASE = ""
    XF_THREAD_PATH = "threads/{ref}/"
    THREAD_URL_RE: re.Pattern = re.compile(r"$^")
    XF_TITLE_SUFFIX_RE: re.Pattern = re.compile(r"$^")

    @classmethod
    def parse_story_id(cls, url_or_id):
        """Return the numeric thread id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = cls.THREAD_URL_RE.search(text)
        if m:
            return int(m.group("tid"))
        raise ValueError(
            f"Cannot parse a {cls.site_name} thread id from: {text!r}\n"
            f"Expected e.g. {cls.XF_BASE}/"
            + cls.XF_THREAD_PATH.format(ref="my-story.12345")
        )

    @classmethod
    def _thread_url(cls, tid: int, slug: str = "") -> str:
        ref = f"{slug}.{tid}" if slug else str(tid)
        return f"{cls.XF_BASE}/" + cls.XF_THREAD_PATH.format(ref=ref)

    @classmethod
    def _slug_of(cls, url_or_id) -> str:
        m = cls.THREAD_URL_RE.search(str(url_or_id))
        return m.group("slug") if m else ""

    @classmethod
    def _parse_metadata(cls, soup, tid: int, slug: str) -> dict:
        title = ""
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = cls.XF_TITLE_SUFFIX_RE.sub(
                "", og_title["content"],
            ).strip()
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                raw = title_tag.get_text(" ", strip=True)
                title = cls.XF_TITLE_SUFFIX_RE.sub("", raw).strip()
        if not title:
            title = slug.replace("-", " ").title() or f"Thread {tid}"

        # The first message article's data-author is the same value the
        # chapter filter trusts, and it survives skins that hide member
        # links from guests (Chastity Mansion does).
        author = "Unknown Author"
        author_url = ""
        first_article = soup.find(
            "article", class_=re.compile(r"\bmessage\b"),
        )
        if first_article is not None:
            author = (
                (first_article.get("data-author") or "").strip() or author
            )
        starter_link = soup.find(
            "a", class_=re.compile(r"username"),
            href=re.compile(r"/members/"),
        )
        if starter_link:
            if author == "Unknown Author":
                author = starter_link.get_text(strip=True) or author
            href = starter_link.get("href", "")
            if href:
                author_url = urljoin(cls.XF_BASE + "/", href)

        summary = ""
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            summary = og_desc["content"].strip()

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": {"thread_id": tid, "slug": slug},
        }

    @staticmethod
    def _page_count(soup) -> int:
        """XenForo paginates with ``<nav class="pageNav">``; the last
        page-N link is the final page."""
        nav = soup.find("nav", class_="pageNav")
        if not nav:
            return 1
        max_page = 1
        for a in nav.find_all("a", href=True):
            m = re.search(r"page-(\d+)", a["href"])
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    @staticmethod
    def _thread_starter_username(soup) -> str:
        """Name to compare each post's author against so we can keep
        only the starter's posts (= the story chapters)."""
        # The thread starter is structurally the first post — the first
        # <article class="message"> — so its data-author is the OP.
        # Prefer that over a page-wide <a class="username"> scan: on some
        # XenForo skins the first such anchor is a sidebar / newest-poster
        # / breadcrumb link, not the OP, which would silently filter out
        # every real chapter and keep the wrong author's posts.
        first_article = soup.find(
            "article", class_=re.compile(r"\bmessage\b")
        )
        if first_article is not None:
            author_attr = (first_article.get("data-author") or "").strip()
            if author_attr:
                return author_attr
        first_post_user = soup.find(
            "a", href=re.compile(r"/members/"), class_=re.compile(r"username"),
        )
        return first_post_user.get_text(strip=True) if first_post_user else ""

    @staticmethod
    def _starter_posts(soup, starter: str, *, is_first_page: bool) -> list[str]:
        """Return ``[html, ...]`` for each post authored by ``starter``.

        When ``starter`` is empty (the skin hid the username) the
        first page falls back to "first post only" — on XenForo the
        thread starter is always the first post — and later pages
        include nothing rather than dragging in every reply.
        """
        articles = list(
            soup.find_all("article", class_=re.compile(r"\bmessage\b"))
        )
        htmls = []
        for idx, article in enumerate(articles):
            author_attr = (article.get("data-author") or "").strip()
            if starter:
                if author_attr and author_attr != starter:
                    continue
                if not author_attr:
                    # Unknown author on a known-starter thread — skip
                    # rather than guess.
                    continue
            else:
                if not (is_first_page and idx == 0):
                    continue
            body = article.find("div", class_=re.compile(r"bbWrapper"))
            if body is None:
                body = article.find("div", class_=re.compile(r"message-content"))
            if body is None:
                continue
            # Drop quoted blocks so we don't include replies-within-a-quote.
            for quote in body.select("blockquote, div.bbCodeBlock"):
                quote.decompose()
            htmls.append(body.decode_contents())
        return htmls

    def _walk_starter_posts(self, tid: int, slug: str):
        """Fetch every thread page and return
        ``(first_page_soup, [starter_post_html, ...])``."""
        thread_url = self._thread_url(tid, slug)
        first_soup = BeautifulSoup(self._fetch(thread_url), "lxml")
        starter = self._thread_starter_username(first_soup)
        total_pages = self._page_count(first_soup)
        posts: list[str] = list(
            self._starter_posts(first_soup, starter, is_first_page=True)
        )
        for page in range(2, total_pages + 1):
            self._delay()
            page_soup = BeautifulSoup(
                self._fetch(thread_url + f"page-{page}"), "lxml",
            )
            posts.extend(
                self._starter_posts(page_soup, starter, is_first_page=False)
            )
        return first_soup, posts

    def get_chapter_count(self, url_or_id):
        tid = self.parse_story_id(url_or_id)
        _soup, posts = self._walk_starter_posts(
            tid, self._slug_of(url_or_id),
        )
        return max(len(posts), 1)

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        tid = self.parse_story_id(url_or_id)
        slug = self._slug_of(url_or_id)

        logger.info("Fetching %s thread %s...", self.site_name, tid)
        first_soup, all_posts = self._walk_starter_posts(tid, slug)
        meta = self._parse_metadata(first_soup, tid, slug)

        if not all_posts:
            raise ValueError(
                f"No starter posts found in thread {tid}; "
                "the thread may be empty or use a non-default layout."
            )

        num_chapters = len(all_posts)
        meta["num_chapters"] = num_chapters
        meta["chapter_titles"] = {
            str(i + 1): f"Post {i + 1}" for i in range(num_chapters)
        }
        self._save_meta_cache(tid, meta)

        story = Story(
            id=tid,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=self._thread_url(tid, slug),
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        for i, post_html in enumerate(all_posts, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            cached = self._load_chapter_cache(tid, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue
            title = f"Post {i}"
            ch = Chapter(number=i, title=title, html=post_html)
            self._save_chapter_cache(tid, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, title, False)

        return story
