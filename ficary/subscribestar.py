"""SubscribeStar (subscribestar.adult) scraper.

SubscribeStar is a creator-subscription platform, not a fiction archive,
so its shape is unusual for ficary:

* A **creator** (``subscribestar.adult/<handle>``) posts many stories at
  once, each as a stream of numbered ``"<Title> Pt.N"`` posts. The posts
  of every story are interleaved in one reverse-chronological feed.
* A **post** body doesn't hold the prose — it links to a Google Doc that
  does. SubscribeStar wraps the link in an ``/away?url=<base64>``
  redirect, but the clean document URL is kept in the anchor's
  ``data-href`` attribute.

So downloading one story means: enumerate the creator's posts
(authenticated — the feed is subscriber-only), keep the posts whose base
title matches, fetch each part's linked Google Doc, and merge the parts
in order. Enumeration follows the feed's ``infinite_scroll-next_page``
control (``/posts?slug=<handle>&page=N&sort_by=newest``).

Auth is a browser cookie for subscribestar.adult (the
``_subscribestar_session`` cookie), passed as ``session_cookie`` /
``--subscribestar-cookie``. The linked Google Docs are shared by-link and
export as plain HTML without Google auth.

Because a creator hosts many stories, a bare creator URL isn't a single
downloadable work. Two entry points fit ficary's model:

* ``download(post_url)`` — a single ``/posts/<id>`` becomes a one-chapter
  story (that post's Google Doc).
* ``download_creator_story(handle, base_title)`` — the merge path the CLI
  ``--subscribestar-story`` flag drives, returning all matching parts as
  one work.
"""

import html as _html
import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story, chapter_in_spec
from .scraper import BaseScraper, CookieAuthMixin, StoryNotFoundError

logger = logging.getLogger(__name__)

SS_BASE = "https://subscribestar.adult"

_POST_URL_RE = re.compile(r"subscribestar\.adult/posts/(\d+)", re.IGNORECASE)
# Fibaro's posts link two Google shapes: native docs (/document/d/<id>)
# and uploaded Word files in Drive (/file/d/<id>?filetype=msword). Both
# render via the document export endpoint, so accept either id form.
_GDOC_ID_RE = re.compile(r"/(?:document|file)/d/([A-Za-z0-9_-]+)")
_PART_RE = re.compile(r"(?:Pt|Part|Ch|Chapter)\.?\s*(\d+)", re.IGNORECASE)

# Reserved first-path segments that are site chrome, not creator handles —
# so is_author_url doesn't treat subscribestar.adult/feed as a creator.
_RESERVED_SEGMENTS = {
    "posts", "feed", "settings", "notifications", "logout", "login",
    "subscriptions", "features", "pricing", "api", "about", "tos",
    "privacy", "away", "search", "explore", "messages",
}


def _base_title(title: str) -> str:
    """Strip a trailing ``Pt.N`` / ``Part N`` / ``Ch. N`` marker and
    normalise punctuation/case so parts of one story group together even
    when the creator's punctuation drifts between posts."""
    stem = re.sub(
        r"\s*(?:Pt|Part|Ch|Chapter)\.?\s*\d+.*$", "", title, flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-z0-9 ]", "", stem.lower()).strip()


def _part_number(title: str):
    m = _PART_RE.search(title)
    return int(m.group(1)) if m else None


