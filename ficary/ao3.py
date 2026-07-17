"""Archive of Our Own (archiveofourown.org) scraper.

AO3 hosts the full work on one page via view_full_work=true, so we
fetch the whole story in a single HTTP request rather than paging
through chapters like FFN. view_adult=true skips the adult-content
interstitial for Explicit-rated works (we're not logged in, so the
setting persists only for the session).
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story
from .scraper import BaseScraper, CloudflareChallengeError, CookieAuthMixin

logger = logging.getLogger(__name__)

AO3_BASE = "https://archiveofourown.org"

# AO3's adult-content gate and login-required markers appear a bit
# further into the body than Cloudflare's challenge signature, so we
# read a larger prefix here than BaseScraper's default.
_BLOCK_CHECK_PREFIX_BYTES = 4000

# Hard ceiling on multi-page list walks (series page, user works page,
# tag-work list, …). 200 pages × 20 works = 4 000 works which is more
# than any sane series or author has, but keeps a misbehaving site —
# pagination param ignored, ``rel="next"`` always present, ``seen``
# never grows — from spinning the GUI's busy lock indefinitely.
_AO3_LIST_MAX_PAGES = 200


class AO3LockedError(Exception):
    """Raised when a work requires an AO3 login to view."""


class AO3Scraper(CookieAuthMixin, BaseScraper):
    """Scraper for archiveofourown.org.

    Optional ``session_cookie`` (a logged-in browser ``Cookie:`` header,
    handled by :class:`CookieAuthMixin`) unlocks restricted / Archive-locked
    works and your own private bookmarks / marked-for-later; anonymous
    otherwise.

    Optional ``session_user_agent`` pins the browser User-Agent to match
    the cookie. When AO3 has Cloudflare "shields up" (an interactive
    ``cf-mitigated: challenge``), a ``cf_clearance`` cookie copied from a
    browser only validates if the UA travelling with it matches the one
    that solved the challenge — so pass both together.
    """

    site_name = "ao3"
    _auth_cookie_domain = ".archiveofourown.org"
    # AO3 uses Cloudflare's interactive challenge; clear it in a real
    # browser and fetch the page there (see BaseScraper._browser_fetch)
    # rather than the doomed cookie-replay solver.
    _browser_fetch_challenge = True

    def _fetch_html(self, url):
        """Fetch an AO3 page, falling back to the browser solver when
        Cloudflare serves the interactive challenge and ``--cf-solve`` is
        on. Without the solver the clear ``CloudflareChallengeError`` from
        ``_fetch`` propagates unchanged."""
        try:
            return self._fetch(url)
        except CloudflareChallengeError:
            if self.cf_solve:
                return self._browser_fetch(url)
            raise

    def __init__(self, session_cookie: str = "", session_user_agent: str = "",
                 **kwargs):
        # AO3 fetches the whole work in a single request, so the inter-
        # chapter delay barely matters. AIMD defaults (floor 0) let us
        # back off only if AO3 ever actually pushes back.
        super().__init__(
            session_cookie=session_cookie,
            session_user_agent=session_user_agent,
            **kwargs,
        )

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"archiveofourown\.org/works/(\d+)", text)
        if match:
            return int(match.group(1))
        # Also accept the mirror hostname used in some links
        match = re.search(r"ao3\.org/works/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse AO3 work ID from: {text!r}\n"
            "Expected a URL like https://archiveofourown.org/works/12345 "
            "or a numeric ID."
        )

    def _check_for_blocks(self, html):
        super()._check_for_blocks(html)
        lower = html[:_BLOCK_CHECK_PREFIX_BYTES].lower()
        if "this work could have adult content" in lower and "proceed" in lower:
            # Should not happen when view_adult=true is set, but guard anyway
            raise AO3LockedError(
                "Adult-content gate was not bypassed. Try again — AO3 may "
                "require the view_adult=true parameter to be on the URL."
            )
        if "users must be logged in to access" in lower or (
            "sorry, you don't have permission" in lower
        ):
            if self.has_auth:
                raise AO3LockedError(
                    "This work is still inaccessible with the supplied AO3 "
                    "cookie — it may have expired, or the work is hidden from "
                    "your account. Re-copy the Cookie header from a "
                    "logged-in AO3 session."
                )
            raise AO3LockedError(
                "This work requires an AO3 login to view. Pass a logged-in "
                "browser Cookie header via --ao3-cookie (or the "
                "FICARY_AO3_COOKIE env var, or the GUI's AO3 cookie field)."
            )

    @staticmethod
    def is_author_url(url):
        """Return True if the URL is an AO3 user (author) page."""
        return bool(re.search(r"archiveofourown\.org/users/[\w.-]+", str(url)))

    @staticmethod
    def is_series_url(url):
        """Return True if the URL is an AO3 series page."""
        return bool(
            re.search(r"archiveofourown\.org/series/\d+", str(url))
        )

    @staticmethod
    def is_search_url(url):
        """Return True if the URL is an AO3 work search page.

        Matches both the front-end form (``/works/search?work_search[...]``)
        and the AJAX-y short form (``/works?work_search[...]``).
        Filtered tag pages also surface as ``/tags/<name>/works`` —
        ``is_tag_url`` covers those.
        """
        text = str(url)
        return bool(
            re.search(
                r"archiveofourown\.org/(?:works/search\?|works\?[^/]*work_search)",
                text,
            )
        )

    @staticmethod
    def is_tag_url(url):
        """Return True if the URL is an AO3 tag works listing.

        Examples: ``/tags/Harry%20Potter/works``,
        ``/tags/<id>/works``. The trailing ``/works`` segment is
        what distinguishes a works listing from a tag info page.
        """
        return bool(
            re.search(
                r"archiveofourown\.org/tags/[^/]+/works(?:[/?]|$)",
                str(url),
            )
        )

    @staticmethod
    def _parse_metadata(soup):
        """Extract title, author, summary, and stats from a work page."""
        title_tag = soup.find("h2", class_="title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        byline = soup.find("h3", class_="byline")
        author = "Unknown Author"
        author_url = ""
        if byline:
            # Co-authored works carry one ``/users/<name>/pseuds/<pseud>``
            # link per author. Picking only the first via ``find`` would
            # silently drop the remaining authors. Collect all and join
            # with " & " so the metadata reflects the real attribution;
            # author_url stays pointed at the first author's pseud.
            author_links = byline.find_all(
                "a", href=re.compile(r"^/users/[^/]+/pseuds/")
            )
            if author_links:
                names = [a.get_text(strip=True) for a in author_links if a.get_text(strip=True)]
                if names:
                    author = " & ".join(names)
                    author_url = AO3_BASE + author_links[0]["href"]
            else:
                # Anonymous or orphan_account works
                author = byline.get_text(strip=True) or "Anonymous"

        summary_tag = None
        # AO3 summary lives inside a preface module: div.summary.module > h3 + blockquote.userstuff
        preface = soup.find("div", class_="preface") or soup
        summary_mod = preface.find("div", class_="summary")
        if summary_mod:
            bq = summary_mod.find("blockquote")
            summary_tag = bq or summary_mod
        # Use a newline separator so AO3 multi-paragraph summaries
        # (``<p>One</p><p>Two</p>``) don't collapse into ``"OneTwo"``.
        summary = summary_tag.get_text("\n", strip=True) if summary_tag else ""

        extra = {}
        meta_dl = soup.select_one("dl.work.meta")
        if meta_dl:
            def dd_text(cls):
                dd = meta_dl.find("dd", class_=cls)
                return dd.get_text(" ", strip=True) if dd else None

            def dd_tags(cls):
                dd = meta_dl.find("dd", class_=cls)
                if not dd:
                    return []
                return [a.get_text(strip=True) for a in dd.find_all("a")]

            fandoms = dd_tags("fandom")
            if fandoms:
                extra["category"] = " / ".join(fandoms)
            rating = dd_text("rating")
            if rating:
                extra["rating"] = rating
            language = dd_text("language")
            if language:
                extra["language"] = language
            relationships = dd_tags("relationship")
            if relationships:
                extra["relationships"] = ", ".join(relationships)
            characters = dd_tags("character")
            if characters:
                extra["characters"] = ", ".join(characters)
            freeform = dd_tags("freeform")
            if freeform:
                extra["tags"] = ", ".join(freeform)
            warnings = dd_tags("warning")
            if warnings:
                extra["warnings"] = ", ".join(warnings)
            categories = dd_tags("category")
            if categories:
                extra["pairing_category"] = ", ".join(categories)

        stats = soup.find("dl", class_="stats")
        if stats:
            for dt, dd in zip(stats.find_all("dt"), stats.find_all("dd")):
                key = dt.get_text(strip=True).rstrip(":").lower().replace(" ", "_")
                val = dd.get_text(strip=True)
                if key == "words":
                    extra["words"] = val
                elif key == "chapters":
                    extra["chapter_ratio"] = val  # e.g. "5/10" or "5/?"
                elif key == "published":
                    extra["published"] = val
                elif key in ("completed", "updated"):
                    extra["date_updated_text"] = val
                elif key == "kudos":
                    extra["kudos"] = val
                elif key == "hits":
                    extra["hits"] = val
                elif key == "bookmarks":
                    extra["bookmarks"] = val
                elif key == "comments":
                    extra["comments"] = val

        # Derive "status" from the chapter ratio (5/5 = complete, 5/? = WIP)
        ratio = extra.get("chapter_ratio", "")
        if ratio:
            parts = ratio.split("/")
            if len(parts) == 2 and parts[0] == parts[1]:
                extra["status"] = "Complete"
            else:
                extra["status"] = "In-Progress"

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _strip_landmarks(node):
        """Drop AO3's screen-reader landmark headings so they don't leak
        into the chapter body. AO3 sticks ``<h3 class="landmark heading">
        Chapter Text</h3>`` (and the matching "Notes" landmark) into the
        markup as accessibility hints; in an EPUB those just look like
        random in-line headings."""
        for landmark in node.find_all("h3", class_="landmark heading"):
            landmark.decompose()

    @classmethod
    def _chapter_extras_html(cls, wrapper, *, kind):
        """Return rendered HTML for a chapter ``summary`` / ``notes`` /
        ``end notes`` block, or empty string if absent. ``wrapper`` is
        the chapter ``<div>`` for multi-chapter works or the surrounding
        ``#workskin`` for oneshots.

        AO3 puts chapter notes in sibling modules outside the
        ``div.userstuff`` body. Dropping them silently loses content
        that authors frequently use for warnings, glossaries, or
        translation footnotes.
        """
        # AO3 markup: <div id="summary" class="summary module"><h3>Summary</h3><blockquote class="userstuff">...</blockquote></div>
        # Same shape for #notes (pre-chapter notes) and .end.notes.module.
        if kind == "summary":
            block = wrapper.find("div", id=re.compile(r"^summary"))
        elif kind == "notes":
            block = wrapper.find("div", id=re.compile(r"^notes"))
        elif kind == "end_notes":
            block = wrapper.find("div", class_=re.compile(r"\bend\b.*\bnotes\b"))
        else:
            return ""
        if not block:
            return ""
        bq = block.find("blockquote", class_=re.compile(r"\buserstuff\b"))
        if not bq:
            return ""
        cls._strip_landmarks(bq)
        rendered = bq.decode_contents().strip()
        if not rendered:
            return ""
        label = {
            "summary": "Chapter Summary",
            "notes": "Notes",
            "end_notes": "End Notes",
        }[kind]
        return (
            f'<aside class="ao3-chapter-{kind.replace("_", "-")}">'
            f"<h4>{label}</h4>{rendered}</aside>"
        )

    @classmethod
    def _parse_chapters(cls, soup, fallback_title):
        """Extract chapters from the full-work page.

        Multi-chapter works have <div id="chapter-N"> blocks with an
        inner h3.title, optional summary/notes modules, the
        div.userstuff body, and a trailing end-notes module. Single-
        chapter works have those modules under #workskin with no
        per-chapter wrapper.

        Chapter summaries, pre-chapter notes, and end-of-chapter notes
        are kept and rendered as ``<aside>`` blocks alongside the body
        so authors' commentary isn't silently dropped from the EPUB.
        """
        chapters = []
        numbered = soup.find_all(
            "div", id=re.compile(r"^chapter-\d+$")
        )

        if numbered:
            for idx, div in enumerate(numbered, 1):
                title_tag = div.find("h3", class_="title")
                title = (
                    title_tag.get_text(strip=True) if title_tag
                    else f"Chapter {idx}"
                )
                title = re.sub(r"^Chapter\s+\d+\s*:\s*", "", title).strip() or f"Chapter {idx}"
                userstuff = div.find("div", class_="userstuff")
                if userstuff is None:
                    continue
                cls._strip_landmarks(userstuff)
                body = userstuff.decode_contents()
                summary_html = cls._chapter_extras_html(div, kind="summary")
                notes_html = cls._chapter_extras_html(div, kind="notes")
                end_notes_html = cls._chapter_extras_html(div, kind="end_notes")
                html = summary_html + notes_html + body + end_notes_html
                chapters.append(Chapter(number=idx, title=title, html=html))
        else:
            # Single-chapter work
            workskin = soup.find(id="workskin")
            userstuff = workskin.find("div", class_="userstuff") if workskin else None
            if userstuff is None:
                raise ValueError(
                    "Could not locate chapter content on AO3 work page."
                )
            cls._strip_landmarks(userstuff)
            body = userstuff.decode_contents()
            # Oneshot notes/summaries live in the preface module rather
            # than #workskin, so look one level up.
            preface = soup.find("div", class_="preface") or workskin
            notes_html = cls._chapter_extras_html(preface, kind="notes")
            end_notes_html = cls._chapter_extras_html(soup, kind="end_notes")
            html = notes_html + body + end_notes_html
            chapters.append(
                Chapter(number=1, title=fallback_title, html=html)
            )

        return chapters

    @staticmethod
    def _parse_chapter_count_from_stats(soup):
        """Extract the 'posted/planned' chapter count from dl.stats."""
        stats = soup.select_one("dl.stats")
        if not stats:
            return None
        chapters_dd = stats.find("dd", class_="chapters")
        if not chapters_dd:
            return None
        ratio = chapters_dd.get_text(strip=True)  # "4/4" or "5/?"
        match = re.match(r"(\d+)", ratio)
        return int(match.group(1)) if match else None

    def get_chapter_count(self, url_or_id):
        work_id = self.parse_story_id(url_or_id)
        # Bare work URL (no view_full_work) loads only chapter 1 + stats —
        # much cheaper than pulling the whole fic to check whether it grew.
        html = self._fetch_html(f"{AO3_BASE}/works/{work_id}?view_adult=true")
        soup = BeautifulSoup(html, "lxml")
        count = self._parse_chapter_count_from_stats(soup)
        if count is None:
            raise ValueError(
                f"Could not determine chapter count for AO3 work {work_id}."
            )
        return count

    def scrape_series_works(self, url):
        """Fetch an AO3 series page and return (series_name, [work_urls]).

        Works are returned in the order the author set for the series.
        AO3 paginates series at 20 works per page via ``?page=N``;
        walking `rel="next"` picks up the full list — without pagination,
        a series of 30 works would silently drop the last 10.

        Accepts both ``archiveofourown.org/series/<id>`` and the
        ``ao3.org`` mirror — ``parse_story_id`` already does, so the
        series-walk path stays consistent with the work-id path.
        """
        match = re.search(r"(?:archiveofourown\.org|ao3\.org)/series/(\d+)", url)
        if not match:
            raise ValueError(f"Not an AO3 series URL: {url}")
        series_id = match.group(1)

        series_name = "Unknown Series"
        seen = set()
        work_urls = []
        page = 1
        while page <= _AO3_LIST_MAX_PAGES:
            page_url = f"{AO3_BASE}/series/{series_id}?page={page}"
            html = self._fetch(page_url)
            soup = BeautifulSoup(html, "lxml")

            if page == 1:
                h2 = soup.find("h2", class_="heading")
                if h2:
                    name = h2.get_text(strip=True)
                    if name:
                        series_name = name

            new_on_page = 0
            # The series page lists works as h4.heading blocks whose
            # first link points at /works/<id>. Scoping to h4.heading
            # excludes the sidebar's "Bookmarks" / "Comments" links,
            # which also target /works/.
            for heading in soup.find_all("h4", class_="heading"):
                link = heading.find("a", href=re.compile(r"^/works/\d+"))
                if not link:
                    continue
                wid_m = re.search(r"/works/(\d+)", link["href"])
                if not wid_m:
                    continue
                wid = wid_m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                work_urls.append(f"{AO3_BASE}/works/{wid}")
                new_on_page += 1

            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()
        else:
            logger.warning(
                "AO3 series %s walk hit the %d-page safety cap; the "
                "work list may be incomplete. This usually indicates "
                "AO3 returned the same page repeatedly or pagination "
                "is broken.", series_id, _AO3_LIST_MAX_PAGES,
            )

        return series_name, work_urls

    def scrape_author_stories(self, url):
        """Fetch an AO3 user works page (all pages) and return
        (author_name, [story_urls]).
        """
        # Normalise: accept /users/Name, /users/Name/works, /users/Name/pseuds/X
        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 user URL: {url}")
        user = match.group(1)
        author_name = user
        story_urls = []
        seen = set()
        page = 1

        while page <= _AO3_LIST_MAX_PAGES:
            page_url = f"{AO3_BASE}/users/{user}/works?page={page}"
            html = self._fetch(page_url)
            soup = BeautifulSoup(html, "lxml")

            if page == 1:
                # Author display name from the heading, if present
                h2 = soup.find("h2", class_="heading")
                if h2:
                    heading = h2.get_text(strip=True)
                    m = re.search(r"Works by (.+)$", heading)
                    if m:
                        author_name = m.group(1).strip()

            new_on_page = 0
            for a in soup.find_all(
                "a", href=re.compile(r"^/works/\d+(?:[?#].*)?$")
            ):
                wid_m = re.search(r"/works/(\d+)", a["href"])
                if not wid_m:
                    continue
                wid = wid_m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                story_urls.append(f"{AO3_BASE}/works/{wid}")
                new_on_page += 1

            # Stop when we've paginated past the end
            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()
        else:
            logger.warning(
                "AO3 user %s works walk hit the %d-page safety cap; "
                "the story list may be incomplete.",
                user, _AO3_LIST_MAX_PAGES,
            )

        return author_name, story_urls

    def scrape_author_works(self, url, max_results=None, cancel_event=None):
        """Return (author_name, [work_dict]) from an AO3 user's works page.

        AO3 works-list blurbs match the shape emitted by the search
        parser, so we reuse `_parse_ao3_results` — that gives us title,
        summary, word count, chapter count, rating, status, fandom, and
        series membership for each work, with no extra HTTP calls.
        """

        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 user URL: {url}")
        user = match.group(1)
        return self._scrape_ao3_work_list(
            f"{AO3_BASE}/users/{user}/works", fallback_name=user,
            max_results=max_results, cancel_event=cancel_event,
        )

    def scrape_bookmark_works(self, url, max_results=None, cancel_event=None):
        """Return (owner_name, [work_dict]) from an AO3 user's public
        bookmarks page. Same blurb shape as works — reuse the parser.
        """
        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 bookmarks URL: {url}")
        user = match.group(1)
        return self._scrape_ao3_work_list(
            f"{AO3_BASE}/users/{user}/bookmarks",
            fallback_name=user,
            section="bookmarks",
            max_results=max_results, cancel_event=cancel_event,
        )

    def scrape_search_works(self, url, max_results=None, cancel_event=None):
        """Return (query_label, [work_dict]) from an AO3 search URL.

        The user's pasted URL is passed through to ``_fetch`` verbatim;
        AO3's search-form URL shape changes shape often enough that
        rebuilding from parsed components would invite drift. We just
        walk pagination and let AO3 redirect if needed.

        ``query_label`` is best-effort — pulled from
        ``work_search[query]`` if present, otherwise the URL's tag
        segment, otherwise an empty string.
        """
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(url)
        params = parse_qs(parts.query)
        label = (
            params.get("work_search[query]", [""])[0]
            or params.get("query", [""])[0]
            or ""
        )
        return self._scrape_ao3_work_list(
            url, fallback_name=label or "Search results", section="search",
            max_results=max_results, cancel_event=cancel_event,
        )

    def scrape_tag_works(self, url, max_results=None, cancel_event=None):
        """Return (tag_label, [work_dict]) from an AO3 tag works listing.

        ``tag_label`` is the ``<name>`` portion of ``/tags/<name>/works``
        URL-decoded — that's what the AO3 page shows in its heading
        too, so picker output stays consistent with what the user
        pasted.
        """
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(url)
        match = re.search(r"/tags/([^/]+)/works", parts.path)
        label = unquote(match.group(1)).replace("*s*", "/") if match else ""
        return self._scrape_ao3_work_list(
            url, fallback_name=label or "Tag works", section="tag",
            max_results=max_results, cancel_event=cancel_event,
        )

    def _scrape_ao3_work_list(
        self,
        base_url,
        *,
        fallback_name,
        section="own",
        max_pages=200,
        max_results=None,
        cancel_event=None,
    ):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        from .search import _parse_ao3_results

        author_name = fallback_name
        works = []
        seen = set()
        page = 1

        # Rebuild the URL through urlsplit so a fragment in the user's
        # input ("…/works#main") doesn't push our ``page=N`` *into* the
        # fragment ("…/works#main?page=1"), and a pre-existing
        # ``page=...`` param in their input gets overwritten instead of
        # duplicated ("…?page=3&page=1"). Both of those degenerate URLs
        # cause the walk to fetch page 1 repeatedly and stop after the
        # first page's worth of works.
        parts = urlsplit(base_url)
        existing_params = [
            (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
            if k != "page"
        ]

        def _page_url(n):
            new_query = urlencode(existing_params + [("page", str(n))])
            # Drop fragment — AO3 doesn't care, and keeping it in the
            # rebuilt URL would just waste bytes.
            return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))

        while page <= max_pages:
            if cancel_event is not None and cancel_event.is_set():
                # Honoured between pagination steps — the Add-from-URL
                # dialog's Cancel used to only suppress the UI callback
                # while the walk ran every page to completion.
                break
            if max_results and len(works) >= max_results:
                break
            page_url = _page_url(page)
            html = self._fetch(page_url)
            if page == 1:
                soup = BeautifulSoup(html, "lxml")
                h2 = soup.find("h2", class_="heading")
                if h2:
                    heading = h2.get_text(" ", strip=True)
                    m = re.search(r"(?:Works|Bookmarks) by (.+)$", heading)
                    if m:
                        author_name = m.group(1).strip()
            page_results = _parse_ao3_results(html)
            new_on_page = 0
            for r in page_results:
                m = re.search(r"/works/(\d+)", r.get("url", ""))
                if not m:
                    continue
                wid = m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                r["section"] = section
                r["updated"] = r.get("updated", "")
                works.append(r)
                new_on_page += 1
            soup = BeautifulSoup(html, "lxml")
            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()
        else:
            # Match scrape_series_works / scrape_author_stories: log when
            # the safety cap is the reason we stopped, so a misbehaving
            # pagination doesn't fail silently.
            logger.warning(
                "AO3 work-list walk for %s hit the %d-page safety cap; "
                "results may be incomplete.", base_url, max_pages,
            )

        return author_name, works

    @staticmethod
    def is_bookmarks_url(url):
        return bool(
            re.search(
                r"(?:archiveofourown\.org|ao3\.org)/users/[\w.-]+/bookmarks",
                str(url),
            )
        )

    @staticmethod
    def is_reading_list_url(url):
        return bool(
            re.search(
                r"(?:archiveofourown\.org|ao3\.org)/users/[\w.-]+/readings",
                str(url),
            )
        )

    def scrape_reading_list_works(self, url, max_results=None, cancel_event=None):
        """Return (owner_name, [work_dict]) from an AO3 reading-history /
        marked-for-later page. Login-only on AO3 (needs --ao3-cookie).
        The pasted URL is walked as-is so ``?show=to-read`` (the
        marked-for-later view) keeps filtering — before this existed, a
        readings URL fell through to ``is_author_url`` and silently
        listed the user's AUTHORED works instead."""
        match = re.search(r"(?:archiveofourown\.org|ao3\.org)/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 readings URL: {url}")
        user = match.group(1)
        return self._scrape_ao3_work_list(
            url, fallback_name=user, section="readings",
            max_results=max_results, cancel_event=cancel_event,
        )

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        """Download an AO3 work. Skip_chapters is honoured after the
        single-page fetch so update mode and caching still work.
        When `chapters` is a list of (lo, hi) ranges (the chapter-spec
        format), only chapters in those ranges are kept.

        Update-mode optimisation: if a cached meta block exists and the
        caller claims they already have `skip_chapters` chapters, probe
        the cheap landing page (chapter 1 + stats) first to get the
        current chapter count. If it hasn't grown, return a Story with
        no chapters (caller's "no new chapters" signal) without paying
        for the full-work download.
        """
        from .models import chapter_in_spec

        # Capture the caller's spec under a different name so the local
        # binding that holds the parsed Chapter list below doesn't
        # shadow it. Same-name locals + params worked by accident before
        # because of the early alias, but the next maintainer to add a
        # branch that read ``chapters`` would silently get the wrong one.
        chapter_spec = chapters
        work_id = self.parse_story_id(url_or_id)
        work_url = f"{AO3_BASE}/works/{work_id}"
        full_url = f"{work_url}?view_adult=true&view_full_work=true"

        cached_meta = self._load_meta_cache(work_id)

        # Cheap probe in update mode
        if skip_chapters > 0 and cached_meta is not None:
            bare_html = self._fetch_html(f"{work_url}?view_adult=true")
            bare_soup = BeautifulSoup(bare_html, "lxml")
            current_count = self._parse_chapter_count_from_stats(bare_soup)
            if current_count is not None and current_count <= skip_chapters:
                # ``.get`` rather than ``[]`` so an older cached meta
                # without one of these fields (older schema, partial
                # write recovered after a crash) still returns a usable
                # Story instead of crashing the update path.
                return Story(
                    id=work_id,
                    title=cached_meta.get("title", ""),
                    author=cached_meta.get("author", ""),
                    summary=cached_meta.get("summary", ""),
                    url=work_url,
                    author_url=cached_meta.get("author_url", ""),
                    metadata=cached_meta.get("extra", {}),
                )

        logger.info("Fetching AO3 work %s...", work_id)
        html = self._fetch_html(full_url)
        fetched_soup = BeautifulSoup(html, "lxml")
        meta = self._parse_metadata(fetched_soup)
        self._save_meta_cache(work_id, meta)

        story = Story(
            id=work_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=work_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        parsed_chapters = self._parse_chapters(fetched_soup, meta["title"])
        total = len(parsed_chapters)

        for ch in parsed_chapters:
            if ch.number <= skip_chapters:
                continue
            if not chapter_in_spec(ch.number, chapter_spec):
                continue
            self._save_chapter_cache(work_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(ch.number, total, ch.title, False)

        return story
