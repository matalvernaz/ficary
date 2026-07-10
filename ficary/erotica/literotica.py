"""Literotica (literotica.com) scraper.

Literotica publishes a single submission as one URL (/s/<slug>) that
may be paginated via ?page=N. The pages are an arbitrary length split
— they break mid-scene, not at story beats — so a submission is ONE
chapter in the Story model with its pages concatenated. (Earlier
versions emitted each page as its own "chapter", which littered
exports with fake "Page 2" chapter headings.) Real chapters are
separate submissions grouped under /series/se/<id>; expanding those
works the same way as AO3 series, one chapter per part.

The layout uses CSS-module hashed class names that change between
site builds, so selectors match on the module *prefix* (e.g.
`_article__content_`) rather than the full obfuscated class.
"""

import hashlib
import logging
import re

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

LIT_BASE = "https://www.literotica.com"

# Cache stem for the merged one-chapter-per-submission format. Distinct
# from the ordinal ``ch_NNNN`` stems the old page-per-chapter versions
# wrote, so a stale page-1-only cache can never be served as the whole
# story.
MERGED_CACHE_KEY = "submission"

_SLUG_RE = re.compile(r"literotica\.com/s/([a-z0-9-]+)", re.IGNORECASE)
_SERIES_RE = re.compile(r"literotica\.com/series/se/(\d+)", re.IGNORECASE)
_AUTHOR_RE = re.compile(r"literotica\.com/authors/([^/?#]+)", re.IGNORECASE)


