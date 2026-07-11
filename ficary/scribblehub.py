"""ScribbleHub (scribblehub.com) scraper.

A series lives at ``/series/<id>/<slug>/``; each chapter is a separate
page at ``/read/<id>-<slug>/chapter/<chapter_id>/``. The series page
carries the metadata (title, author, description, genres) but only the
most recent handful of chapters — the full table of contents is fetched
from ScribbleHub's WordPress AJAX endpoint
(``/wp-admin/admin-ajax.php`` action ``wi_getreleases_pagination``),
the same mechanism the site's own "show all chapters" control uses.

ScribbleHub sits behind Cloudflare. The shared fetch machinery in
:class:`~ficary.scraper.BaseScraper` handles that the usual two ways:
the optional ``cf-solve`` Playwright fallback, or a browser-exported
cookie header passed as ``session_cookie`` (``--scribblehub-cookie`` on
the CLI). A logged-in cookie also unlocks members-only and mature
chapters that a bare fetch can't see.
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Story
from .scraper import BaseScraper, CookieAuthMixin, StoryNotFoundError

logger = logging.getLogger(__name__)

SH_BASE = "https://www.scribblehub.com"
SH_AJAX = f"{SH_BASE}/wp-admin/admin-ajax.php"

# ScribbleHub renders series and chapter ids as 7-digit integers today,
# but the id length has grown over the site's life, so match one-or-more
# digits rather than pinning a width.
_SERIES_URL_RE = re.compile(r"scribblehub\.com/series/(\d+)/", re.IGNORECASE)
_READ_URL_RE = re.compile(r"scribblehub\.com/read/(\d+)-", re.IGNORECASE)
_CHAPTER_URL_RE = re.compile(r"/read/\d+-[^/]+/chapter/(\d+)", re.IGNORECASE)
_PROFILE_URL_RE = re.compile(r"scribblehub\.com/profile/\d+", re.IGNORECASE)


class ScribbleHubScraper(CookieAuthMixin, BaseScraper):
    """Scraper for scribblehub.com original fiction."""

    site_name = "scribblehub"
    _auth_cookie_domain = ".scribblehub.com"

    def __init__(self, **kwargs):
        # ScribbleHub tolerates a few parallel chapter fetches; AIMD in
        # _fetch_parallel backs off on any 429/503.
        kwargs.setdefault("concurrency", 3)
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        for pattern in (_SERIES_URL_RE, _READ_URL_RE):
            m = pattern.search(text)
            if m:
                return int(m.group(1))
        raise ValueError(
            f"Cannot parse ScribbleHub series id from: {text!r}\n"
            "Expected a URL like "
            "https://www.scribblehub.com/series/12345/some-slug/ "
            "or a numeric id."
        )

    @staticmethod
    def is_author_url(url):
        return bool(_PROFILE_URL_RE.search(str(url)))

    def _series_url(self, story_id) -> str:
        # The slug is cosmetic — ScribbleHub redirects the slug-less form
        # to the canonical one, so a bare id is enough to load the page.
        return f"{SH_BASE}/series/{story_id}/"

    @staticmethod
    def _parse_metadata(soup) -> dict:
        def text_of(selector):
            el = soup.select_one(selector)
            return el.get_text(" ", strip=True) if el else ""

        title = text_of(".fic_title") or "Untitled"
        author = text_of(".auth_name_fic") or "Unknown Author"
        summary = text_of(".wi_fic_desc")

        author_url = ""
        author_a = soup.select_one("a.auth_name_fic") or soup.find(
            "a", href=_PROFILE_URL_RE,
        )
        if author_a and author_a.get("href"):
            author_url = author_a["href"]

        genres = [
            a.get_text(strip=True)
            for a in soup.select("a.fic_genre")
            if a.get_text(strip=True)
        ]

        return {
            "title": title,
            "author": author,
            "summary": summary,
            "author_url": author_url,
            "extra": {"fandoms": genres} if genres else {},
        }

    @staticmethod
    def _parse_toc_anchors(soup) -> list[dict]:
        """Return ``[{id, title, url, unixtime}]`` from a block of
        ``a.toc_a`` chapter links, oldest chapter first.

        ScribbleHub lists chapters newest-first both on the series page
        and in the AJAX fragment, so the list is reversed to reading
        order before returning."""
        chapters = []
        seen = set()
        for a in soup.select("a.toc_a"):
            href = a.get("href") or ""
            m = _CHAPTER_URL_RE.search(href)
            if not m:
                continue
            chap_id = int(m.group(1))
            if chap_id in seen:
                continue
            seen.add(chap_id)
            chapters.append({
                "id": chap_id,
                "title": a.get_text(strip=True) or f"Chapter {chap_id}",
                "url": href if href.startswith("http") else SH_BASE + href,
                "unixtime": None,
            })
        chapters.reverse()
        return chapters

    def _fetch_full_toc(self, story_id, series_soup) -> list[dict]:
        """Fetch the complete chapter list.

        The series page only embeds the latest chapters, so ask the
        AJAX endpoint for the whole table of contents. The GET of the
        series page (already done by the caller) has primed the session
        with any Cloudflare-clearance cookies, so the POST rides the
        same session. Falls back to whatever chapters the series page
        did embed if the AJAX call fails or returns nothing.
        """
        page_chapters = self._parse_toc_anchors(series_soup)
        try:
            self._delay()
            sess = self._session()
            resp = sess.post(
                SH_AJAX,
                data={
                    "action": "wi_getreleases_pagination",
                    "pagenum": "1",
                    "mypostid": str(story_id),
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._series_url(story_id),
                },
                timeout=30,
            )
            if resp.status_code == 200 and resp.text.strip():
                ajax_chapters = self._parse_toc_anchors(
                    BeautifulSoup(resp.text, "lxml")
                )
                # The AJAX list is authoritative when it's at least as
                # complete as the embedded one; only fall back when it
                # came back short (challenge page, markup change).
                if len(ajax_chapters) >= len(page_chapters):
                    return ajax_chapters
        except Exception as exc:
            logger.debug("ScribbleHub TOC AJAX failed: %s", exc, exc_info=True)
        return page_chapters

    @staticmethod
    def _parse_chapter_html(soup):
        content = soup.select_one("#chp_raw") or soup.select_one(".chp_raw")
        if content is None:
            raise ValueError(
                "Could not locate ScribbleHub chapter body "
                "(page layout may have changed)."
            )
        # Author notes are rendered in a sibling widget, but a stray
        # ``.wi_authornotes`` block can land inside the body container on
        # some layouts — drop it so notes don't masquerade as prose.
        for note in content.select(".wi_authornotes"):
            note.decompose()
        return content.decode_contents()

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        html = self._fetch(self._series_url(story_id))
        soup = BeautifulSoup(html, "lxml")
        return len(self._fetch_full_toc(story_id, soup))

    def scrape_author_stories(self, url):
        """A ScribbleHub profile lists the member's own series."""
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if t:
                author_name = t.split("|")[0].strip() or author_name

        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=_SERIES_URL_RE):
            m = _SERIES_URL_RE.search(a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                story_urls.append(self._series_url(m.group(1)))
        return author_name, story_urls

    def download(self, url_or_id, progress_callback=None, skip_chapters=0,
                 chapters=None):
        story_id = self.parse_story_id(url_or_id)
        series_url = self._series_url(story_id)

        logger.info("Fetching ScribbleHub series %s...", story_id)
        html = self._fetch(series_url)
        soup = BeautifulSoup(html, "lxml")

        meta = self._parse_metadata(soup)
        chapter_list = self._fetch_full_toc(story_id, soup)
        if not chapter_list:
            raise StoryNotFoundError(
                f"No chapters found on ScribbleHub series {story_id}."
            )

        self._save_meta_cache(
            story_id, {**meta, "num_chapters": len(chapter_list)},
        )

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=series_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        story.chapters.extend(self._materialise_chapters(
            story_id=story_id,
            chapter_list=chapter_list,
            skip_chapters=skip_chapters,
            chapter_spec=chapters,
            parse_chapter=self._parse_chapter_html,
            progress_callback=progress_callback,
        ))
        return story
