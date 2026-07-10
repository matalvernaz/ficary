"""Royal Road (royalroad.com) scraper.

Fiction landing page lists every chapter in `table#chapters` with direct
links; per-chapter content lives in `div.chapter-inner.chapter-content`.
Cleaner than FFN — no captcha wall, chapter list is already complete
without pagination.
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Story
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

RR_BASE = "https://www.royalroad.com"

# RR's house conversion for its "N pages" stat (words per page); the
# same factor the search-card parser in ficary.search uses.
RR_WORDS_PER_PAGE = 275


def _parse_abbreviated_count(raw: str) -> int:
    """Parse RR's abbreviated counters — "2.7k" → 2700, "796" → 796.
    Returns 0 on anything unparseable."""
    s = raw.strip().lower().replace(",", "")
    multiplier = 1000 if s.endswith("k") else 1
    if s.endswith("k"):
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return 0


class RoyalRoadScraper(BaseScraper):
    """Scraper for royalroad.com."""

    site_name = "royalroad"

    def __init__(self, **kwargs):
        # Royal Road is happy with a few parallel connections; AIMD in
        # _fetch_parallel halves this on any 429/503.
        kwargs.setdefault("concurrency", 3)
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"royalroad\.com/fiction/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse Royal Road fiction ID from: {text!r}\n"
            "Expected a URL like https://www.royalroad.com/fiction/12345 "
            "or a numeric ID."
        )

    @staticmethod
    def is_author_url(url):
        return bool(re.search(r"royalroad\.com/profile/\d+", str(url)))

    @staticmethod
    def is_search_url(url):
        """Return True if the URL is a Royal Road fiction search.

        RR's search lives at ``/fictions/search`` with optional
        ``title``, ``keyword``, ``status_*``, ``tagsAdd`` filters.
        We don't try to discriminate facets here — anything under
        the search path qualifies and the user's filter choices
        survive verbatim.
        """
        return bool(
            re.search(r"royalroad\.com/fictions/search", str(url))
        )

    @staticmethod
    def _parse_metadata(soup):
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find("a", href=re.compile(r"/profile/\d+"))
        if author_link:
            author = author_link.get_text(strip=True)
            href = author_link["href"]
            author_url = RR_BASE + href if href.startswith("/") else href

        desc = soup.find("div", class_="description")
        summary = desc.get_text(" ", strip=True) if desc else ""

        extra = {}
        cover = soup.find("img", class_="thumbnail")
        if cover and cover.get("src"):
            extra["cover_url"] = cover["src"]

        # "Original / Fanfiction", "ONGOING / COMPLETED / HIATUS / STUB", "N Chapters"
        status = None
        for label in soup.find_all("span", class_="label"):
            text = label.get_text(strip=True).upper()
            if text in ("ONGOING", "COMPLETED", "HIATUS", "STUB", "DROPPED"):
                status = "Complete" if text == "COMPLETED" else text.title()
        if status:
            extra["status"] = status

        # Tags from the fiction's tag list — stored as "genre"
        tag_links = soup.select("span.tags a.fiction-tag")
        if tag_links:
            extra["genre"] = ", ".join(
                a.get_text(strip=True) for a in tag_links[:12]
            )

        # The fiction page shows page counts up front, but the real
        # word count hides in the pages-tooltip text: "... an average
        # of 275 words per page, calculated from 751,549 words." It's
        # the authoritative site count, so exports prefer it over
        # counting the downloaded text.
        w_m = re.search(
            r"calculated from\s+([\d,]+)\s+words",
            soup.get_text(" ", strip=True),
        )
        if not w_m:
            # The tooltip sometimes lives in a data-content/title
            # attribute rather than rendered text.
            w_m = re.search(r"calculated from\s+([\d,]+)\s+words", str(soup))
        if w_m:
            extra["words"] = w_m.group(1)

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_list(soup):
        """Return a list of {id, title, url, unixtime} dicts from the chapters table.
        `unixtime` is the chapter's publish time as an int epoch, or
        None if the row doesn't expose one.
        """
        table = soup.find("table", id="chapters")
        if not table:
            return []
        tbody = table.find("tbody") or table
        chapters = []
        for row in tbody.find_all("tr"):
            link = row.find(
                "a", href=re.compile(r"/fiction/\d+/[^/]+/chapter/\d+")
            )
            if not link:
                continue
            href = link["href"]
            match = re.search(r"/chapter/(\d+)", href)
            if not match:
                continue
            unixtime = None
            time_tag = row.find("time")
            if time_tag and time_tag.get("unixtime"):
                try:
                    unixtime = int(time_tag["unixtime"])
                except ValueError:
                    unixtime = None
            chapters.append({
                "id": int(match.group(1)),
                "title": link.get_text(strip=True),
                "url": RR_BASE + href if href.startswith("/") else href,
                "unixtime": unixtime,
            })
        return chapters

    # Royal Road injects anti-piracy paragraphs ("if you spot this
    # narrative on amazon, know that it has been stolen — report the
    # violation", and rotating variants) into chapter HTML. Each
    # injected element carries a random class that's hidden via a
    # display:none rule in the same page's <style> block. Real browsers
    # never show them; curl_cffi doesn't render CSS, so the text ends up
    # in the EPUB unless we strip at scrape time. FanFicFare and
    # Aivean/royalroad-downloader both solve this the same way: collect
    # the hidden classes from CSS, drop elements that use them. That's
    # survived ~2 years of RR rotating both class names and phrasing.
    #
    # We only honour *simple* class selectors and comma-grouped simple
    # class selectors here: ``.foo { display:none }`` and ``.foo, .bar
    # { display:none }``. A descendant selector like ``.outer .inner
    # { display:none }`` only hides ``.inner`` *inside* ``.outer``, not
    # any element with class ``outer``, so blindly removing every
    # ``.outer`` element (as the previous "extract every class name"
    # logic would) could delete a visible container — and with it the
    # entire chapter body.
    # Anchor at a rule boundary (start-of-text or right after the
    # previous rule's closing ``}``) so a descendant selector like
    # ``.outer .inner { display:none }`` doesn't backtrack into
    # ``.inner { display:none }`` and incorrectly flag every ``.inner``
    # element on the page as hidden. The boundary is matched with a
    # zero-width lookbehind so consecutive rules in the same <style>
    # block each anchor cleanly without the previous match swallowing
    # the next rule's ``}`` separator.
    _HIDDEN_RULE_RE = re.compile(
        r"(?:^|(?<=\}))\s*"
        r"(?P<selectors>\.[A-Za-z0-9_-]+(?:\s*,\s*\.[A-Za-z0-9_-]+)*)\s*\{"
        r"[^}]*"
        r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|"
        r"font-size\s*:\s*0|speak\s*:\s*never)"
        r"[^}]*\}",
        re.IGNORECASE,
    )
    _CLASS_NAME_RE = re.compile(r"\.([A-Za-z0-9_-]+)")

    @classmethod
    def _hidden_classes(cls, soup) -> set:
        """Collect CSS class names that any inline <style> block hides.

        Royal Road's anti-piracy injection attaches one of these classes
        to the paragraph it wants hidden. We only look at <style> tags
        on the page itself — external stylesheets are normal site CSS
        unrelated to per-request injection.
        """
        classes = set()
        for style in soup.find_all("style"):
            css = style.string or style.get_text() or ""
            if not css:
                continue
            for match in cls._HIDDEN_RULE_RE.finditer(css):
                selector_block = match.group("selectors")
                for name_match in cls._CLASS_NAME_RE.finditer(selector_block):
                    classes.add(name_match.group(1))
        return classes

    @classmethod
    def _parse_chapter_html(cls, soup):
        content = soup.find("div", class_="chapter-inner")
        if content is None:
            content = soup.find("div", class_="chapter-content")
        if content is None:
            raise ValueError("Could not locate chapter content on Royal Road page.")

        hidden = cls._hidden_classes(soup)
        if hidden:
            # Collect first, then decompose: mutating the tree mid-iteration
            # leaves orphaned descendants whose `attrs` becomes None, which
            # then crashes the next `tag.get("class")` call.
            doomed = [
                tag for tag in content.find_all(True)
                if any(c in hidden for c in (tag.get("class") or []))
            ]
            for tag in doomed:
                tag.decompose()
            removed = len(doomed)
            if removed:
                logger.debug(
                    "Stripped %d element(s) hidden by page CSS (likely "
                    "Royal Road anti-piracy injection)", removed,
                )
        return content.decode_contents()

    def get_chapter_count(self, url_or_id):
        fiction_id = self.parse_story_id(url_or_id)
        html = self._fetch(f"{RR_BASE}/fiction/{fiction_id}")
        soup = BeautifulSoup(html, "lxml")
        return len(self._parse_chapter_list(soup))

    def scrape_author_stories(self, url):
        """Author page lists fictions they've written under 'Fictions'."""
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if " |" in t:
                author_name = t.split(" |")[0].strip()

        seen = set()
        story_urls = []
        for a in soup.find_all(
            "a", href=re.compile(r"^/fiction/\d+(?:/[^/]+)?/?$")
        ):
            match = re.search(r"/fiction/(\d+)", a["href"])
            if match and match.group(1) not in seen:
                seen.add(match.group(1))
                story_urls.append(f"{RR_BASE}/fiction/{match.group(1)}")

        return author_name, story_urls

    def scrape_search_works(self, url):
        """Walk a Royal Road search URL and return
        ``(query_label, [work_dict, ...])``.

        Pagination is via ``page=N``. Each result row is a
        ``div.fiction-list-item`` carrying title, author link,
        a description blurb, and the tag chips.
        """
        from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        params = parse_qs(parts.query)
        label = (
            params.get("title", [""])[0]
            or params.get("keyword", [""])[0]
            or ""
        )

        works = []
        seen = set()
        page = 1
        max_pages = 200

        while page <= max_pages:
            params["page"] = [str(page)]
            page_url = urlunsplit((
                parts.scheme, parts.netloc, parts.path,
                urlencode(params, doseq=True), parts.fragment,
            ))
            html = self._fetch(page_url)
            soup = BeautifulSoup(html, "lxml")
            new_on_page = 0
            for item in soup.find_all("div", class_="fiction-list-item"):
                title_link = item.find(
                    "a", href=re.compile(r"^/fiction/\d+(?:/|$)"),
                )
                if not title_link:
                    continue
                m = re.search(r"/fiction/(\d+)", title_link["href"])
                if not m:
                    continue
                fid = m.group(1)
                if fid in seen:
                    continue
                seen.add(fid)
                # Author link is an anchor pointing at /profile/<id>
                author_link = item.find(
                    "a", href=re.compile(r"^/profile/\d+"),
                )
                # Description blurb sits under a `.description` or
                # `.fiction-description` div depending on listing variant.
                desc = item.find("div", class_="description") or item.find(
                    "div", class_="fiction-description",
                )
                tags = ", ".join(
                    a.get_text(strip=True) for a in item.select(
                        "span.tags a.fiction-tag",
                    )[:8]
                )
                # Cards expose "N Pages", not a word count — estimate
                # at RR's house 275 words/page, "~"-prefixed, matching
                # the GUI search parser.
                item_text = item.get_text(" ", strip=True)
                pages_m = re.search(r"(\d[\d,]*)\s+Pages", item_text)
                words = (
                    f"~{int(pages_m.group(1).replace(',', '')) * 275:,}"
                    if pages_m else ""
                )
                ch_m = re.search(r"(\d[\d,]*)\s+Chapters", item_text)
                works.append({
                    "title": title_link.get_text(strip=True) or f"Fiction {fid}",
                    "url": f"{RR_BASE}/fiction/{fid}",
                    "author": author_link.get_text(strip=True) if author_link else "",
                    "summary": desc.get_text(" ", strip=True) if desc else "",
                    "words": words,
                    "chapters": ch_m.group(1) if ch_m else "",
                    "rating": "",
                    "fandom": tags,
                    "status": "",
                    "updated": "",
                    "section": "search",
                })
                new_on_page += 1
            if new_on_page == 0:
                break
            page += 1
            self._delay()

        return label or "Search results", works

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a RR profile page.

        The bare ``/profile/<id>`` page only shows aggregate stats —
        the fiction list lives on the ``/fictions`` tab, one
        ``div.mt-overlay-3`` card per fiction with the title, a
        STUB/status ribbon, "N pages" (k-abbreviated), and the full
        description.
        """
        fictions_url = url.rstrip("/")
        if not fictions_url.endswith("/fictions"):
            fictions_url += "/fictions"
        html = self._fetch(fictions_url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if " |" in t:
                author_name = t.split(" |")[0].strip()
            # The /fictions tab titles itself "<Name>'s Fictions".
            author_name = re.sub(
                r"['’]s Fictions$", "", author_name,
            ).strip() or author_name

        seen = set()
        works = []
        for card in soup.find_all("div", class_="mt-overlay-3"):
            a = card.find("a", href=re.compile(r"^/fiction/\d+(?:/[^/]+)?/?$"))
            if a is None:
                continue
            match = re.search(r"/fiction/(\d+)", a["href"])
            if not match:
                continue
            fid = match.group(1)
            if fid in seen:
                continue
            seen.add(fid)

            h2 = card.find("h2")
            work_title = ""
            status = ""
            words = ""
            if h2 is not None:
                # h2 holds the title as its first text line, then
                # <small>STUB</small> / <small>2.7k pages</small>.
                first_line = next(
                    (
                        t.strip() for t in h2.find_all(string=True)
                        if t.strip()
                    ),
                    "",
                )
                work_title = first_line
                h2_text = h2.get_text(" ", strip=True)
                if re.search(r"\bSTUB\b", h2_text):
                    status = "Stub"
                p_m = re.search(r"([\d.]+k?|\d[\d,]*)\s+pages", h2_text, re.I)
                if p_m:
                    pages = _parse_abbreviated_count(p_m.group(1))
                    if pages:
                        words = f"~{pages * RR_WORDS_PER_PAGE:,}"
            if not work_title:
                work_title = a.get_text(strip=True) or f"Fiction {fid}"

            summary = ""
            desc = card.find("div", class_="fiction-description")
            if desc is not None:
                summary = desc.get_text(" ", strip=True)

            works.append({
                "title": work_title,
                "url": f"{RR_BASE}/fiction/{fid}",
                "author": author_name,
                "summary": summary,
                "words": words,
                "chapters": "",
                "rating": "",
                "fandom": "",
                "status": status,
                "updated": "",
                "section": "own",
            })
        return author_name, works

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        fiction_id = self.parse_story_id(url_or_id)
        fiction_url = f"{RR_BASE}/fiction/{fiction_id}"

        logger.info("Fetching Royal Road fiction %s...", fiction_id)
        html = self._fetch(fiction_url)
        soup = BeautifulSoup(html, "lxml")

        meta = self._parse_metadata(soup)
        chapter_list = self._parse_chapter_list(soup)
        if not chapter_list:
            raise StoryNotFoundError(
                f"No chapters found on Royal Road fiction {fiction_id}."
            )

        # Derive story publish/update dates from the per-chapter
        # timestamps RR exposes in <time unixtime="…">. The fiction
        # header doesn't expose them directly.
        #
        # Row order ≠ timestamp order: authors who insert an omake or
        # bonus chapter out-of-sequence (e.g. a 2024 "Chapter Ω1"
        # slotted next to 2019's Chapter 4) leave the last table row
        # at an older timestamp than a middle row. Taking min/max
        # rather than first/last keeps date_updated correct when that
        # happens, and it's cheap.
        chapter_times = [
            c["unixtime"] for c in chapter_list if c.get("unixtime")
        ]
        if chapter_times:
            meta.setdefault("extra", {})["date_published"] = min(chapter_times)
            meta["extra"]["date_updated"] = max(chapter_times)

        self._save_meta_cache(fiction_id, {**meta, "num_chapters": len(chapter_list)})

        story = Story(
            id=fiction_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=fiction_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        story.chapters.extend(self._materialise_chapters(
            story_id=fiction_id,
            chapter_list=chapter_list,
            skip_chapters=skip_chapters,
            chapter_spec=chapters,
            parse_chapter=self._parse_chapter_html,
            progress_callback=progress_callback,
        ))
        return story