def _slug_to_id(slug: str) -> int:
    """Stable integer derived from the story slug — Literotica's canonical
    identifier is a string, but our Story model expects numeric ids so we
    hash deterministically. 48 bits is plenty of room to avoid collisions
    across a user's library."""
    h = hashlib.md5(slug.encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


class LiteroticaScraper(BaseScraper):
    """Scraper for literotica.com stories and series."""

    site_name = "literotica"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the *slug* (not an int) — use _slug_to_id for numeric.

        Callers that want a numeric id should go through _slug_to_id.
        """
        text = str(url_or_id).strip()
        m = _SLUG_RE.search(text)
        if m:
            return m.group(1)
        # Accept a bare slug as well
        if re.fullmatch(r"[a-z0-9][a-z0-9-]+", text, re.IGNORECASE):
            return text
        raise ValueError(
            f"Cannot parse Literotica story slug from: {text!r}\n"
            "Expected a URL like https://www.literotica.com/s/story-slug "
            "or a bare slug."
        )

    @classmethod
    def cache_key_for_url(cls, url_or_id):
        """Cache writes use ``_slug_to_id`` of the slug — mirror that here
        so the cache_doctor's orphan match lines up with disk."""
        return _slug_to_id(cls.parse_story_id(url_or_id))

    @staticmethod
    def is_author_url(url):
        return bool(_AUTHOR_RE.search(str(url)))

    @staticmethod
    def is_series_url(url):
        return bool(_SERIES_RE.search(str(url)))

    def resolve_series_url(self, story_url):
        """Given any chapter URL (/s/<slug>-ch-NN), fetch the page and
        return the canonical /series/se/<id> URL if the story belongs
        to a series, else None.
        """
        slug = self.parse_story_id(story_url)
        html = self._fetch_page(slug, 1)
        m = re.search(r"/series/se/(\d+)", html)
        if not m:
            return None
        return f"{LIT_BASE}/series/se/{m.group(1)}"

    @staticmethod
    def _content_div(soup):
        """Return the element wrapping the story body, or ``None``.

        Literotica's CSS-module class names rotate on each build
        (``_article__content_10cj1_81`` today, a different hash tomorrow),
        so we try increasingly structural selectors and stop on the
        first hit. Most-stable first:

        1. ``itemprop="articleBody"`` — schema.org microdata. Literotica
           has exposed this on the story body for years because it's
           what screen readers and search indexers rely on; it survives
           pure CSS-bundle rebuilds that would invalidate the hashed
           class.
        2. Any element whose class contains the ``_article__content_``
           module prefix. Matches the current build without pinning to
           a specific hash suffix.
        3. ``<article itemtype="https://schema.org/Article">`` — the
           enclosing semantic element, for the case where Literotica
           drops the inner ``itemprop`` but keeps Article-level markup.
        """
        el = soup.find(attrs={"itemprop": "articleBody"})
        if el is not None:
            return el
        el = soup.find(
            ["div", "article"],
            class_=re.compile(r"_article__content_", re.I),
        )
        if el is not None:
            return el
        return soup.find(
            "article",
            attrs={"itemtype": re.compile(r"schema\.org/Article", re.I)},
        )

    @staticmethod
    def _intro_div(soup):
        """The summary/blurb. Literotica's current layout has no standalone
        intro div — the old `_introduction__text_` class is now where the
        story body lives. Fall back to the meta description tag, which
        the site still populates with the author-written blurb."""
        m = soup.find("meta", attrs={"name": "description"})
        if m and m.get("content"):
            return m
        m = soup.find("meta", attrs={"property": "og:description"})
        if m and m.get("content"):
            return m
        return None

    @staticmethod
    def _page_count(soup):
        """Return the number of paginated pages for this story."""
        max_page = 1
        for a in soup.find_all("a", href=re.compile(r"\?page=\d+")):
            m = re.search(r"\?page=(\d+)", a["href"])
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    @staticmethod
    def _parse_author(soup):
        """Return (author_name, author_url) from the first /authors/ link
        that carries visible text (not an icon-only bookmark link)."""
        name = "Unknown Author"
        url = ""
        for a in soup.find_all("a", href=_AUTHOR_RE):
            text = a.get_text(strip=True)
            # Skip stats-row links ("12,453 reads", "4.5 ★", etc.) but
            # accept handles that legitimately start with digits
            # (1ManArmy, 2HotForCollege, 99Cents are real Lit handles).
            if not text or len(text) >= 40:
                continue
            if text.isdigit():
                continue
            if any(ch in text for ch in "★·•"):
                continue
            name = text
            href = a["href"]
            url = href if href.startswith("http") else LIT_BASE + href
            break
        if not url:
            # Fall back to the slug in the href
            a = soup.find("a", href=_AUTHOR_RE)
            if a:
                m = _AUTHOR_RE.search(a["href"])
                if m:
                    name = m.group(1)
                    url = f"{LIT_BASE}/authors/{name}/works/stories"
        return name, url

    def _parse_metadata(self, soup, slug):
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else slug

        author, author_url = self._parse_author(soup)

        intro = self._intro_div(soup)
        if intro is None:
            summary = ""
        elif intro.name == "meta":
            summary = (intro.get("content") or "").strip()
        else:
            summary = intro.get_text(" ", strip=True)

        num_pages = self._page_count(soup)

        extra = {"num_pages": num_pages}

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
            "num_pages": num_pages,
            # One chapter per submission — pages are a length split,
            # not story structure.
            "num_chapters": 1,
        }

    def _fetch_page(self, slug, page_num):
        url = f"{LIT_BASE}/s/{slug}"
        if page_num > 1:
            url += f"?page={page_num}"
        return self._fetch(url)

    def get_chapter_count(self, url_or_id):
        """A submission is always exactly one chapter (its pages are a
        length split, not story structure), so this never needs the
        network. New parts of a serial arrive as new submissions in
        the series — an existing submission doesn't grow."""
        self.parse_story_id(url_or_id)  # still validate the URL shape
        return 1

    def scrape_author_stories(self, url):
        """Return (author_name, [story_urls]) for a Literotica author page."""
        m = _AUTHOR_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica author URL: {url}")
        slug = m.group(1)
        works_url = f"{LIT_BASE}/authors/{slug}/works/stories"
        html = self._fetch(works_url)
        soup = BeautifulSoup(html, "lxml")

        author_name = slug
        # Try heading text first
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and len(text) < 60:
                author_name = text

        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=_SLUG_RE):
            m = _SLUG_RE.search(a["href"])
            if not m:
                continue
            story_slug = m.group(1)
            # Skip known non-story slugs (promo banners etc. use /s/ for events)
            if story_slug in seen:
                continue
            seen.add(story_slug)
            story_urls.append(f"{LIT_BASE}/s/{story_slug}")

        return author_name, story_urls

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a Literotica author page."""
        m = _AUTHOR_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica author URL: {url}")
        slug = m.group(1)
        works_url = f"{LIT_BASE}/authors/{slug}/works/stories"
        html = self._fetch(works_url)
        soup = BeautifulSoup(html, "lxml")

        author_name = slug
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            # The works page titles itself "Stories by <name>".
            text = re.sub(r"^Stories\s+by\s+", "", text, flags=re.I)
            if text and len(text) < 60:
                author_name = text

        seen = set()
        works = []
        # The works page is React SSR with content-hashed CSS-module
        # classes (``_card_1gpbw_15`` — the hash churns per build, so
        # match on the stable prefix). Each <article> card: h3 title
        # anchor, a real per-story blurb in p._description_*, and a
        # meta row with the category link and a M/D/YYYY date. No word
        # or page count appears anywhere on the cards.
        for card in soup.select("article[class*='_card_']"):
            a = card.find("a", href=_SLUG_RE)
            if a is None:
                continue
            m2 = _SLUG_RE.search(a["href"])
            if not m2:
                continue
            story_slug = m2.group(1)
            if story_slug in seen:
                continue
            seen.add(story_slug)

            summary = ""
            desc = card.select_one("p[class*='_description_']")
            if desc is not None:
                summary = desc.get_text(" ", strip=True)

            fandom = ""
            updated = ""
            meta = card.select_one("div[class*='_meta_row_']")
            if meta is not None:
                cat_a = meta.find(
                    "a", href=re.compile(r"literotica\.com/c/|^/c/"),
                )
                if cat_a is not None:
                    fandom = cat_a.get_text(" ", strip=True)
                d_m = re.search(
                    r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
                    meta.get_text(" ", strip=True),
                )
                if d_m:
                    updated = (
                        f"{d_m.group(3)}-{int(d_m.group(1)):02d}"
                        f"-{int(d_m.group(2)):02d}"
                    )

            works.append({
                "title": a.get_text(strip=True) or story_slug,
                "url": f"{LIT_BASE}/s/{story_slug}",
                "author": author_name,
                "summary": summary,
                "words": "",
                "chapters": "",
                "rating": "",
                "fandom": fandom,
                "status": "",
                "updated": updated,
                "section": "own",
            })
        if works:
            return author_name, works
        # Markup-drift fallback: the old flat anchor walk (titles only).
        for a in soup.find_all("a", href=_SLUG_RE):
            m2 = _SLUG_RE.search(a["href"])
            if not m2:
                continue
            story_slug = m2.group(1)
            if story_slug in seen:
                continue
            seen.add(story_slug)
            works.append({
                "title": a.get_text(strip=True) or story_slug,
                "url": f"{LIT_BASE}/s/{story_slug}",
                "author": author_name,
                "words": "",
                "chapters": "",
                "rating": "",
                "fandom": "",
                "status": "",
                "updated": "",
                "section": "own",
            })
        return author_name, works

    def scrape_series_works(self, url):
        """Return (series_name, [story_urls]) for a Literotica series page."""
        m = _SERIES_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica series URL: {url}")
        series_id = m.group(1)
        page_url = f"{LIT_BASE}/series/se/{series_id}"
        html = self._fetch(page_url)
        soup = BeautifulSoup(html, "lxml")

        series_name = "Literotica series"
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(strip=True)
            if t:
                series_name = t

        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=_SLUG_RE):
            m2 = _SLUG_RE.search(a["href"])
            if not m2:
                continue
            slug = m2.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            story_urls.append(f"{LIT_BASE}/s/{slug}")
        return series_name, story_urls

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        slug = self.parse_story_id(url_or_id)
        story_id = _slug_to_id(slug)
        story_url = f"{LIT_BASE}/s/{slug}"

        logger.info("Fetching Literotica story %s...", slug)
        page1_html = self._fetch_page(slug, 1)
        soup = BeautifulSoup(page1_html, "lxml")

        meta = self._parse_metadata(soup, slug)
        num_pages = meta["num_pages"]
        meta["extra"]["slug"] = slug
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        chrome_prefixes = (
            "_widget", "_pager", "_pagination",
            "_share", "_tags",
            "_rating", "_comments",
        )

        def extract_body(page_soup):
            body = self._content_div(page_soup)
            if body is None:
                raise ValueError(
                    "Could not locate Literotica story body "
                    "(page layout may have changed)."
                )
            # Drop site chrome & pagination widgets that also land inside
            # _article__content_. Collect victims first, then decompose,
            # so iterating a live tree doesn't invalidate references.
            victims = []
            for tag in body.find_all(True):
                if tag.attrs is None:
                    continue
                classes = tag.attrs.get("class") or []
                if any(
                    any(prefix in cls for prefix in chrome_prefixes)
                    for cls in classes
                ):
                    victims.append(tag)
            for tag in victims:
                if tag.parent is not None:
                    tag.decompose()
            return body.decode_contents()

        # A submission's ?page=N splits are arbitrary length breaks
        # (mid-scene), not story structure — so all pages concatenate
        # into ONE chapter. Skip/spec therefore apply to that single
        # chapter, and the merged body caches under MERGED_CACHE_KEY
        # (ordinal ``ch_NNNN`` stems belong to the retired
        # page-per-chapter format; reading them here would serve a
        # page-1-only story from stale caches).
        if skip_chapters >= 1 or not chapter_in_spec(1, chapters):
            return story

        cached = self._load_chapter_cache(
            story_id, 1, cache_key=MERGED_CACHE_KEY,
        )
        if cached is not None:
            story.chapters.append(cached)
            if progress_callback:
                progress_callback(1, 1, cached.title, True)
            return story

        bodies = [extract_body(soup)]
        for page in range(2, num_pages + 1):
            self._delay()
            if progress_callback:
                progress_callback(
                    page, num_pages, f"{meta['title']} (page {page})", False,
                )
            page_html = self._fetch_page(slug, page)
            bodies.append(extract_body(BeautifulSoup(page_html, "lxml")))

        ch = Chapter(number=1, title=meta["title"], html="\n".join(bodies))
        self._save_chapter_cache(story_id, ch, cache_key=MERGED_CACHE_KEY)
        story.chapters.append(ch)
        if progress_callback:
            progress_callback(1, 1, ch.title, False)

        return story
