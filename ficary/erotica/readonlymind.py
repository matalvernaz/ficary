"""ReadOnlyMind (readonlymind.com) — mind-control-scene story archive.

Strong hashtag taxonomy across ficary's core kinks (femdom, feet,
cunnilingus, hypnosis...), plain server-side HTML, no Cloudflare.
The age gate is a client-side JS overlay only — the prose is in the
guest HTML underneath it.

Story layout:
    ``/@Author/StorySlug/``      — overview: title, author, tag links,
                                   author-note foreword(s); one-shots
                                   carry the prose right here.
    ``/@Author/StorySlug/<N>/``  — chapter pages for serials; the
                                   overview lists them as story cards.

Prose lives in ``<section class="chapter-prose">``; sections that
also carry ``author-note`` are forewords/synopses, kept out of the
chapter body (the first one becomes the summary).

Story ids are ``@Author/Slug`` strings, hashed to satisfy the Story
model's numeric-id contract (same approach as MCStories).
"""

import hashlib
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

ROM_BASE = "https://readonlymind.com"

ROM_STORY_URL_RE = re.compile(
    r"readonlymind\.com/(?P<ref>@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.I,
)
ROM_REF_RE = re.compile(r"^@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _ref_to_id(ref: str) -> int:
    """Stable integer derived from the ``@Author/Slug`` reference."""
    h = hashlib.md5(ref.encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


class ReadOnlyMindScraper(BaseScraper):
    """Scraper for readonlymind.com."""

    site_name = "readonlymind"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the ``@Author/Slug`` reference."""
        text = str(url_or_id).strip()
        m = ROM_STORY_URL_RE.search(text)
        if m:
            return m.group("ref")
        if ROM_REF_RE.match(text):
            return text
        raise ValueError(
            f"Cannot parse a ReadOnlyMind story from: {text!r}\n"
            "Expected e.g. https://readonlymind.com/@Author/StorySlug/"
        )

    @classmethod
    def cache_key_for_url(cls, url_or_id):
        return _ref_to_id(cls.parse_story_id(url_or_id))

    @staticmethod
    def _story_url(ref: str) -> str:
        return f"{ROM_BASE}/{ref}/"

    @staticmethod
    def _chapter_list(soup, ref: str) -> list[tuple[str, str]]:
        """``[(title, url), ...]`` from the overview's chapter cards.
        Empty for one-shots (their prose sits on the overview page)."""
        out = []
        seen = set()
        for a in soup.find_all(
            "a", href=re.compile(rf"^/{re.escape(ref)}/(\d+)/$"),
        ):
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            title = a.get_text(" ", strip=True)
            out.append((title, f"{ROM_BASE}{href}"))
        return out

    @staticmethod
    def _prose_html(soup) -> str:
        """Join the page's non-foreword ``chapter-prose`` sections."""
        parts = []
        for section in soup.find_all(
            "section", class_=re.compile(r"\bchapter-prose\b"),
        ):
            classes = section.get("class") or []
            if "author-note" in classes:
                continue
            parts.append(section.decode_contents())
        return "\n".join(parts)

    @classmethod
    def _parse_metadata(cls, soup, ref: str) -> dict:
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        # h1 on an age-gate overlay says "Age verification required";
        # the real title is also in <title>.
        if not title or title.lower().startswith("age verification"):
            title_tag = soup.find("title")
            title = (
                title_tag.get_text(" ", strip=True) if title_tag else ""
            )
        author = ref.split("/", 1)[0].lstrip("@")
        by = soup.find("h3")
        if by:
            a = by.find("a", href=re.compile(r"^/@"))
            if a:
                author = a.get_text(strip=True) or author

        summary = ""
        note = soup.find(
            "section", class_=re.compile(r"\bchapter-prose\b.*author-note|"
                                         r"author-note.*\bchapter-prose\b"),
        )
        if note is None:
            for section in soup.find_all("section"):
                classes = section.get("class") or []
                if "chapter-prose" in classes and "author-note" in classes:
                    note = section
                    break
        if note is not None:
            summary = note.get_text(" ", strip=True)

        tags = [
            a.get_text(strip=True).lstrip("#")
            for a in soup.find_all("a", class_="tag-link")
        ]

        return {
            "title": title or ref,
            "author": author,
            "author_url": f"{ROM_BASE}/@{author}/",
            "summary": summary,
            "extra": {"ref": ref, "tags": tags},
        }

    def get_chapter_count(self, url_or_id):
        ref = self.parse_story_id(url_or_id)
        soup = BeautifulSoup(self._fetch(self._story_url(ref)), "lxml")
        return len(self._chapter_list(soup, ref)) or 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        ref = self.parse_story_id(url_or_id)
        story_id = _ref_to_id(ref)
        overview_url = self._story_url(ref)

        logger.info("Fetching ReadOnlyMind story %s...", ref)
        soup = BeautifulSoup(self._fetch(overview_url), "lxml")
        meta = self._parse_metadata(soup, ref)
        chapter_links = self._chapter_list(soup, ref)

        if chapter_links:
            num_chapters = len(chapter_links)
            chapter_titles = {
                str(i): t for i, (t, _) in enumerate(chapter_links, 1)
            }
        else:
            num_chapters = 1
            chapter_titles = {"1": meta["title"]}
        meta["num_chapters"] = num_chapters
        meta["chapter_titles"] = chapter_titles
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=overview_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story

        if not chapter_links:
            if chapter_in_spec(1, chapters) and skip_chapters < 1:
                html = self._prose_html(soup)
                if not html:
                    raise ValueError(
                        f"No prose found on {overview_url} — the page "
                        "layout may have changed."
                    )
                ch = Chapter(number=1, title="", html=html)
                self._save_chapter_cache(story_id, ch)
                story.chapters.append(ch)
                if progress_callback:
                    progress_callback(1, 1, meta["title"], False)
            return story

        for i, (ch_title, ch_url) in enumerate(chapter_links, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            cached = self._load_chapter_cache(story_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue
            self._delay()
            ch_soup = BeautifulSoup(self._fetch(ch_url), "lxml")
            html = self._prose_html(ch_soup)
            if not html:
                raise ValueError(
                    f"No prose found on {ch_url} — the page layout "
                    "may have changed."
                )
            ch = Chapter(number=i, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch_title, False)

        return story
