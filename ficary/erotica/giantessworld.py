"""Giantess World (giantessworld.net) — eFiction giantess archive.

Nearly 50k stories; feet/foot-worship content is pervasive in-niche.
Classic eFiction layout:

    ``viewstory.php?sid=N``             — story index: ``#pagetitle``
                                          carries "Title by Author",
                                          chapter links carry
                                          ``&chapter=M``.
    ``viewstory.php?sid=N&chapter=M``   — chapter body in the
                                          ``#story`` div.

Listings live at ``browse.php?type=recent&offset=K`` (20 rows per
page) — the search adapter uses the same page.
"""

import logging
import re
from typing import Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

GW_BASE = "https://giantessworld.net"

GW_STORY_URL_RE = re.compile(
    r"giantessworld\.net/viewstory\.php\?(?:[^#\s]*&)?sid=(?P<sid>\d+)",
    re.I,
)

# Link texts on the story index that are chrome, not chapter titles.
_GW_NON_CHAPTER_TEXT = {"table of contents", "report this", "printer"}


class GiantessWorldScraper(BaseScraper):
    """Scraper for giantessworld.net (eFiction)."""

    site_name = "giantessworld"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric story id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = GW_STORY_URL_RE.search(text)
        if m:
            return int(m.group("sid"))
        raise ValueError(
            f"Cannot parse a Giantess World story id from: {text!r}\n"
            "Expected e.g. https://giantessworld.net/viewstory.php?sid=11467"
        )

    @staticmethod
    def _index_url(sid: int) -> str:
        return f"{GW_BASE}/viewstory.php?sid={sid}"

    @classmethod
    def _chapter_url(cls, sid: int, chapter: int) -> str:
        return f"{cls._index_url(sid)}&chapter={chapter}"

    @classmethod
    def _parse_metadata(cls, soup, sid: int) -> dict:
        title = f"Giantess World story {sid}"
        author = "Unknown Author"
        author_url = ""
        pagetitle = soup.find(id="pagetitle")
        if pagetitle:
            story_a = pagetitle.find(
                "a", href=re.compile(rf"viewstory\.php\?sid={sid}\b"),
            )
            if story_a:
                title = story_a.get_text(" ", strip=True) or title
            user_a = pagetitle.find(
                "a", href=re.compile(r"viewuser\.php\?uid=\d+"),
            )
            if user_a:
                author = user_a.get_text(" ", strip=True) or author
                author_url = f"{GW_BASE}/{user_a['href'].lstrip('/')}"

        summary = ""
        # eFiction renders the synopsis in the story listing block on
        # the index page; the first sizable text block under the
        # content div is a good stand-in across skins.
        content = soup.find("div", class_="content") or soup
        p = content.find("p")
        if p:
            summary = p.get_text(" ", strip=True)

        chapters: list[tuple[str, int]] = []
        seen: set[int] = set()
        for a in soup.find_all(
            "a",
            href=re.compile(rf"viewstory\.php\?sid={sid}&(?:amp;)?chapter=\d+"),
        ):
            m = re.search(r"chapter=(\d+)", unquote(a["href"]))
            if not m:
                continue
            num = int(m.group(1))
            text = a.get_text(" ", strip=True)
            if not text or text.strip().lower() in _GW_NON_CHAPTER_TEXT:
                continue
            if num in seen:
                continue
            seen.add(num)
            chapters.append((text, num))
        chapters.sort(key=lambda c: c[1])

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": len(chapters) or 1,
            "chapter_titles": {
                str(i): t for i, (t, _n) in enumerate(chapters, 1)
            },
            "extra": {
                "sid": sid,
                "chapter_numbers": [n for _t, n in chapters] or [1],
            },
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        body = soup.find(id="story")
        if body is None:
            body = soup.find("div", class_="story")
        if body is None:
            raise ValueError(
                "Could not locate the Giantess World chapter body "
                "(page layout may have changed)."
            )
        return body.decode_contents()

    def get_chapter_count(self, url_or_id):
        sid = self.parse_story_id(url_or_id)
        soup = BeautifulSoup(self._fetch(self._index_url(sid)), "lxml")
        return self._parse_metadata(soup, sid)["num_chapters"]

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        sid = self.parse_story_id(url_or_id)

        logger.info("Fetching Giantess World story %s...", sid)
        soup = BeautifulSoup(self._fetch(self._index_url(sid)), "lxml")
        meta = self._parse_metadata(soup, sid)
        num_chapters = meta["num_chapters"]
        chapter_numbers = meta["extra"]["chapter_numbers"]
        chapter_titles = meta["chapter_titles"]
        self._save_meta_cache(sid, meta)

        story = Story(
            id=sid,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=self._index_url(sid),
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story

        for i, site_num in enumerate(chapter_numbers, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            ch_title = chapter_titles.get(str(i), "")

            cached = self._load_chapter_cache(sid, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue

            self._delay()
            page = self._fetch(self._chapter_url(sid, site_num))
            html = self._parse_chapter_html(BeautifulSoup(page, "lxml"))
            ch = Chapter(number=i, title=ch_title, html=html)
            self._save_chapter_cache(sid, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch_title, False)

        return story