class SubscribeStarScraper(CookieAuthMixin, BaseScraper):
    """Scraper for subscribestar.adult creator posts."""

    site_name = "subscribestar"
    _auth_cookie_domain = ".subscribestar.adult"

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = _POST_URL_RE.search(text)
        if m:
            return int(m.group(1))
        raise ValueError(
            f"Cannot parse a SubscribeStar post id from: {text!r}\n"
            "Expected a post URL like https://subscribestar.adult/posts/12345 "
            "(or use --subscribestar-story with a creator URL for a whole "
            "series)."
        )

    @staticmethod
    def is_author_url(url):
        """True for a creator page (``subscribestar.adult/<handle>``) —
        a single path segment that isn't site chrome or a post."""
        m = re.match(
            r"https?://(?:www\.)?subscribestar\.adult/([^/?#]+)/?$", str(url),
        )
        return bool(m and m.group(1).lower() not in _RESERVED_SEGMENTS)

    @staticmethod
    def _handle_from_url(url: str) -> str:
        m = re.search(r"subscribestar\.adult/([^/?#]+)", str(url))
        if not m or m.group(1).lower() in _RESERVED_SEGMENTS:
            raise ValueError(f"Not a SubscribeStar creator URL: {url!r}")
        return m.group(1)

    # ── Post + doc parsing ─────────────────────────────────────

    @staticmethod
    def _parse_posts(soup) -> list[dict]:
        """Extract ``{id, title, doc_url}`` from every ``div.post`` in a
        page or an AJAX feed fragment. ``doc_url`` is the clean Google Doc
        link from the post link's ``data-href`` (the visible ``href`` is a
        base64 ``/away`` redirect); ``None`` when the post carries no doc
        link."""
        posts = []
        for post in soup.select("div.post"):
            heading = post.select_one(".trix-content h1")
            if not heading:
                continue
            title = heading.get_text(strip=True)
            doc_url = None
            for a in post.select(".trix-content a[data-href]"):
                href = _html.unescape(a.get("data-href") or "")
                if "docs.google.com" in href:
                    doc_url = href
                    break
            posts.append({
                "id": post.get("data-id") or "",
                "title": title,
                "doc_url": doc_url,
            })
        return posts

    @staticmethod
    def _next_page_url(soup):
        nxt = soup.select_one("[data-role='infinite_scroll-next_page']")
        if not nxt or not nxt.get("href"):
            return None
        return SS_BASE + _html.unescape(nxt["href"])

    def _enumerate_posts(self, handle: str) -> list[dict]:
        """Walk the creator's whole feed, newest first, returning every
        titled post. Follows the feed's own next-page control until it's
        gone."""
        url = f"{SS_BASE}/{handle}"
        seen_ids = set()
        out = []
        # A generous page ceiling: a prolific creator's full history is a
        # few hundred posts (~6/page). The loop also stops the moment the
        # feed offers no next page, so this only guards a broken cursor.
        for _ in range(500):
            html = self._fetch(url)
            fragment = html.strip()
            # The paginated endpoint may answer with a JSON envelope
            # ({"html": "..."}) rather than a bare fragment.
            if fragment.startswith("{"):
                import json
                try:
                    fragment = json.loads(fragment).get("html", "")
                except ValueError:
                    pass
            soup = BeautifulSoup(fragment, "lxml")
            batch = self._parse_posts(soup)
            fresh = [p for p in batch if p["id"] not in seen_ids]
            for p in fresh:
                seen_ids.add(p["id"])
            out.extend(fresh)
            nxt = self._next_page_url(soup)
            if not nxt or not batch:
                break
            url = nxt
            self._delay()
        return out

    def _fetch_gdoc_html(self, doc_url: str):
        """Fetch a link-shared Google Doc and return its prose as simple
        ``<p>`` HTML. Returns None if the doc id can't be parsed or the
        export is empty."""
        m = _GDOC_ID_RE.search(doc_url or "")
        if not m:
            return None
        export = f"https://docs.google.com/document/d/{m.group(1)}/export?format=html"
        html = self._fetch(export)
        soup = BeautifulSoup(html, "lxml")
        body = soup.body or soup
        for tag in body.find_all(["style", "script"]):
            tag.decompose()
        paras = []
        for p in body.find_all("p"):
            text = p.get_text(" ", strip=True)
            if not text:
                continue
            # Google docs often open with a bare "Chapter N" line that
            # duplicates the part title we assign; drop it as the leader.
            if not paras and re.fullmatch(r"Chapter\s+\d+", text, re.IGNORECASE):
                continue
            paras.append(f"<p>{text}</p>")
        return "\n".join(paras) if paras else None

    # ── ficary entry points ────────────────────────────────────

    def scrape_author_stories(self, url):
        """List a creator's stories, grouped by base title.

        Returns ``(creator_name, [labels])`` where each label is the
        distinct story base-title. Callers pick one and route it to
        :meth:`download_creator_story`. (The base tit­les are returned as
        the "story urls" slot the author flow expects — they're not URLs,
        but the merge path keys on the title, not a URL.)"""
        handle = self._handle_from_url(url)
        posts = self._enumerate_posts(handle)
        titles = {}
        for p in posts:
            base = _base_title(p["title"])
            if base:
                titles.setdefault(base, p["title"])
        return handle, sorted(titles.values())

    def download(self, url_or_id, progress_callback=None, skip_chapters=0,
                 chapters=None):
        """Download a single post as a one-chapter story."""
        post_id = self.parse_story_id(url_or_id)
        post_url = f"{SS_BASE}/posts/{post_id}"
        html = self._fetch(post_url)
        soup = BeautifulSoup(html, "lxml")
        parsed = self._parse_posts(soup)
        if not parsed:
            raise StoryNotFoundError(
                f"No post content found at {post_url} "
                "(is the SubscribeStar cookie set and the subscription active?)."
            )
        post = parsed[0]
        body = (
            self._fetch_gdoc_html(post["doc_url"])
            if post.get("doc_url") else None
        )
        story = Story(
            id=post_id,
            title=post["title"],
            author="",
            summary="",
            url=post_url,
            chapters=[],
        )
        if body and not skip_chapters and chapter_in_spec(1, chapters):
            story.chapters.append(Chapter(number=1, title=post["title"], html=body))
        return story

    def download_creator_story(self, url_or_handle, base_title, *,
                               progress_callback=None):
        """Enumerate a creator's posts, keep the ones whose base title
        matches ``base_title``, fetch each part's Google Doc, and return a
        single merged :class:`Story` with one chapter per part in part
        order. This is the ``--subscribestar-story`` path."""
        handle = (
            url_or_handle if "/" not in str(url_or_handle)
            else self._handle_from_url(url_or_handle)
        )
        want = _base_title(base_title)
        posts = self._enumerate_posts(handle)
        parts = []
        for p in posts:
            if _base_title(p["title"]) != want:
                continue
            n = _part_number(p["title"])
            if n is not None and p.get("doc_url"):
                parts.append((n, p["title"], p["doc_url"]))
        parts = sorted(set(parts))
        if not parts:
            raise StoryNotFoundError(
                f"No posts matched {base_title!r} for creator {handle!r}."
            )
        display_title = re.sub(
            r"\s*(?:Pt|Part|Ch|Chapter)\.?\s*\d+.*$", "", parts[0][1],
            flags=re.IGNORECASE,
        ).strip() or base_title

        chapters = []
        total = len(parts)
        for i, (n, _title, doc_url) in enumerate(parts, 1):
            body = self._fetch_gdoc_html(doc_url)
            if progress_callback:
                progress_callback(i, total, f"Part {n}", body is None)
            if body:
                chapters.append(Chapter(number=n, title=f"Part {n}", html=body))
            self._delay()
        return Story(
            id=0,
            title=display_title,
            author=handle,
            summary=f"{display_title} by {handle} (SubscribeStar).",
            url=f"{SS_BASE}/{handle}",
            chapters=chapters,
        )
