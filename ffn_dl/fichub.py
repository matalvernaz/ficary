"""FicHub fast-path backend for fanfiction.net.

FFN throttles direct scraping hard: :class:`~ffn_dl.scraper.FFNScraper`
holds a steady ~6s/chapter delay (matching FanFicFare's conservative
default) to stay under Cloudflare's bot wall, so a long fic costs
minutes of wall-clock no matter how the fetch loop is tuned. The delay
is deliberate — the limit is behavioural (request speed + volume +
fingerprint), not a quota we can out-clever.

`FicHub <https://fichub.net>`_ is the community's answer to that wall:
it fetches each story from the source a single time, globally, caches
the result, and serves a pre-built export to everyone. Pulling a
122-chapter fic from FicHub is one ~2s request instead of ~12 minutes
of rate-limited per-chapter fetches — the politeness cost is paid once,
by FicHub, on behalf of every reader.

This module queries FicHub's unstable ``/api/v0/epub`` endpoint,
downloads the EPUB it hands back, and re-ingests it into ffn-dl's own
:class:`~ffn_dl.models.Story` / :class:`~ffn_dl.models.Chapter` model so
the rest of the pipeline (every export format, ``--strip-notes``, the
metadata header, the audiobook builder, library autosort) runs
identically to a direct scrape.

Tradeoffs the caller must respect:
  * **Staleness.** FicHub's copy can lag the source — its background
    refresh catches new chapters "eventually", so update-mode and any
    deliberate "fresh copy" pull must NOT route through here. The
    fast-path is for first-time downloads of fics that already exist.
  * **Soft dependency.** Every entry point degrades to a ``None``
    return on any failure (cache miss, network error, parse failure)
    so the caller falls straight back to a direct scrape.
  * **Fidelity.** Re-ingested bodies are FicHub's HTML, not FFN's raw
    DOM — faithful, but not byte-identical to a direct scrape.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import warnings
from typing import Callable, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .models import Chapter, Story, chapter_in_spec

logger = logging.getLogger(__name__)

FICHUB_BASE = "https://fichub.net"
FICHUB_API_EPUB = FICHUB_BASE + "/api/v0/epub"

API_TIMEOUT_S = 30
"""Timeout for the metadata API call. FicHub answers an already-cached
fic in ~1-3s; a cold fic (FicHub itself scraping the source for the
first time) takes longer, but past this we'd rather fall back to a
direct scrape than hang the caller."""

EPUB_DOWNLOAD_TIMEOUT_S = 120
"""Timeout for the EPUB download. Multi-hundred-chapter fics produce
multi-MB EPUBs; 120s covers a slow link without hanging forever."""

# FicHub names each chapter document ``chap_<N>.xhtml`` in the EPUB.
# The filename ordinal is the authoritative chapter number (the visible
# title lives in the document's first heading); ``introduction.xhtml``
# and ``nav.xhtml`` are chrome and deliberately don't match.
_CHAP_NAME_RE = re.compile(r"chap_(\d+)\.xhtml$", re.IGNORECASE)

# curl_cffi's RequestsError is the base for ConnectionError/Timeout and
# subclasses OSError; catching it covers every transport failure.
_NETWORK_ERRORS = (curl_requests.errors.RequestsError, OSError)


def _new_session():
    """A curl_cffi session impersonating Chrome.

    FicHub doesn't gate its API behind a challenge, but a real browser
    fingerprint is the polite, least-surprising default and matches how
    the rest of the project talks to the web.
    """
    return curl_requests.Session(impersonate="chrome")


def query_meta(
    url: str, *, session=None, timeout: float = API_TIMEOUT_S
) -> Optional[dict]:
    """Query FicHub's v0 API for ``url`` and return the parsed JSON.

    Returns ``None`` — never raises — when FicHub has no export for the
    URL, the request fails, or the response isn't the expected JSON
    shape. The caller treats every ``None`` as "fall back to a direct
    scrape".
    """
    sess = session or _new_session()
    try:
        resp = sess.get(
            FICHUB_API_EPUB,
            params={"q": url},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except _NETWORK_ERRORS as exc:
        logger.info("FicHub API request failed for %s: %s", url, exc)
        return None

    if resp.status_code != 200:
        logger.info(
            "FicHub API returned HTTP %d for %s", resp.status_code, url
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.info("FicHub API returned non-JSON for %s", url)
        return None

    # ``err`` is 0 on success; a truthy ``err`` means FicHub couldn't
    # build an export for this URL (unsupported site, dead link, etc.).
    if not isinstance(data, dict) or data.get("err"):
        logger.info(
            "FicHub reported no export for %s (err=%r)",
            url, (data.get("err") if isinstance(data, dict) else "?"),
        )
        return None
    return data


def _download_epub(
    epub_url_path: str, *, session=None, timeout: float = EPUB_DOWNLOAD_TIMEOUT_S
) -> Optional[bytes]:
    """Download a FicHub EPUB given the ``urls.epub`` path from the API.

    ``epub_url_path`` is the site-relative path FicHub returns (e.g.
    ``/cache/epub/<id>/<slug>.epub?h=<hash>``); an absolute URL is also
    accepted. Returns the raw bytes, or ``None`` on any failure.
    """
    full = (
        epub_url_path
        if epub_url_path.startswith("http")
        else FICHUB_BASE + epub_url_path
    )
    sess = session or _new_session()
    try:
        resp = sess.get(full, timeout=timeout)
    except _NETWORK_ERRORS as exc:
        logger.info("FicHub EPUB download failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.info("FicHub EPUB download returned HTTP %d", resp.status_code)
        return None
    return resp.content


def _to_epoch(value) -> Optional[int]:
    """Coerce a FicHub epoch-seconds field (str or int) to a positive int."""
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return None
    return epoch if epoch > 0 else None


def _plain_text(html: Optional[str]) -> str:
    """Flatten a FicHub HTML fragment (e.g. the description) to text.

    FicHub returns the summary as ``<p>...</p>`` HTML, but a directly
    scraped FFN ``Story.summary`` is plain text — match that so the
    metadata header renders identically.
    """
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _build_metadata(meta: dict) -> dict:
    """Map a FicHub ``meta`` block to the ``Story.metadata`` keys the
    exporters and metadata-header builder read.

    Prefers ``rawExtendedMeta`` (the verbatim FFN fields — pre-formatted
    counts like ``"661,619"``, the FFN-worded status, character list)
    so a FicHub-sourced story renders the same header as a direct
    scrape. Falls back to the normalised top-level fields when the raw
    block is absent.
    """
    raw = meta.get("rawExtendedMeta") or {}
    extra: dict = {}

    def put(key, *candidates):
        for value in candidates:
            if value not in (None, ""):
                extra[key] = value
                return

    put("words", raw.get("words"), meta.get("words"))
    put("chapters", raw.get("chapters"), meta.get("chapters"))
    # FFN's own wording is "Complete"; FicHub's top-level status is the
    # lowercase "complete"/"ongoing". Prefer the raw FFN string.
    put("status", raw.get("status"), meta.get("status"))
    put("rating", raw.get("rated"))
    put("language", raw.get("language"))
    put("genre", raw.get("genres"))
    put("characters", raw.get("characters"))
    put("category", raw.get("raw_fandom"), raw.get("fandom"))
    put("reviews", raw.get("reviews"))
    put("favs", raw.get("favorites"))
    put("follows", raw.get("follows"))

    published = _to_epoch(raw.get("published"))
    updated = _to_epoch(raw.get("updated"))
    if published is not None:
        extra["date_published"] = published
    if updated is not None:
        extra["date_updated"] = updated
    return extra


def _split_chapter(content: bytes) -> tuple[str, str]:
    """Split a FicHub chapter document into ``(title, body_html)``.

    FicHub lays each ``chap_<N>.xhtml`` out as ``<h1>/<h2>`` title
    followed by the chapter body. ffn-dl's exporters render their own
    chapter heading via :func:`models.format_chapter_heading`, and a
    directly scraped FFN chapter's ``.html`` is the bare body
    (``#storytext`` inner HTML, no heading) — so strip FicHub's heading
    and return the remaining body to keep the contract identical.
    """
    # The documents are XHTML; the html parser handles them fine and
    # avoids a hard lxml-xml dependency. Silence bs4's cosmetic
    # "XML parsed as HTML" warning — it doesn't affect extraction.
    with warnings.catch_warnings():
        from bs4 import XMLParsedAsHTMLWarning
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(content, "lxml")
    body = soup.find("body") or soup
    heading = body.find(["h1", "h2", "h3"])
    title = heading.get_text(strip=True) if heading else ""
    if heading is not None:
        heading.decompose()
    return title, body.decode_contents().strip()


def _story_from_epub(
    epub_bytes: bytes,
    *,
    ffn_url: str,
    meta: dict,
    chapters_spec=None,
    progress_callback: Optional[Callable[[int, int, str, bool], None]] = None,
) -> Optional[Story]:
    """Parse a FicHub EPUB into a :class:`Story`.

    ``chapters_spec`` is an optional parsed chapter-spec (see
    :func:`models.parse_chapter_spec`) restricting which chapters are
    kept — applied here because FicHub only serves whole fics. Returns
    ``None`` if the EPUB can't be parsed or contains no chapter
    documents.
    """
    try:
        from ebooklib import ITEM_DOCUMENT, epub as epublib
    except ImportError:
        # ebooklib is the optional [epub] extra. FicHub hands back an
        # EPUB regardless of the user's chosen output format, so without
        # it the fast-path simply isn't available — degrade to a direct
        # scrape rather than crash a (e.g.) ``--fichub -f txt`` run.
        logger.info(
            "FicHub fast-path needs ebooklib (pip install 'ffn-dl[epub]'); "
            "falling back to a direct scrape."
        )
        return None

    # ebooklib reads from a filesystem path, not bytes. Stage to a temp
    # file with delete=False + explicit unlink so it also works on
    # Windows (where a still-open NamedTemporaryFile can't be reopened).
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".epub", delete=False
        ) as handle:
            handle.write(epub_bytes)
            tmp_path = handle.name
        try:
            book = epublib.read_epub(tmp_path)
        except Exception:
            # ebooklib's read path has a broad failure surface
            # (EpubException, zipfile.BadZipFile, KeyError on malformed
            # manifests). A bad download must fall back to scraping,
            # never crash the caller.
            logger.exception("FicHub EPUB parse failed for %s", ffn_url)
            return None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    chapters: list[Chapter] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        match = _CHAP_NAME_RE.search(item.get_name() or "")
        if not match:
            continue
        number = int(match.group(1))
        if chapters_spec is not None and not chapter_in_spec(
            number, chapters_spec
        ):
            continue
        title, body_html = _split_chapter(item.get_content())
        chapters.append(Chapter(number=number, title=title, html=body_html))

    if not chapters:
        logger.info("FicHub EPUB had no chapter documents for %s", ffn_url)
        return None
    chapters.sort(key=lambda chapter: chapter.number)

    story = Story(
        id=_ffn_story_id(ffn_url, meta),
        title=(meta.get("title") or _epub_title(book) or "Unknown Title"),
        author=(meta.get("author") or "Unknown Author"),
        summary=_plain_text(meta.get("description")),
        url=(meta.get("source") or ffn_url),
        author_url=(meta.get("authorUrl") or ""),
        chapters=chapters,
        metadata=_build_metadata(meta),
    )

    if progress_callback:
        total = len(chapters)
        for index, chapter in enumerate(chapters, start=1):
            progress_callback(chapter.number, total, chapter.title, False)
    return story


def _epub_title(book) -> str:
    """Best-effort EPUB ``dc:title`` fallback when the API meta lacks one."""
    try:
        entries = book.get_metadata("DC", "title")
    except Exception:
        return ""
    return entries[0][0] if entries else ""


def _ffn_story_id(ffn_url: str, meta: dict) -> int:
    """Recover the numeric FFN story id from the meta block or the URL.

    FicHub's ``rawExtendedMeta.id`` is the FFN story id as a string;
    fall back to parsing it out of the source/query URL. Returns 0 if
    neither yields a number — the id is only used for cache keys and
    output naming, so a missing one degrades gracefully rather than
    raising.
    """
    raw = meta.get("rawExtendedMeta") or {}
    candidate = raw.get("id")
    try:
        return int(candidate)
    except (TypeError, ValueError):
        pass
    match = re.search(r"/s/(\d+)", meta.get("source") or ffn_url or "")
    return int(match.group(1)) if match else 0


def fetch_story(
    url: str,
    *,
    chapters=None,
    session=None,
    progress_callback: Optional[Callable[[int, int, str, bool], None]] = None,
) -> Optional[Story]:
    """Build a :class:`Story` for an FFN ``url`` from FicHub's cache.

    Returns a fully-populated story on success, or ``None`` if FicHub
    has no export, the download/parse fails, or anything else goes
    wrong — every failure path is non-fatal so the caller falls back to
    a direct scrape. ``chapters`` is an optional parsed chapter-spec
    restricting which chapters are kept.
    """
    sess = session or _new_session()
    data = query_meta(url, session=sess)
    if not data:
        return None

    urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
    epub_path = urls.get("epub") or data.get("epub_url")
    if not epub_path:
        logger.info("FicHub response carried no EPUB URL for %s", url)
        return None

    epub_bytes = _download_epub(epub_path, session=sess)
    if not epub_bytes:
        return None

    story = _story_from_epub(
        epub_bytes,
        ffn_url=url,
        meta=(data.get("meta") or {}),
        chapters_spec=chapters,
        progress_callback=progress_callback,
    )
    if story is not None:
        logger.info(
            "FicHub fast-path: %r by %s (%d chapters) — skipped direct scrape",
            story.title, story.author, len(story.chapters),
        )
    return story
