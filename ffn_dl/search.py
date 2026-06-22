"""Search fanfiction.net, Archive of Our Own, and Royal Road."""

import logging
import random
import re
import time
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup, NavigableString
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

FFN_BASE = "https://www.fanfiction.net"
AO3_BASE = "https://archiveofourown.org"
RR_BASE = "https://www.royalroad.com"
LIT_BASE = "https://www.literotica.com"
LIT_TAGS_BASE = "https://tags.literotica.com"
SEARCH_PATH = "/search/"
DEFAULT_LIMIT = 25


class SearchPage(list):
    """A list of search-result dicts with explicit pagination metadata.

    ``exhausted=True`` signals the upstream archive has no more pages
    for this query; ``exhausted=False`` signals "this page filtered
    down to empty but more pages may still have results" (the
    Wattpad mature/completion-filter case). Callers that iterate the
    list see the same behaviour as a plain list; ``fetch_until_limit``
    reads ``exhausted`` to decide whether to keep paging.

    For backwards compatibility, a plain ``[]`` from a legacy
    ``search_*`` function is treated as ``exhausted=True`` — the
    fetcher can only know the page is "really empty" or "filtered to
    empty" if the search function tells it.
    """

    def __init__(self, iterable=(), *, exhausted: bool = False):
        super().__init__(iterable)
        self.exhausted: bool = bool(exhausted)


def _page_exhausted(page_results) -> bool:
    """Decide whether a search-page result signals upstream exhaustion.

    Plain lists default to ``True`` so legacy search functions retain
    their existing behaviour (break on empty). :class:`SearchPage`
    carries the explicit flag.
    """
    flag = getattr(page_results, "exhausted", None)
    if flag is None:
        return not page_results
    return bool(flag)


# ── Filter option tables ──────────────────────────────────────────
# Keys are the human-readable labels shown in CLI --choices / GUI combos.
# Values are the numeric IDs FFN's search form submits.
# `None` means "no filter" — omit the param entirely.

FFN_RATING = {
    "all": None,
    "K": 1,
    "K+": 2,
    "T": 3,
    "M": 4,
    "K-T": 103,
}

FFN_STATUS = {
    "all": None,
    "in-progress": 1,
    "complete": 2,
}

FFN_GENRE = {
    "any": None,
    "general": 1,
    "romance": 2,
    "humor": 3,
    "drama": 4,
    "poetry": 5,
    "adventure": 6,
    "mystery": 7,
    "horror": 8,
    "parody": 9,
    "angst": 10,
    "supernatural": 11,
    "suspense": 12,
    "sci-fi": 13,
    "fantasy": 14,
    "tragedy": 16,
    "crime": 18,
    "family": 19,
    "hurt/comfort": 20,
    "friendship": 21,
}

FFN_WORDS = {
    "any": None,
    "<1k": 1,
    "<5k": 2,
    "5k+": 3,
    "10k+": 4,
    "30k+": 5,
    "50k+": 6,
    "150k+": 7,
    "300k+": 8,
}

FFN_LANGUAGE = {
    "any": None,
    "english": 1,
    "spanish": 2,
    "french": 3,
    "german": 4,
    "chinese": 5,
    "dutch": 7,
    "portuguese": 8,
    "russian": 10,
    "italian": 11,
    "polish": 13,
    "hungarian": 14,
    "swedish": 17,
    "norwegian": 18,
    "danish": 19,
    "finnish": 20,
    "turkish": 30,
    "czech": 31,
    "indonesian": 32,
    "vietnamese": 37,
}

FFN_CROSSOVER = {
    "any": None,
    "only": 1,
    "exclude": 2,
}

FFN_MATCH = {
    "any": None,
    "title": "title",
    "summary": "summary",
}

FFN_SORT = {
    "best match": None,
    "updated": 1,
    "published": 2,
    "reviews": 3,
    "favorites": 4,
    "follows": 5,
}

# Fandom-page time-range filter (the ``t`` URL param). Global across
# fandoms; verified live 2026-06-22.
FFN_TIME = {
    "any": None,
    "updated 24h": 1,
    "updated 1 week": 2,
    "updated 1 month": 3,
    "updated 6 months": 4,
    "updated 1 year": 5,
    "published 24h": 11,
    "published 1 week": 12,
    "published 1 month": 13,
    "published 6 months": 14,
    "published 1 year": 15,
}

# Fandom pages use the ``len`` param with a DIFFERENT value encoding than
# the keyword-search ``words`` param (FFN_WORDS). Translate the shared
# human labels to the nearest fandom ``len`` bucket. (Before this, fandom
# browse sent ``w=<FFN_WORDS value>`` — wrong param name AND wrong values,
# so the word-length filter silently did nothing on fandom pages.)
_FFN_WORDS_TO_LEN = {
    "<1k": 11,
    "<5k": 51,
    "5k+": 5,
    "10k+": 10,
    "30k+": 20,
    "50k+": 40,
    "150k+": 100,
    "300k+": 100,
}


# ── Fandom-browse (parallel to erotica tag-browse) ───────────────
#
# FFN's category pages (``/book/Harry-Potter/`` etc.) serve story
# listings using the same z-list HTML as the keyword search endpoint,
# so the existing :func:`_parse_results` can be re-used. The URL
# parameter names differ — the fandom-page form uses short keys
# (``r``, ``srt``, ``g1``, ``w``, ``s``, ``lan``, ``p``) where the
# search endpoint uses ``censorid``, ``sortid``, ``genreid``, etc.

FFN_CATEGORIES = {
    "any": None,
    "book": "book",
    "anime": "anime",
    "movie": "movie",
    "tv": "tv",
    "game": "game",
    "cartoon": "cartoon",
    "comic": "comic",
    "play": "play",
    "misc": "misc",
}
"""URL prefix per FFN category. ``any`` triggers auto-detect (the
slug is tried against each category until a 200-with-results comes
back). The curated :data:`FFN_TOP_FANDOMS` entries below pin a
category for the popular cases so auto-detect doesn't have to
brute-force on every Harry Potter / Naruto / etc. search."""


FFN_TOP_FANDOMS: list[tuple[str, str, str]] = [
    # (display_label, category, url_slug). Curated from FFN's "Just
    # In" + top-fandom pages — ASCII-only slugs to avoid URL-encoding
    # surprises. Power users can type any fandom name into the
    # Fandom text field; the curated list just gives the multi-picker
    # something to populate without making users guess the slug.
    # ── Books ──
    ("Harry Potter", "book", "Harry-Potter"),
    ("Lord of the Rings", "book", "Lord-of-the-Rings"),
    ("Percy Jackson and the Olympians", "book", "Percy-Jackson-and-the-Olympians"),
    ("Heroes of Olympus", "book", "Heroes-of-Olympus"),
    ("Twilight", "book", "Twilight"),
    ("Hunger Games", "book", "Hunger-Games"),
    ("Warriors", "book", "Warriors"),
    ("Maximum Ride", "book", "Maximum-Ride"),
    ("Mortal Instruments", "book", "Mortal-Instruments"),
    ("Hobbit", "book", "Hobbit"),
    ("Outsiders", "book", "Outsiders"),
    ("Lightning Thief", "book", "Lightning-Thief"),
    # ── Anime / Manga ──
    ("Naruto", "anime", "Naruto"),
    ("Bleach", "anime", "Bleach"),
    ("Inuyasha", "anime", "Inuyasha"),
    ("Fullmetal Alchemist", "anime", "Fullmetal-Alchemist"),
    ("Hetalia - Axis Powers", "anime", "Hetalia-Axis-Powers"),
    ("Death Note", "anime", "Death-Note"),
    ("Fairy Tail", "anime", "Fairy-Tail"),
    ("One Piece", "anime", "One-Piece"),
    ("Dragon Ball Z", "anime", "Dragon-Ball-Z"),
    ("Sword Art Online", "anime", "Sword-Art-Online"),
    ("My Hero Academia", "anime", "My-Hero-Academia"),
    ("Attack on Titan", "anime", "Attack-on-Titan"),
    ("RWBY", "anime", "RWBY"),
    ("Code Geass", "anime", "Code-Geass"),
    ("Yu-Gi-Oh", "anime", "Yu-Gi-Oh"),
    # ── Movies ──
    ("Star Wars", "movie", "Star-Wars"),
    ("Marvel", "movie", "Marvel"),
    ("Pirates of the Caribbean", "movie", "Pirates-of-the-Caribbean"),
    ("Disney", "movie", "Disney"),
    ("Lion King", "movie", "Lion-King"),
    ("Frozen", "movie", "Frozen"),
    # ── TV ──
    ("Supernatural", "tv", "Supernatural"),
    ("Glee", "tv", "Glee"),
    ("Buffy The Vampire Slayer", "tv", "Buffy-The-Vampire-Slayer"),
    ("Sherlock", "tv", "Sherlock"),
    ("Doctor Who", "tv", "Doctor-Who"),
    ("NCIS", "tv", "NCIS"),
    ("Once Upon a Time", "tv", "Once-Upon-a-Time"),
    ("Walking Dead", "tv", "Walking-Dead"),
    ("Vampire Diaries", "tv", "Vampire-Diaries"),
    ("Stranger Things", "tv", "Stranger-Things"),
    # ── Games ──
    ("Mass Effect", "game", "Mass-Effect"),
    ("Halo", "game", "Halo"),
    ("Elder Scroll series", "game", "Elder-Scroll-series"),
    ("Legend of Zelda", "game", "Legend-of-Zelda"),
    ("Kingdom Hearts", "game", "Kingdom-Hearts"),
    ("Final Fantasy VII", "game", "Final-Fantasy-VII"),
    ("Five Nights at Freddy's", "game", "Five-Nights-at-Freddy-s"),
    ("Undertale", "game", "Undertale"),
    # ── Cartoons ──
    ("Avatar: Last Airbender", "cartoon", "Avatar-Last-Airbender"),
    ("Teen Titans", "cartoon", "Teen-Titans"),
    ("Danny Phantom", "cartoon", "Danny-Phantom"),
    ("Transformers", "cartoon", "Transformers"),
    ("Miraculous: Tales of Ladybug & Cat Noir", "cartoon",
     "Miraculous-Tales-of-Ladybug-Cat-Noir"),
    ("Steven Universe", "cartoon", "Steven-Universe"),
    # ── Comics ──
    ("Batman", "comic", "Batman"),
    ("X-Men", "comic", "X-Men"),
    ("Justice League", "comic", "Justice-League"),
    ("Spider-Man", "comic", "Spider-Man"),
    ("Young Justice", "comic", "Young-Justice"),
    # ── Plays / Musicals ──
    ("Hamilton", "play", "Hamilton"),
    ("RENT", "play", "RENT"),
    ("Wicked", "play", "Wicked"),
    ("Les Miserables", "play", "Les-Miserables"),
    # ── Misc ──
    ("Bible", "misc", "Bible"),
    ("Greek Mythology", "misc", "Greek-Mythology"),
    ("Wrestling", "misc", "Wrestling"),
]
"""Curated popular FFN fandoms — populates the GUI's Fandom multi-
picker. Power users typing in the Fandom text field bypass this
entirely. Display labels are user-facing; slugs are FFN's canonical
URL slugs."""


_FFN_TOP_FANDOM_INDEX: dict[str, tuple[str, str]] = {
    label.lower(): (cat, slug) for label, cat, slug in FFN_TOP_FANDOMS
}


# FFN fandom-page URL params use shorter names than the keyword
# search endpoint. Sort table also drops the "best match" entry —
# fandom browses default to "updated" so a missing sort means the
# server's default, not a relevance score.
FFN_FANDOM_SORT = {
    "updated": 1, "published": 2, "reviews": 3,
    "favorites": 4, "follows": 5,
}


def _ffn_fandom_slug(name: str) -> str:
    """Convert a user-typed fandom name into FFN's URL slug shape.

    FFN's slugs are mostly title-cased words joined by hyphens
    (``Harry Potter`` → ``Harry-Potter``), with a handful of
    quirks:

    * Punctuation other than apostrophes / colons / commas is
      stripped; replaced by hyphens between word boundaries.
    * The connectors ``a``, ``an``, ``and``, ``of``, ``the``, ``to``
      stay lowercase unless they're the first word
      (``Percy-Jackson-and-the-Olympians``).
    * Apostrophes drop ("Freddy's" → ``Freddy-s``).

    Returns ``""`` for inputs that slug to nothing — the caller
    treats that as "no fandom set".
    """
    if not name:
        return ""
    s = name.strip()
    if not s:
        return ""
    # Hyphenate around non-alphanumeric runs.
    s = re.sub(r"[^A-Za-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        return ""
    # Title-case but leave the small connectors lowercase unless at
    # the start. Single-letter tail tokens (the ``s`` from ``Freddy's``
    # → ``Freddy-s``) also stay lowercase — FFN's canonical slug for
    # those is ``/Five-Nights-at-Freddy-s/`` with the trailing lowercase
    # letter intact.
    SMALL = {
        "a", "an", "and", "at", "by", "for", "in", "of", "on", "or",
        "the", "to", "vs", "with",
    }
    parts = s.split("-")
    out = []
    for i, p in enumerate(parts):
        if not p:
            continue
        lower = p.lower()
        if i > 0 and (lower in SMALL or len(p) == 1):
            out.append(lower)
        else:
            out.append(p[:1].upper() + p[1:].lower())
    return "-".join(out)


def _resolve_filter(value, choices, name):
    """Map a user value (label or raw ID) to a FFN param value.

    Label matching is case-insensitive so callers can pass natural-case
    labels like "K+" or "English" without having to remember the table
    casing.
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    lower = s.lower()
    for key, resolved in choices.items():
        if key.lower() == lower:
            return resolved
    valid = ", ".join(k for k in choices if k not in ("any", "all"))
    raise ValueError(f"Unknown {name}: {value!r}. Valid: {valid}")


def _build_search_url(query, filters, page=1):
    params = {"keywords": query, "ready": 1, "type": "story"}

    mapping = [
        ("censorid", "rating", FFN_RATING),
        ("languageid", "language", FFN_LANGUAGE),
        ("statusid", "status", FFN_STATUS),
        ("genreid", "genre", FFN_GENRE),
        # FFN's search form has a second genre dropdown (AND filter);
        # same value table as the first.
        ("genreid2", "genre2", FFN_GENRE),
        ("words", "min_words", FFN_WORDS),
        ("formatid", "crossover", FFN_CROSSOVER),
        ("match", "match", FFN_MATCH),
        ("sortid", "sort", FFN_SORT),
    ]
    for param, key, table in mapping:
        value = filters.get(key)
        resolved = _resolve_filter(value, table, key)
        if resolved is not None:
            params[param] = resolved

    if page and page > 1:
        params["ppage"] = int(page)

    return FFN_BASE + SEARCH_PATH + "?" + urlencode(params)


_SEARCH_FETCH_MAX_RETRIES = 4
"""How many attempts ``_fetch_search_page`` makes before giving up.
The chapter scraper uses ``max_retries=5`` by default; search is a
single fetch per user click rather than a long library walk, so we
want comparable first-contact 403 resilience without spending the
full chapter-fetch budget on one dropdown change."""

_SEARCH_FETCH_BACKOFF_S = 2.0
"""Base sleep between search retries (linear: ``base * attempt``).
FFN's Cloudflare 403s on this path resolve on the next CDN edge
cache hit rather than needing exponential decay, and search is
interactive — a long wait would feel broken to the user."""

_SEARCH_CF_CHALLENGE_BODY_PREFIX = 2000
"""Bytes of the response body inspected for the ``Just a moment...``
Cloudflare challenge signature. The marker lands well inside the
first 2KB on every challenge variant we've seen; reading more wastes
work on the happy path."""


def _new_search_session(browser=None):
    """Construct a curl_cffi Session pre-loaded with the chapter
    scraper's client-hint headers.

    FFN's Cloudflare deployment 403s first-contact requests that
    don't carry the high-entropy client hints listed in its
    ``Critical-CH`` response. Reusing the scraper's constants pins
    the Chromium version in one place — when curl_cffi bumps its
    Chrome target, both code paths update together.
    """
    from .scraper import BROWSERS, _CHROMIUM_CLIENT_HINTS
    if browser is None:
        browser = BROWSERS[0]
    sess = curl_requests.Session(impersonate=browser)
    if browser in ("chrome", "edge"):
        sess.headers.update(_CHROMIUM_CLIENT_HINTS)
    return sess


def _search_host_for_url(url):
    """Lower-case the URL's host with the ``www.`` prefix stripped.
    Matches the chapter scraper's CF-cookie cache key shape so search
    and chapter fetches share a single on-disk entry per host."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _seed_search_cf_cookies(sess, url):
    """Apply on-disk Cloudflare cookies for the URL's host onto
    ``sess`` if a cached solve exists. Returns ``True`` when cookies
    were injected.

    Search never invokes the Playwright solver itself (the 300MB
    browser dep is opt-in and search is interactive), but if a prior
    chapter download solved the challenge already, riding those
    persisted cookies is free.
    """
    try:
        from . import cf_solve
    except ImportError:
        return False
    host = _search_host_for_url(url)
    if not host:
        return False
    cached = cf_solve.load_cached(host)
    if cached is None:
        return False
    cf_solve.inject_into_session(sess, cached)
    return True


def _fetch_search_page(url):
    """Fetch a search-results page, retrying through impersonation
    rotation on Cloudflare 403s.

    The chapter scraper has the full retry/rotation/cf-solve
    machinery for 403s. Search used to bypass all of it — one
    ``Session(impersonate="chrome")`` with no retries — and abort on
    the first refusal. This mirrors the same minimum hardening:
    client-hint headers, browser-impersonation rotation between
    attempts, on-disk CF cookie seeding (free re-use of any solve a
    chapter fetch already produced), and short linear backoff.

    HTTP 404 still raises immediately — the fandom-browse
    auto-detect loop in :func:`search_ffn` relies on the ``"404"``
    substring to mean "wrong category, try the next slug".
    """
    from .scraper import BROWSERS
    browser = BROWSERS[0]
    sess = _new_search_session(browser)
    _seed_search_cf_cookies(sess, url)

    last_status = None
    last_exc = None
    for attempt in range(_SEARCH_FETCH_MAX_RETRIES):
        try:
            resp = sess.get(url, timeout=30)
        except Exception as exc:
            last_exc = exc
            if attempt >= _SEARCH_FETCH_MAX_RETRIES - 1:
                break
            time.sleep(_SEARCH_FETCH_BACKOFF_S * (attempt + 1))
            continue

        last_status = resp.status_code
        if resp.status_code == 200:
            lower = resp.text[:_SEARCH_CF_CHALLENGE_BODY_PREFIX].lower()
            if "just a moment" in lower and "cloudflare" in lower:
                if attempt >= _SEARCH_FETCH_MAX_RETRIES - 1:
                    raise RuntimeError(
                        "Cloudflare challenge detected. "
                        "Try again in a few minutes."
                    )
                browser = random.choice(BROWSERS)
                sess = _new_search_session(browser)
                _seed_search_cf_cookies(sess, url)
                time.sleep(_SEARCH_FETCH_BACKOFF_S * (attempt + 1))
                continue
            return resp.text

        if resp.status_code == 404:
            # search_ffn's fandom auto-detect inspects the message
            # for "404"; keep it terminal here so the next category
            # slug gets tried instead of burning retries on a wrong URL.
            raise RuntimeError("Search request failed (HTTP 404).")

        if resp.status_code in (403, 429, 503):
            if attempt >= _SEARCH_FETCH_MAX_RETRIES - 1:
                break
            logger.debug(
                "Search HTTP %d on %s; rotating impersonation and "
                "retrying (attempt %d/%d)",
                resp.status_code, url, attempt + 1,
                _SEARCH_FETCH_MAX_RETRIES,
            )
            browser = random.choice(BROWSERS)
            sess = _new_search_session(browser)
            _seed_search_cf_cookies(sess, url)
            time.sleep(_SEARCH_FETCH_BACKOFF_S * (attempt + 1))
            continue

        raise RuntimeError(
            f"Search request failed (HTTP {resp.status_code}). "
            "FFN may be blocking requests — try again later."
        )

    if last_exc is not None and last_status is None:
        raise RuntimeError(
            f"Search request failed: {last_exc}. "
            "FFN may be blocking requests — try again later."
        ) from last_exc
    raise RuntimeError(
        f"Search request failed (HTTP {last_status}). "
        "FFN may be blocking requests — try again later."
    )


def _extract_title(stitle_tag):
    """Extract the story title from the stitle link, preserving spaces
    between bold-wrapped keywords and surrounding text."""
    parts = []
    for child in stitle_tag.children:
        # Skip the cover image thumbnail
        if hasattr(child, "name") and child.name == "img":
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        else:
            parts.append(child.get_text())
    return " ".join("".join(parts).split())


def _parse_results(html):
    """Parse the FFN search results HTML and return a list of result dicts."""
    soup = BeautifulSoup(html, "lxml")
    result_divs = soup.find_all("div", class_="z-list")
    results = []

    for div in result_divs:
        stitle = div.find("a", class_="stitle")
        if not stitle:
            continue

        href = stitle.get("href", "")
        url = FFN_BASE + href if href else ""
        title = _extract_title(stitle)

        author_tag = div.find("a", href=lambda h: h and "/u/" in h)
        author = author_tag.get_text(strip=True) if author_tag else "Unknown"

        # Summary is the text content of z-indent before the metadata div
        zindent = div.find("div", class_="z-indent")
        summary = ""
        if zindent:
            summary_parts = []
            for child in zindent.children:
                if hasattr(child, "attrs") and "z-padtop2" in child.get(
                    "class", []
                ):
                    break
                text = (
                    child.get_text(" ", strip=True)
                    if hasattr(child, "get_text")
                    else str(child).strip()
                )
                if text:
                    summary_parts.append(text)
            summary = " ".join(summary_parts)

        # Metadata from the gray div
        meta_div = div.find("div", class_="z-padtop2")
        meta_text = meta_div.get_text(" ", strip=True) if meta_div else ""

        words_m = re.search(r"Words:\s*([\d,]+)", meta_text)
        # Accept comma-grouped chapter counts (``Chapters: 1,234``).
        # FFN normally shows small counts but the old ``\d+``-only regex
        # would silently truncate a four-digit count to its leading digit.
        chapters_m = re.search(r"Chapters:\s*([\d,]+)", meta_text)
        rating_m = re.search(r"Rated:\s*(\S+)", meta_text)
        status_m = re.search(r"\bComplete\b", meta_text)

        # Fandom is the first segment before " - Rated:"
        fandom = ""
        fandom_m = re.match(r"^(.+?)\s*-\s*Rated:", meta_text)
        if fandom_m:
            fandom = fandom_m.group(1).strip()

        results.append(
            {
                "title": title,
                "author": author,
                "url": url,
                "summary": summary,
                "words": words_m.group(1) if words_m else "?",
                "chapters": chapters_m.group(1) if chapters_m else "1",
                "rating": rating_m.group(1) if rating_m else "?",
                "fandom": fandom,
                "status": "Complete" if status_m else "In-Progress",
            }
        )

    return results


def _resolve_fandom(
    fandom_raw: str | None, category_raw: str | None,
) -> tuple[str, str] | None:
    """Map a user-typed Fandom + Category onto an ``(category, slug)``
    pair. Returns ``None`` when the input doesn't look like a fandom
    request — the caller then falls through to the keyword-search
    endpoint.

    Resolution order:

    1. Empty fandom → ``None`` (no fandom browse).
    2. Curated label match (case-insensitive) → use the pinned
       ``(category, slug)`` from :data:`_FFN_TOP_FANDOM_INDEX`, even
       if the user picked a category that disagrees. The curated
       entry wins because we know it's correct.
    3. User-supplied category + free-typed name → slugify the name
       and pair with the category.
    4. Free-typed name without a category → caller tries each
       category in turn (auto-detect). Returned ``category`` is
       empty in this case.
    """
    raw = (fandom_raw or "").strip()
    if not raw:
        return None
    # Multi-picker writes comma-joined picks; FFN fandom-browse is
    # single-fandom by URL shape, so take the first entry. Tail
    # entries effectively become discoverability hints, not filters.
    name = raw.split(",", 1)[0].strip()
    if not name:
        return None
    # Extract the picker's annotation suffix BEFORE stripping it — the
    # bracketed category ("Naruto [anime]") is a real hint about which
    # FFN section the user meant. ``(book)`` parentheticals also count
    # for hand-composed inputs.
    picker_cat_hint = ""
    m = re.search(r"\s*[\(\[]([A-Za-z]+)[\)\]]\s*$", name)
    if m:
        hint = m.group(1).strip().lower()
        if hint in FFN_CATEGORIES and FFN_CATEGORIES[hint]:
            picker_cat_hint = hint
        name = name[:m.start()].strip()
    if not name:
        return None
    cat_str = (category_raw or "").strip().lower().lstrip("/")
    if cat_str == "any":
        cat_str = ""
    # Precedence for category resolution:
    #   1. User's explicit Category dropdown selection (not "any").
    #   2. The picker's bracketed hint (e.g. "Naruto [anime]").
    #   3. The curated label index pinned category.
    #   4. Auto-detect across categories in popularity order.
    effective_cat = cat_str or picker_cat_hint
    if not effective_cat:
        pinned = _FFN_TOP_FANDOM_INDEX.get(name.lower())
        if pinned:
            return pinned
    slug = _ffn_fandom_slug(name)
    if not slug:
        return None
    if (
        effective_cat
        and effective_cat in FFN_CATEGORIES
        and FFN_CATEGORIES[effective_cat]
    ):
        # When the user pinned a category but typed a curated name,
        # honour the curated slug shape (it's the canonical FFN slug)
        # under the pinned category.
        pinned = _FFN_TOP_FANDOM_INDEX.get(name.lower())
        if pinned:
            return effective_cat, pinned[1]
        return effective_cat, slug
    return "", slug


_FFN_AUTO_DETECT_ORDER = (
    "book", "anime", "movie", "tv", "game", "cartoon", "comic",
    "play", "misc",
)
"""Order :func:`search_ffn` tries when the user didn't pin a category.
Roughly by popularity so the common case (book + anime) gets answered
without burning HTTPS round-trips against the long tail."""


def _split_names(value) -> list[str]:
    """Normalise a comma-separated string or a list into a clean name list."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _parse_fandom_filter_options(html: str) -> dict:
    """Parse a fandom page's character/world ``<select>`` options into
    ``{"characters": {name: id}, "worlds": {name: id}}``.

    Character and world IDs are fandom-specific (Harry Potter has ~460
    characters, each with its own numeric id), so they can only be
    resolved by name against the live page — there's no global table.
    """
    soup = BeautifulSoup(html, "lxml")

    def options(select_name):
        sel = soup.find("select", attrs={"name": select_name})
        out = {}
        if sel:
            for opt in sel.find_all("option"):
                val = opt.get("value")
                label = opt.get_text(strip=True)
                if val and val != "0" and label:
                    out[label] = val
        return out

    return {
        "characters": options("characterid1"),
        "worlds": options("verseid1"),
    }


def fetch_ffn_fandom_filters(category: str, slug: str) -> dict:
    """Fetch a fandom page and return its per-fandom filter option maps
    (``characters`` and ``worlds``) for name→id resolution."""
    html = _fetch_search_page(f"{FFN_BASE}/{category}/{slug}/")
    return _parse_fandom_filter_options(html)


def _resolve_named(value, options: dict, kind: str):
    """Resolve a character/world NAME (or raw numeric id) to its FFN id
    against a fandom's option map. Case-insensitive exact match first, then
    a unique substring match. Raises ValueError on unknown/ambiguous names
    so the caller can surface a useful message instead of silently
    dropping the filter."""
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return s
    low = s.lower()
    for label, vid in options.items():
        if label.lower() == low:
            return vid
    matches = [(label, vid) for label, vid in options.items() if low in label.lower()]
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        raise ValueError(
            f"Unknown {kind} {value!r} for this fandom. "
            "Check spelling, or use the exact name from FFN's filter list."
        )
    raise ValueError(
        f"Ambiguous {kind} {value!r} — matches: "
        + ", ".join(label for label, _ in matches[:6])
    )


def _fandom_filters_requested(filters: dict) -> bool:
    """True if any filter that needs the per-fandom character/world option
    map (so we must fetch the fandom page first to resolve names)."""
    return any(
        filters.get(k)
        for k in ("characters", "world", "exclude_characters", "exclude_world")
    )


def _build_ffn_fandom_url(
    category: str, slug: str, filters: dict, page: int, options: dict = None,
) -> str:
    """Build the fandom-page URL with the short param names FFN uses on
    category listings (``srt``, ``t``, ``r``, ``g1``, ``len``, ``v1``,
    ``c1``…``c4``, ``pm`` and the ``_``-prefixed exclusion variants).

    ``options`` is the ``{"characters": ..., "worlds": ...}`` map from
    :func:`fetch_ffn_fandom_filters`, required only when a character or
    world filter (by name) is present.
    """
    options = options or {"characters": {}, "worlds": {}}
    params: dict = {}
    # Sort: fandom URLs use ``srt`` (no "best match" — default is
    # "updated" server-side, so omit when the user didn't override).
    sort_raw = filters.get("sort")
    if sort_raw:
        s = str(sort_raw).strip().lower()
        if s in FFN_FANDOM_SORT:
            params["srt"] = FFN_FANDOM_SORT[s]
        elif s.isdigit():
            params["srt"] = int(s)
        elif s == "best match":
            pass  # default
    time_val = _resolve_filter(filters.get("time"), FFN_TIME, "time")
    if time_val is not None:
        params["t"] = time_val
    rating = _resolve_filter(filters.get("rating"), FFN_RATING, "rating")
    if rating is not None:
        params["r"] = rating
    g1 = _resolve_filter(filters.get("genre"), FFN_GENRE, "genre")
    if g1 is not None:
        params["g1"] = g1
    g2 = _resolve_filter(filters.get("genre2"), FFN_GENRE, "genre2")
    if g2 is not None:
        params["g2"] = g2
    ex_genre = _resolve_filter(
        filters.get("exclude_genre"), FFN_GENRE, "exclude_genre",
    )
    if ex_genre is not None:
        params["_g1"] = ex_genre
    # Word length: the fandom ``len`` param, NOT the keyword ``words`` param.
    mw = filters.get("min_words")
    if mw:
        key = str(mw).strip().lower()
        if key in _FFN_WORDS_TO_LEN:
            params["len"] = _FFN_WORDS_TO_LEN[key]
        elif key.isdigit():
            params["len"] = int(key)
    status_raw = filters.get("status")
    if status_raw:
        sr = str(status_raw).strip().lower()
        if sr == "in-progress":
            params["s"] = 1
        elif sr == "complete":
            params["s"] = 2
        elif sr.isdigit():
            params["s"] = int(sr)
    lang = _resolve_filter(filters.get("language"), FFN_LANGUAGE, "language")
    if lang is not None:
        params["lan"] = lang
    # World / verse (fandom-specific id, resolved by name).
    world = filters.get("world")
    if world:
        vid = _resolve_named(world, options["worlds"], "world")
        if vid:
            params["v1"] = vid
    ex_world = filters.get("exclude_world")
    if ex_world:
        vid = _resolve_named(ex_world, options["worlds"], "world")
        if vid:
            params["_v1"] = vid
    # Characters A–D (c1..c4) and exclusions (_c1, _c2), resolved by name.
    for idx, name in enumerate(_split_names(filters.get("characters"))[:4], 1):
        cid = _resolve_named(name, options["characters"], "character")
        if cid:
            params[f"c{idx}"] = cid
    for idx, name in enumerate(
        _split_names(filters.get("exclude_characters"))[:2], 1,
    ):
        cid = _resolve_named(name, options["characters"], "character")
        if cid:
            params[f"_c{idx}"] = cid
    # Pairing: require the selected characters to be in a relationship.
    if filters.get("pairing"):
        params["pm"] = 1
    if page and page > 1:
        params["p"] = int(page)
    base = f"{FFN_BASE}/{category}/{slug}/"
    if params:
        return base + "?" + urlencode(params)
    return base


def _search_ffn_fandom(
    query: str, category: str, slug: str, filters: dict, page: int,
) -> list[dict]:
    """Fetch a fandom-page listing and return parsed result dicts. If
    ``query`` is non-empty, results are post-filtered on title /
    summary substring match — fandom pages have no native keyword
    filter, so this is the best we can do.

    Backfills the per-row ``fandom`` field from the URL because
    fandom-page HTML omits the redundant fandom name from each card's
    metadata div (the whole page IS the fandom, so it'd be noise
    inline). The GUI's Fandom column then shows the human-readable
    label instead of staying empty.

    Character/world filters need the fandom's own option list (those ids
    are fandom-specific), so when one is requested we fetch the fandom
    page once for its filter options before building the filtered URL.
    """
    options = (
        fetch_ffn_fandom_filters(category, slug)
        if _fandom_filters_requested(filters)
        else None
    )
    url = _build_ffn_fandom_url(category, slug, filters, page, options=options)
    html = _fetch_search_page(url)
    results = _parse_results(html)
    if query:
        q_lower = query.lower()
        kept = []
        for r in results:
            blob = (
                (r.get("title", "") or "") + " "
                + (r.get("summary", "") or "")
            ).lower()
            if q_lower in blob:
                kept.append(r)
        results = kept
    # Backfill fandom name from the slug (FFN's slug→display is
    # ``Harry-Potter`` → ``Harry Potter``; we don't have an authoritative
    # reverse for compound names but un-hyphening is the canonical
    # round-trip for the curated entries).
    display = slug.replace("-", " ")
    for r in results:
        if not r.get("fandom"):
            r["fandom"] = display
    return results


def search_ffn(query, *, page=1, **filters):
    """Search fanfiction.net and return a list of result dicts.

    Two modes:

    * **Keyword search** (no ``fandom``) — hits ``/search/`` with the
      ``keywords`` field. The original behaviour; all the existing
      filter knobs apply (rating, language, status, genre, etc.).
    * **Fandom browse** (``fandom`` set) — fetches the FFN category
      page (``/<category>/<slug>/``) directly. Parallel to the
      erotica tag-browse pattern: the URL IS the search target.
      When ``query`` is also supplied, results are post-filtered on
      title/summary substring match (FFN's fandom pages have no
      native keyword filter).

    Keyword filters (all optional — pass a label from the corresponding
    FFN_* table, or the raw numeric ID):
        rating: all / K / K+ / T / M / K-T
        language: english / spanish / french / ... (see FFN_LANGUAGE)
        status: all / in-progress / complete
        genre: romance / humor / adventure / angst / ... (see FFN_GENRE)
        genre2: same values as `genre`; adds a second AND-filtered genre.
        min_words: <1k / <5k / 5k+ / 30k+ / 50k+ / 150k+ / 300k+
        crossover: any / only / exclude  (keyword-search mode only)
        match: any / title / summary  (keyword-search mode only)
        fandom: free-typed or curated label — switches to fandom-browse.
        category: book / anime / movie / tv / game / cartoon / comic
                  / play / misc — pins the fandom URL category.

    Each result dict has keys: title, author, url, summary, words,
    chapters, rating, fandom, status.

    `page` (keyword-only) selects a specific results page for "load more"
    workflows — defaults to 1.
    """
    resolved = _resolve_fandom(filters.get("fandom"), filters.get("category"))
    if resolved is not None:
        category, slug = resolved
        if category:
            return _search_ffn_fandom(query, category, slug, filters, page)
        # Auto-detect: try each category in turn until one returns rows
        # or returns a 200 with an empty parse (i.e. the URL existed).
        # 404s mean wrong category and we keep walking.
        first_empty: list[dict] | None = None
        for guess in _FFN_AUTO_DETECT_ORDER:
            try:
                results = _search_ffn_fandom(query, guess, slug, filters, page)
            except RuntimeError as exc:
                # ``_fetch_search_page`` raises on non-200; 404 means
                # "wrong category", try the next.
                if "404" in str(exc):
                    continue
                raise
            if results:
                return results
            if first_empty is None:
                first_empty = results
        return first_empty or []
    # Keyword-search mode: the character/world/time/pairing/exclusion
    # filters live only on FFN's fandom pages, so warn rather than
    # silently drop them when no fandom was given.
    fandom_only = [
        k for k in (
            "time", "characters", "world", "exclude_genre",
            "exclude_characters", "exclude_world", "pairing",
        )
        if filters.get(k)
    ]
    if fandom_only:
        logger.warning(
            "FFN keyword search ignores fandom-only filters (%s); "
            "pick a fandom to use them.", ", ".join(fandom_only),
        )
    url = _build_search_url(query, filters, page=page)
    html = _fetch_search_page(url)
    return _parse_results(html)


# ── AO3 search ────────────────────────────────────────────────────

AO3_RATING = {
    "all": None,
    "not rated": 9,
    "general": 10,
    "teen": 11,
    "mature": 12,
    "explicit": 13,
}

AO3_COMPLETE = {
    "any": None,
    "complete": "T",
    "in-progress": "F",
}

AO3_CROSSOVER = {
    "any": None,
    "only": "T",
    "exclude": "F",
}

AO3_SORT = {
    "best match": "_score",
    "author": "authors_to_sort_on",
    "title": "title_to_sort_on",
    "date posted": "created_at",
    "date updated": "revised_at",
    "word count": "word_count",
    "hits": "hits",
    "kudos": "kudos_count",
    "comments": "comments_count",
    "bookmarks": "bookmarks_count",
}

# AO3 category IDs (work_search[category_ids][]). These are global
# tag IDs assigned by AO3 to each of the six top-level relationship
# categories; values sourced from AO3's search form source.
AO3_CATEGORY = {
    "any": None,
    "gen": 21,
    "f/m": 22,
    "m/m": 23,
    "f/f": 116,
    "multi": 2246,
    "other": 24,
}

# AO3 Archive Warning tag IDs (work_search[archive_warning_ids][]). Global
# tag IDs read from AO3's search form; verified live 2026-06-22.
AO3_WARNINGS = {
    "any": None,
    "none apply": 16,
    "creator chose not to warn": 14,
    "graphic violence": 17,
    "major character death": 18,
    "rape/non-con": 19,
    "underage": 20,
}

# AO3 accepts either the short language code (ISO-ish — "en", "zh") or
# the numeric language_id in `work_search[language_id]`. Short codes are
# stable across AO3's DB rebuilds, so we use those. The pretty-label
# lookup here just saves users from memorizing codes; anything unknown
# is passed through so raw codes still work for languages not listed.
AO3_LANGUAGES = {
    "any": None,
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "polish": "pl",
    "dutch": "nl",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "turkish": "tr",
    "arabic": "ar",
    "hebrew": "he",
    "indonesian": "id",
    "vietnamese": "vi",
    "czech": "cs",
    "hungarian": "hu",
    "greek": "el",
    "ukrainian": "uk",
    "thai": "th",
    "latin": "la",
}


def _build_ao3_search_url(query, filters, page=1):
    params = {}
    if query:
        params["work_search[query]"] = query
    if page and page > 1:
        params["page"] = int(page)

    def resolve(value, choices, name):
        if value is None or value == "":
            return None
        s = str(value).strip()
        if s.isdigit():
            return int(s) if name != "complete" and name != "crossover" else s
        lower = s.lower()
        for key, resolved in choices.items():
            if key.lower() == lower:
                return resolved
        valid = ", ".join(k for k in choices if k != "any" and k != "all")
        raise ValueError(f"Unknown {name}: {value!r}. Valid: {valid}")

    rating = resolve(filters.get("rating"), AO3_RATING, "rating")
    if rating is not None:
        params["work_search[rating_ids]"] = rating

    complete = resolve(filters.get("complete"), AO3_COMPLETE, "complete")
    if complete is not None:
        params["work_search[complete]"] = complete

    crossover = resolve(filters.get("crossover"), AO3_CROSSOVER, "crossover")
    if crossover is not None:
        params["work_search[crossover]"] = crossover

    category = resolve(filters.get("category"), AO3_CATEGORY, "category")
    if category is not None:
        # AO3 expects category_ids as an array param — urlencode handles
        # the [] suffix when we feed it a list under the same key.
        params["work_search[category_ids][]"] = category

    warning = resolve(filters.get("warning"), AO3_WARNINGS, "warning")
    if warning is not None:
        params["work_search[archive_warning_ids][]"] = warning

    sort = resolve(filters.get("sort"), AO3_SORT, "sort")
    if sort is not None:
        params["work_search[sort_column]"] = sort

    if filters.get("single_chapter"):
        params["work_search[single_chapter]"] = 1

    # Language: accept a pretty label from AO3_LANGUAGES, or a raw ISO
    # code / numeric ID passed through verbatim. The "any" label in
    # AO3_LANGUAGES maps to None and means "omit the filter" — so a
    # ``matched is None`` after a successful lookup must NOT fall back
    # to passing through ``lang_str="any"`` as a literal language code,
    # which AO3's search would treat as a no-result filter.
    lang_raw = filters.get("language")
    if lang_raw:
        lang_str = str(lang_raw).strip()
        sentinel = object()
        matched = sentinel
        for label, code in AO3_LANGUAGES.items():
            if label.lower() == lang_str.lower():
                matched = code
                break
        if matched is sentinel:
            params["work_search[language_id]"] = lang_str
        elif matched is not None:
            params["work_search[language_id]"] = matched

    # Free-text AO3 fields pass straight through
    for key, param in [
        ("fandom", "work_search[fandom_names]"),
        ("word_count", "work_search[word_count]"),
        ("character", "work_search[character_names]"),
        ("relationship", "work_search[relationship_names]"),
        ("freeform", "work_search[freeform_names]"),
        ("title", "work_search[title]"),
        ("creator", "work_search[creators]"),
    ]:
        value = filters.get(key)
        if value:
            params[param] = value

    return AO3_BASE + "/works/search?" + urlencode(params)


def _parse_ao3_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    works_ol = soup.find("ol", class_="work")
    if not works_ol:
        return results

    for li in works_ol.find_all("li", recursive=False):
        heading = li.find("h4", class_="heading")
        if not heading:
            continue

        title_link = heading.find("a", href=re.compile(r"^/works/\d+"))
        if not title_link:
            continue
        title = title_link.get_text(strip=True)
        work_id_m = re.search(r"/works/(\d+)", title_link["href"])
        if not work_id_m:
            continue
        url = f"{AO3_BASE}/works/{work_id_m.group(1)}"

        # Authors are the other <a> tags in the heading (all but the title link)
        authors = [
            a.get_text(strip=True)
            for a in heading.find_all("a")
            if a is not title_link and "/users/" in a.get("href", "")
        ]
        author = ", ".join(authors) if authors else "Anonymous"

        fandoms_h5 = li.find("h5", class_="fandoms")
        fandom = ""
        if fandoms_h5:
            fandoms = [a.get_text(strip=True) for a in fandoms_h5.find_all("a")]
            fandom = ", ".join(fandoms)

        summary_bq = li.find("blockquote", class_="summary")
        summary = summary_bq.get_text(" ", strip=True) if summary_bq else ""

        stats_dl = li.find("dl", class_="stats")
        words = "?"
        chapters = "1"
        status = "In-Progress"
        if stats_dl:
            w = stats_dl.find("dd", class_="words")
            if w:
                words = w.get_text(strip=True)
            c = stats_dl.find("dd", class_="chapters")
            if c:
                ratio = c.get_text(strip=True)
                parts = ratio.split("/")
                if parts:
                    chapters = parts[0]
                if len(parts) == 2 and parts[0] == parts[1]:
                    status = "Complete"

        rating = "?"
        rating_li = li.find("span", class_="rating")
        if rating_li:
            rating = rating_li.get("title") or rating_li.get_text(strip=True)

        # Series membership — AO3 blurbs show "Part N of <a>Series</a>"
        series_entries = []
        for s_li in li.select("ul.series li"):
            s_link = s_li.find("a", href=re.compile(r"^/series/\d+"))
            if not s_link:
                continue
            s_id_m = re.search(r"/series/(\d+)", s_link["href"])
            if not s_id_m:
                continue
            part_m = re.match(
                r"Part\s+(\d+)\s+of", s_li.get_text(" ", strip=True), re.I,
            )
            series_entries.append({
                "id": s_id_m.group(1),
                "name": s_link.get_text(strip=True),
                "url": f"{AO3_BASE}/series/{s_id_m.group(1)}",
                "part": int(part_m.group(1)) if part_m else None,
            })

        results.append(
            {
                "title": title,
                "author": author,
                "url": url,
                "summary": summary,
                "words": words,
                "chapters": chapters,
                "rating": rating,
                "fandom": fandom,
                "status": status,
                "series": series_entries,
            }
        )

    return results


def search_ao3(query, *, page=1, **filters):
    """Search Archive of Our Own and return a list of result dicts.

    Keyword filters (all optional):
        rating: all / not rated / general / teen / mature / explicit
        category: any / gen / f/m / m/m / f/f / multi / other
        complete: any / complete / in-progress
        crossover: any / only / exclude
        sort: best match / date updated / kudos / hits / ... (see AO3_SORT)
        single_chapter: truthy → one-shots only
        language: label ("english", "french") or raw ISO code ("en", "fr")
        fandom: fandom name(s) (AO3 accepts loose matching)
        word_count: range expression e.g. "<5000", ">10000", "1000-5000"
        character, relationship, freeform, title, creator: AO3 free-text fields

    `page` (keyword-only) selects a specific results page.
    """
    url = _build_ao3_search_url(query, filters, page=page)
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"AO3 search failed (HTTP {resp.status_code}). "
            "The site may be temporarily unavailable."
        )
    return _parse_ao3_results(resp.text)


# ── Royal Road search ─────────────────────────────────────────────

RR_STATUS = {
    "any": None,
    "ongoing": "ONGOING",
    "hiatus": "HIATUS",
    "completed": "COMPLETED",
    "complete": "COMPLETED",
    "dropped": "DROPPED",
    "stub": "STUB",
}

RR_TYPE = {
    "any": None,
    "original": "ORIGINAL",
    "fanfiction": "FANFICTION",
}

RR_ORDER_BY = {
    "relevance": "relevance",
    "popularity": "popularity",
    "last update": "last_update",
    "pages": "pages",
    "rating": "rating",
    "title": "title",
}

# RR's curated discovery lists. When one of these is picked, the search
# switches from /fictions/search?title=... to /fictions/<slug>, which
# doesn't accept a free-text query but does accept tagsAdd.
RR_LISTS = {
    "search": None,
    "best rated": "best-rated",
    "trending": "trending",
    "active popular": "active-popular",
    "weekly popular": "weekly-popular",
    "monthly popular": "monthly-popular",
    "latest updates": "latest-updates",
    "new releases": "new-releases",
    "complete": "complete",
    "rising stars": "rising-stars",
}

# Royal Road genres and content tags. On RR these are the same field
# under the hood (tagsAdd=<slug>) — "genres" is just the first 15 RR
# assigns top-billing to. Split here for UX: users recognize "Fantasy"
# as a genre and "LitRPG" as a tag.
# Key = human-readable label; value = RR's slug for the tagsAdd param.
RR_GENRES = {
    "Action": "action",
    "Adventure": "adventure",
    "Comedy": "comedy",
    "Contemporary": "contemporary",
    "Drama": "drama",
    "Fantasy": "fantasy",
    "Historical": "historical",
    "Horror": "horror",
    "Mystery": "mystery",
    "Psychological": "psychological",
    "Romance": "romance",
    "Satire": "satire",
    "Sci-fi": "sci_fi",
    "Short Story": "one_shot",
    "Tragedy": "tragedy",
}

RR_TAGS = {
    "Anti-Hero Lead": "anti-hero_lead",
    "Artificial Intelligence": "artificial_intelligence",
    "Attractive Lead": "attractive_lead",
    "Cyberpunk": "cyberpunk",
    "Dungeon": "dungeon",
    "Dystopia": "dystopia",
    "Female Lead": "female_lead",
    "First Contact": "first_contact",
    "GameLit": "gamelit",
    "Gender Bender": "gender_bender",
    "Genetically Engineered": "genetically_engineered",
    "Grimdark": "grimdark",
    "Hard Sci-fi": "hard_sci-fi",
    "Harem": "harem",
    "Hero": "hero",
    "High Fantasy": "high_fantasy",
    "LitRPG": "litrpg",
    "Low Fantasy": "low_fantasy",
    "Magic": "magic",
    "Male Lead": "male_lead",
    "Martial Arts": "martial_arts",
    "Military": "military",
    "Multiple Lead Characters": "multiple_lead",
    "Mythos": "mythos",
    "Non-Human Lead": "non-human_lead",
    "Portal Fantasy / Isekai": "summoned_hero",
    "Post Apocalyptic": "post_apocalyptic",
    "Progression": "progression",
    "Reader Interactive": "reader_interactive",
    "Reincarnation": "reincarnation",
    "Ruling Class": "ruling_class",
    "School Life": "school_life",
    "Secret Identity": "secret_identity",
    "Slice of Life": "slice_of_life",
    "Soft Sci-fi": "soft_sci-fi",
    "Space Opera": "space_opera",
    "Sports": "sports",
    "Steampunk": "steampunk",
    "Strategy": "strategy",
    "Strong Lead": "strong_lead",
    "Super Heroes": "super_heroes",
    "Supernatural": "supernatural",
    "Technologically Engineered": "technologically_engineered",
    "Time Loop": "loop",
    "Time Travel": "time_travel",
    "Urban Fantasy": "urban_fantasy",
    "Villainous Lead": "villainous_lead",
    "Virtual Reality": "virtual_reality",
    "War and Military": "war_and_military",
    "Wuxia": "wuxia",
    "Xianxia": "xianxia",
}

# Content warnings — RR submits these as warningsAdd params (separate
# bucket from tagsAdd, though the UI looks identical).
RR_WARNINGS = {
    "Profanity": "profanity",
    "Sexual Content": "sexuality",
    "Gore": "gore",
    "Traumatising Content": "traumatising",
    "AI-Assisted Content": "ai_assisted",
    "AI-Generated Content": "ai_generated",
}


def _rr_slug_list(raw, lookup=None):
    """Split a comma/semicolon list into slugs, translating labels via
    `lookup` (label → slug) when possible. Unknown entries pass through
    verbatim so power users can hand-write RR's raw slugs.
    """
    if not raw:
        return []
    out = []
    for piece in re.split(r"[,;]", str(raw)):
        s = piece.strip()
        if not s:
            continue
        if lookup:
            # Case-insensitive label lookup
            matched = None
            for label, slug in lookup.items():
                if label.lower() == s.lower():
                    matched = slug
                    break
            out.append(matched if matched else s)
        else:
            out.append(s)
    return out


def _rr_positive_int(value, name):
    """Coerce a user-supplied min/max filter value to int, or None when
    blank. Rejects negatives and non-numeric input with ValueError.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if n < 0:
        raise ValueError(f"{name} must be >= 0, got {n}")
    return n


def _collect_rr_tag_slugs(filters):
    """Gather tagsAdd slugs from the three filter surfaces — genres,
    tags, and the legacy free-text `tags` field — in a single list.
    De-dupes so a user who selects "Fantasy" in genres and then also
    types "fantasy" in tags doesn't send it twice.
    """
    seen = []
    for source, lookup in (
        ("genres", RR_GENRES),
        ("tags_picked", RR_TAGS),
        ("tags", None),
    ):
        for slug in _rr_slug_list(filters.get(source), lookup):
            if slug and slug not in seen:
                seen.append(slug)
    return seen


def _build_rr_search_url(query, filters, page=1):
    list_key = (filters.get("list") or "").strip().lower()
    list_slug = RR_LISTS.get(list_key)
    tag_slugs = _collect_rr_tag_slugs(filters)
    warning_slugs = _rr_slug_list(filters.get("warnings"), RR_WARNINGS)

    if list_slug:
        # List endpoints ignore `title=`. Keep tagsAdd / warningsAdd
        # working so users can browse e.g. "Rising Stars tagged
        # progression with gore warnings filtered in".
        params = {}
        if page and page > 1:
            params["page"] = int(page)
        query_parts = (
            list(params.items())
            + [("tagsAdd", t) for t in tag_slugs]
            + [("warningsAdd", w) for w in warning_slugs]
        )
        suffix = "?" + urlencode(query_parts) if query_parts else ""
        return RR_BASE + f"/fictions/{list_slug}" + suffix

    params = {}
    if query:
        params["title"] = query
    if page and page > 1:
        params["page"] = int(page)
    status = (filters.get("status") or "").strip().lower()
    if status and status in RR_STATUS and RR_STATUS[status]:
        params["status"] = RR_STATUS[status]
    type_ = (filters.get("type") or "").strip().lower()
    if type_ and type_ in RR_TYPE and RR_TYPE[type_]:
        params["type"] = RR_TYPE[type_]
    order = (filters.get("order_by") or "").strip().lower()
    if order and order in RR_ORDER_BY and RR_ORDER_BY[order] != "relevance":
        params["orderBy"] = RR_ORDER_BY[order]

    for key, param in (
        ("min_words", "minWords"),
        ("max_words", "maxWords"),
        ("min_pages", "minPages"),
        ("max_pages", "maxPages"),
    ):
        n = _rr_positive_int(filters.get(key), key)
        if n is not None:
            params[param] = n

    # Rating is 0.0–5.0 on RR; pass through as-is when non-empty.
    rating_raw = filters.get("min_rating")
    if rating_raw is not None and str(rating_raw).strip() != "":
        try:
            rating = float(str(rating_raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"min_rating must be a number, got {rating_raw!r}")
        if rating < 0 or rating > 5:
            raise ValueError(f"min_rating must be between 0 and 5, got {rating}")
        params["minRating"] = rating

    if tag_slugs or warning_slugs:
        return (
            RR_BASE + "/fictions/search?" +
            urlencode(
                list(params.items())
                + [("tagsAdd", t) for t in tag_slugs]
                + [("warningsAdd", w) for w in warning_slugs]
            )
        )
    return RR_BASE + "/fictions/search?" + urlencode(params)


def _parse_rr_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.find_all("div", class_="fiction-list-item"):
        title_link = item.find("h2", class_="fiction-title")
        if title_link:
            title_link = title_link.find("a", href=re.compile(r"/fiction/\d+"))
        if not title_link:
            continue
        href = title_link["href"]
        url = RR_BASE + href if href.startswith("/") else href
        title = title_link.get_text(strip=True)

        # Author — not directly shown in search results, leave blank
        author = ""

        # Status labels (ONGOING/COMPLETED/etc.) and type (Original/Fanfiction).
        # STUB is orthogonal to completion state: the author stubbed the
        # fiction (usually because it's been published elsewhere), but the
        # work may still be ongoing or complete underneath. Track it as a
        # flag and combine with whatever completion label is present. When
        # the card only carries STUB with no completion label (common for
        # stubbed works), flag the result for later enrichment from the
        # fiction page — see search_royalroad().
        status = "In-Progress"
        stubbed = False
        completion_from_card = False
        rating = "?"
        labels = [
            lbl.get_text(strip=True).upper()
            for lbl in item.find_all("span", class_="label")
        ]
        for lbl in labels:
            if lbl == "COMPLETED":
                status = "Complete"
                completion_from_card = True
            elif lbl in ("HIATUS", "DROPPED", "INACTIVE"):
                status = lbl.title()
                completion_from_card = True
            elif lbl == "ONGOING":
                status = "In-Progress"
                completion_from_card = True
            elif lbl == "STUB":
                stubbed = True
        stubbed_unknown = stubbed and not completion_from_card
        if stubbed and completion_from_card:
            status = f"{status} (Stubbed)"
        elif stubbed:
            status = "Stubbed"

        # Genre tags
        tag_links = item.find_all("a", class_="fiction-tag")
        genre_or_fandom = ", ".join(a.get_text(strip=True) for a in tag_links[:5])

        # Stats — pages, chapters, followers. RR search cards DON'T
        # expose a raw word count anywhere — only "N Pages". Convert at
        # RR's house conversion of ~275 words per page so the "Words"
        # column actually resembles a word count instead of looking
        # like a tiny 4-digit story. A leading "~" signals the
        # estimate; the fiction page itself (fetched on download) has
        # the authoritative count.
        stats_text = item.get_text(" ", strip=True)
        pages_m = re.search(r"(\d[\d,]*)\s+Pages", stats_text)
        chapters_m = re.search(r"(\d[\d,]*)\s+Chapters", stats_text)
        if pages_m:
            pages = int(pages_m.group(1).replace(",", ""))
            words = f"~{pages * 275:,}"
        else:
            words = "?"
        chapters = chapters_m.group(1) if chapters_m else "?"

        # Description — in a hidden #description-<id> div; show first N chars
        desc_div = item.find("div", id=re.compile(r"^description-\d+"))
        summary = desc_div.get_text(" ", strip=True) if desc_div else ""
        if not summary:
            desc_wrap = item.find("div", class_="fiction-description")
            if desc_wrap:
                summary = desc_wrap.get_text(" ", strip=True)

        results.append({
            "title": title,
            "author": author,
            "url": url,
            "summary": summary,
            "words": words,
            "chapters": chapters,
            "rating": rating,
            "fandom": genre_or_fandom,
            "status": status,
            "_stubbed_unknown": stubbed_unknown,
        })

    return results


def _fetch_rr_fiction_status(session, fiction_url):
    """Pull the canonical completion label (Complete/In-Progress/Hiatus/…)
    from a Royal Road fiction page. Used to fill in the status for
    stubbed search results, whose cards don't carry completion info.
    Returns None if the page can't be parsed.
    """
    try:
        resp = session.get(fiction_url, timeout=30)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    for lbl in soup.find_all("span", class_="label"):
        text = lbl.get_text(strip=True).upper()
        if text == "COMPLETED":
            return "Complete"
        if text == "ONGOING":
            return "In-Progress"
        if text in ("HIATUS", "DROPPED", "INACTIVE"):
            return text.title()
    return None


def search_royalroad(query, *, page=1, **filters):
    """Search royalroad.com. Returns result dicts matching search_ffn shape.

    Keyword filters:
        status:   any / ongoing / hiatus / completed / dropped / stub
        type:     any / original / fanfiction
        order_by: relevance / popularity / last update / pages / rating / title
        genres:   comma-separated RR genre labels (see RR_GENRES, e.g.
                  "Fantasy,Sci-fi"). Accepts raw slugs too.
        tags_picked: comma-separated RR tag labels (see RR_TAGS, e.g.
                  "LitRPG,Progression"). Accepts raw slugs too.
        tags:     legacy free-text tag list (raw slugs, e.g. "progression,magic")
        warnings: comma-separated warning labels (see RR_WARNINGS) —
                  selects fictions that carry each listed warning.
        min_words / max_words: word-count bounds (integer).
        min_pages / max_pages: page-count bounds (integer).
        min_rating: minimum average rating 0.0-5.0.
        list:     search (default) / best rated / trending / active popular /
                  weekly popular / monthly popular / latest updates /
                  new releases / complete / rising stars

    `page` (keyword-only) selects a specific results page. When `list` is
    set to one of the non-search values, the free-text query is ignored
    and the corresponding RR discovery page is browsed instead; `tags`
    and `warnings` still filter, but `status`/`type`/`order_by` and the
    min/max numeric filters do not apply to list-browse endpoints.
    """
    url = _build_rr_search_url(query, filters, page=page)
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Royal Road search failed (HTTP {resp.status_code})."
        )
    results = _parse_rr_results(resp.text)
    # Enrich stubbed results whose cards don't carry a completion label.
    # The fiction page reliably does, so one cheap GET per such row
    # gives us "Complete (Stubbed)" instead of a bare "Stubbed".
    for r in results:
        if r.pop("_stubbed_unknown", False):
            real = _fetch_rr_fiction_status(session, r["url"])
            if real:
                r["status"] = f"{real} (Stubbed)"
    return results


# ── Literotica search ─────────────────────────────────────────────
# Literotica's keyword search is JS-rendered against an auth-gated API.
# The public tag-browse subdomain (tags.literotica.com/<tag>) is
# server-rendered with schema.org microdata on every story card, so we
# treat "search" as tag-browsing: the user's query becomes a tag slug
# (lowercased, spaces → hyphens). That covers most what-you'd-actually-
# type-into-a-search-box use cases on Literotica.


def _literotica_tag_slug(query: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 -]+", "", query).strip().lower()
    return re.sub(r"\s+", "-", s)


_LIT_STORY_HREF_RE = re.compile(
    r"^https?://(?:www\.)?literotica\.com/s/[a-z0-9][a-z0-9-]*$"
)
"""Whitelist for story-card title anchors. Pinning the host + the
``/s/`` path keeps tag-page nav links (``/femdom?dialog=log_in``,
``/c/<category>``) and series links (``/series/se/<id>``) from being
mistaken for cards. The trailing ``$`` rejects query-string variants
(bookmark dialog, login redirect) that share the ``/s/`` prefix but
aren't story permalinks."""

_LIT_AUTHOR_HREF_RE = re.compile(
    r"/authors/[^/]+/works", re.I
)

_LIT_CATEGORY_HREF_RE = re.compile(
    r"literotica\.com/c/[a-z0-9-]+", re.I
)


def _parse_literotica_results(html):
    """Parse story cards from a Literotica tag-browse page.

    Literotica migrated to a Next.js front-end whose class names rotate
    per build (``_works_item_kpkm_4`` → ``_works_item_<rand>_4`` etc.),
    so the previous schema.org selectors
    (``property="itemListElement"``, ``property="ratingValue"``) match
    zero cards on the live site. Switch to attribute-based selectors
    that don't depend on rotating class names:

    * Title links carry ``rel="external"`` and a stable
      ``literotica.com/s/<slug>`` href. That's the durable anchor we
      use to enumerate cards.
    * Author links land on ``/authors/<name>/works``.
    * Category links land on ``literotica.com/c/<slug>``.
    * Ratings are wrapped in a ``<span data-value="X.YZ" title="Rating">``.

    Each title anchor's enclosing ``<div role="article">`` carries the
    full card metadata; we walk up at most eight levels to find it
    before falling back to the bare title/url pair.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_urls = set()
    for a in soup.find_all("a", attrs={"rel": "external"}):
        href = a.get("href", "")
        if not _LIT_STORY_HREF_RE.match(href):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = a.get_text(" ", strip=True)
        if not title:
            continue

        # Walk up to the enclosing card so we can sibling-search for
        # description / author / category / rating without re-matching
        # at the document root.
        card = a
        for _ in range(8):
            card = getattr(card, "parent", None)
            if card is None:
                break
            if (
                getattr(card, "name", None) == "div"
                and card.get("role") == "article"
            ):
                break
        else:
            card = None
        if card is None or card.name != "div":
            results.append({
                "title": title, "author": "", "url": href, "summary": "",
                "words": "?", "chapters": "?", "rating": "?", "fandom": "",
                "status": "",
            })
            continue

        author = ""
        author_a = card.find("a", href=_LIT_AUTHOR_HREF_RE)
        if author_a:
            author = author_a.get_text(" ", strip=True)

        # Description is the only <p> in the card that isn't the title
        # paragraph. The title paragraph carries ``role="heading"``;
        # everything else (including the bookmark / login aria-label
        # span) lives inside that heading paragraph too, so the only
        # way to reach the real description is to skip heading <p>s
        # outright. Among the remaining paragraphs, ignore the
        # "by <author>" attribution line.
        summary = ""
        for p in card.find_all("p"):
            if p.get("role") == "heading":
                continue
            ptext = p.get_text(" ", strip=True)
            if not ptext or ptext == title or ptext.lower().startswith("by "):
                continue
            summary = ptext
            break

        fandom = ""
        cat_a = card.find("a", href=_LIT_CATEGORY_HREF_RE)
        if cat_a:
            fandom = cat_a.get_text(" ", strip=True)

        rating = "?"
        rating_span = card.find(
            "span", attrs={"data-value": True, "title": "Rating"}
        )
        if rating_span:
            rating = rating_span.get("data-value", "?")

        results.append({
            "title": title,
            "author": author,
            "url": href,
            "summary": summary,
            "words": "?",
            "chapters": "?",
            "rating": rating,
            "fandom": fandom,
            "status": "",
        })
    return results


# Hard ceiling on how many pages ``fetch_until_limit`` will request,
# regardless of ``limit``. A misbehaving site that returns the same
# rows on every page (CDN caching the wrong query, server-side bug,
# pagination param the site ignores) would otherwise loop forever
# even though the real result set is small. 200 pages is more than
# any sane "load more" UI traversal would need.
_FETCH_UNTIL_LIMIT_MAX_PAGES = 200


def fetch_until_limit(search_fn, query, *, limit, start_page=1, **kwargs):
    """Call `search_fn` across successive pages until `limit` results are
    collected or upstream signals exhaustion. Returns
    ``(SearchPage(results, exhausted=...), next_page)``.

    The ``SearchPage.exhausted`` flag on the returned batch tells the
    caller (e.g. the GUI's Load More button) whether further calls
    would be useful. Earlier code keyed Load More off ``bool(results)``
    — empty filtered pages then mis-disabled the button even when more
    upstream pages remained.

    `results` is the full list of dicts from all fetched pages (may run
    a little past `limit` if the last page's natural size overshoots).
    The caller can trim it further if they want a hard cap.

    Bounded by :data:`_FETCH_UNTIL_LIMIT_MAX_PAGES` and a "no new
    results between consecutive pages" check so a site that keeps
    serving the same page forever can't peg the worker thread.
    """
    collected: list[dict] = []
    page = max(1, int(start_page))
    end_page = page + _FETCH_UNTIL_LIMIT_MAX_PAGES
    seen_signatures: set[tuple] = set()
    exhausted = False
    while len(collected) < limit and page < end_page:
        page_results = search_fn(query, page=page, **kwargs)
        # _page_exhausted handles legacy ``[]`` (treated as exhausted)
        # vs SearchPage(exhausted=False) (a filtered-empty page that
        # may have keepers later).
        page_exhausted = _page_exhausted(page_results)
        if not page_results:
            if page_exhausted:
                exhausted = True
                page += 1
                break
            # Filtered-empty page — keep walking until upstream signals
            # exhaustion or we hit _FETCH_UNTIL_LIMIT_MAX_PAGES.
            page += 1
            continue
        # Detect a page that returned exactly the same rows as a
        # previous page (URL+title fingerprint). Any one collision is
        # already a strong signal of a non-paginating endpoint — bail
        # rather than re-collect the same rows another 199 times.
        signature = tuple(
            (r.get("url") or "", r.get("title") or "")
            for r in page_results
        )
        if signature in seen_signatures:
            exhausted = True
            page += 1
            break
        seen_signatures.add(signature)
        collected.extend(page_results)
        if page_exhausted:
            exhausted = True
            page += 1
            break
        page += 1
    return SearchPage(collected, exhausted=exhausted), page


# Bounded multi-page ceiling for the erotica fan-out specifically.
# Lower than the per-site _FETCH_UNTIL_LIMIT_MAX_PAGES because each
# erotica iteration fires N parallel HTTP requests (one per active
# site), so the polite-network budget burns faster. Six pages × ~5
# active sites × ~8 rows/page is more than enough headroom for the
# default GUI ``limit=25`` target.
_FETCH_EROTICA_MAX_PAGES = 6


def fetch_erotica_until_limit(
    search_fn, query, *, limit, start_page=1, **kwargs,
):
    """Multi-page driver for the erotica fan-out.

    The plain :func:`fetch_until_limit` helper flattens the wrapper
    object the GUI relies on (``ErotiCAResults.site_stats`` /
    ``.exhausted_sites``), so the erotica frame used to bypass it and
    fetch only one page per click. That capped a tag-only search at
    ``PER_SITE_LIMIT * num_supported_sites`` rows on first load — fine
    for a narrow query, but a broad tag like ``feet`` (5 sites, ~8
    rows each, then series collapse + dedup) ended up presenting
    ~20 results when 100+ existed.

    This driver preserves the wrapper. Each iteration calls
    ``search_fn`` with ``page=N``, accumulates the rows, merges the
    per-site stats, and forwards ``exhausted_sites`` back into
    ``skip_sites`` so finished archives don't get re-polled. Stops
    when ``len(accumulated) >= limit``, when every active site is
    exhausted, or when the page ceiling is hit.

    Returns ``(ErotiCAResults, next_page)`` — the same shape the GUI's
    Load More expects from :func:`fetch_until_limit`.
    """
    from .erotica.search import ErotiCAResults

    accumulated = ErotiCAResults()
    accumulated.site_stats = {}
    accumulated.exhausted_sites = set()
    accumulated.total_sites = set()
    skip_set: set[str] = set(kwargs.pop("skip_sites", None) or ())
    page = max(1, int(start_page))
    end_page = page + _FETCH_EROTICA_MAX_PAGES

    while len(accumulated) < limit and page < end_page:
        page_results = search_fn(
            query, page=page, skip_sites=skip_set, **kwargs,
        )
        accumulated.extend(page_results)
        # Capture the canonical eligible-sites set from the first page
        # that surfaces one. Subsequent pages invoke ``search_erotica``
        # with a larger ``skip_sites``, so their ``total_sites`` shrinks
        # — only the first page reports the original cohort, which the
        # GUI needs as a stable denominator for its "all exhausted?"
        # check on Load More.
        if not accumulated.total_sites:
            page_total = getattr(page_results, "total_sites", None)
            if page_total:
                accumulated.total_sites = set(page_total)

        stats = getattr(page_results, "site_stats", None) or {}
        for site, st in stats.items():
            prev = accumulated.site_stats.get(site)
            if prev is None:
                accumulated.site_stats[site] = dict(st)
            else:
                # Merge counts; flip ``ok`` off if any page failed;
                # take the latest ``exhausted`` flag.
                prev["count"] = (prev.get("count") or 0) + (st.get("count") or 0)
                if st.get("ok") is False:
                    prev["ok"] = False
                    prev["error"] = st.get("error") or prev.get("error")
                prev["exhausted"] = bool(st.get("exhausted"))

        new_exhausted = getattr(page_results, "exhausted_sites", None) or set()
        accumulated.exhausted_sites |= new_exhausted
        skip_set |= new_exhausted

        # If every site that was active is now exhausted, stop early.
        active = set(stats.keys()) - accumulated.exhausted_sites
        if not active:
            page += 1
            break
        # If this page didn't add any new rows for any active site, the
        # remaining sites have nothing useful — bail rather than burn
        # the rest of the page budget polling for empty pages.
        if not page_results:
            page += 1
            break
        page += 1

    return accumulated, page


def collapse_ao3_series(results):
    """Fold multiple AO3 works that share a series into a single series
    row, but only when 2+ parts of the same series appear in the results.
    Solo matches stay as regular work rows — promoting them to a "series"
    label hides the work's real title behind the series title.

    Works that belong to more than one series still appear as work rows;
    collapsing them would hide the multi-membership.

    ``series_parts`` is sorted by the AO3 part number so a downstream
    consumer iterating the list reads chapters in series order; AO3's
    search results come back in relevance / date order which would
    otherwise scramble the part sequence.
    """
    series_counts = {}
    for r in results:
        series = r.get("series") or []
        if len(series) != 1:
            continue
        sid = series[0]["id"]
        series_counts[sid] = series_counts.get(sid, 0) + 1

    collapsed = []
    seen_series = {}
    for r in results:
        series = r.get("series") or []
        if len(series) != 1 or series_counts.get(series[0]["id"], 0) < 2:
            collapsed.append(r)
            continue
        s = series[0]
        if s["id"] in seen_series:
            seen_series[s["id"]]["series_parts"].append(r)
            continue
        row = {
            "title": s["name"],
            "author": r.get("author", ""),
            "url": s["url"],
            "summary": r.get("summary", ""),
            "words": "?",
            "chapters": str(series_counts[s["id"]]),
            "rating": r.get("rating", "?"),
            "fandom": r.get("fandom", ""),
            "status": "Series",
            "is_series": True,
            "series_id": s["id"],
            "series_parts": [r],
        }
        seen_series[s["id"]] = row
        collapsed.append(row)

    def _part_key(work):
        entries = work.get("series") or [{}]
        part = entries[0].get("part") if entries else None
        # Unknown parts sort to the end so explicit parts come first.
        return (part is None, part if part is not None else 10**9)

    for row in seen_series.values():
        row["series_parts"].sort(key=_part_key)
    return collapsed


# Literotica titles/URLs suffix chapters and parts a few different ways:
#   /s/foo-ch-07          "Foo Ch. 07"
#   /s/foo-pt-02          "Foo Pt. 02"
#   /s/foo-6              "Foo - 6"     (bare-number)
#   /s/foo-p4             "Foo P4"      (compact "P<N>")
# Authors sometimes append a variant tag ("Ch. 07 - Alt Ending") which we
# strip so variants group with their canonical siblings.
_LIT_CHAPTER_URL_RE = re.compile(
    r"/s/(.+?)-(?:ch|chapter|pt|part)-(\d+)(?:-[^/]*)?/?$",
    re.IGNORECASE,
)
_LIT_COMPACT_P_URL_RE = re.compile(
    r"/s/(.+?)-p(\d+)/?$",
    re.IGNORECASE,
)
_LIT_BARE_NUM_URL_RE = re.compile(
    r"/s/(.+?)-(\d+)/?$",
)
_LIT_BARE_SLUG_URL_RE = re.compile(r"/s/([^/?#]+?)/?$", re.IGNORECASE)
_LIT_CHAPTER_TITLE_RE = re.compile(
    r"^(.*?)(?:[\s:]+(?:Ch|Chapter|Pt|Part)\.?\s*\d+"
    r"|\s*[-\u2013]\s*\d+"
    r"|\s+P\d+)(?:\s*[-:].*)?$",
    re.IGNORECASE,
)

# Prefix-style chapter titles where the marker leads instead of
# trails \u2014 e.g. ``"Chapter 2. The Package"``, ``"Ch 12 The Visit"``,
# ``"Part 4: Aftermath"``. The base story title is whatever follows
# the marker (and trailing punctuation), so the group key uses that
# as the base instead of an empty string. Without this alternation,
# any work whose author followed the leading-chapter convention slips
# past ``collapse_literotica_series`` entirely.
_LIT_CHAPTER_TITLE_PREFIX_RE = re.compile(
    r"^(?:Ch|Chapter|Pt|Part)\.?\s*(\d+)(?:\s*[-:.,]?\s*)(.+?)\s*$",
    re.IGNORECASE,
)


def _literotica_series_key(result):
    """Return (base_slug, part_number, base_title) if the result looks like
    a numbered chapter of a larger Literotica work, else None.

    URL patterns with an explicit `ch`/`pt`/`part`/`p<N>` marker are
    trusted on the URL alone. The bare `-N` URL suffix is ambiguous
    (it also matches year-tagged annual stories like
    `/s/new-years-eve-2024`) so it's only accepted when the *title*
    also carries a numeric chapter marker — either a trailing
    ``" Ch. NN"`` suffix or a leading ``"Chapter NN"`` prefix.
    """
    url = result.get("url") or ""
    title = result.get("title") or ""
    suffix_match = _LIT_CHAPTER_TITLE_RE.match(title)
    prefix_match = (
        _LIT_CHAPTER_TITLE_PREFIX_RE.match(title) if not suffix_match else None
    )
    title_has_marker = bool(suffix_match or prefix_match)
    strict_patterns = (
        (_LIT_CHAPTER_URL_RE, True),
        (_LIT_COMPACT_P_URL_RE, True),
        (_LIT_BARE_NUM_URL_RE, title_has_marker),
    )
    for pattern, accept in strict_patterns:
        m = pattern.search(url)
        if not m:
            continue
        if not accept:
            return None
        base_slug = m.group(1).lower()
        try:
            part = int(m.group(2))
        except ValueError:
            continue
        if suffix_match:
            base_title = suffix_match.group(1).strip()
        elif prefix_match:
            # Prefix form: ``"Chapter 2. The Package"`` → base title is
            # ``"The Package"``. Keeps the displayed series row anchored
            # on the meaningful part of the title rather than the empty
            # head before the marker.
            base_title = prefix_match.group(2).strip().rstrip(":,. ")
        else:
            base_title = title
        return base_slug, part, base_title
    return None


def _literotica_bare_slug(result):
    """Return the /s/<slug> slug portion of a Literotica URL, lowercased."""
    url = result.get("url") or ""
    m = _LIT_BARE_SLUG_URL_RE.search(url)
    return m.group(1).lower() if m else None


def collapse_literotica_series(results):
    """Group Literotica results that are numbered chapters of the same
    work (same URL slug stem, same author) into one series row. Only
    collapses when 2+ parts are present — otherwise a lone "Ch. 06"
    would be hidden behind a series label with no siblings to show.

    Literotica's own convention is that Part 1 of a series is posted
    *without* any suffix (just the bare title), and subsequent parts
    get "Pt. 02" / "Ch. 02" / "- 2" appended. So if we see a bare-
    titled work whose URL slug is the base stem of some other suffixed
    work by the same author, we treat it as part 1 of that series.
    """
    groups = {}  # (author, base_slug) → [(index, result, part, base_title)]
    seen_indices = set()
    for i, r in enumerate(results):
        key = _literotica_series_key(r)
        if key is None:
            continue
        base_slug, part, base_title = key
        group_key = (r.get("author") or "", base_slug)
        groups.setdefault(group_key, []).append((i, r, part, base_title))
        seen_indices.add(i)

    # Second pass: adopt bare-titled results as part 1 when their slug
    # matches an existing group's base_slug and the author lines up —
    # but only if that group doesn't *already* have an explicit Part 1
    # member. That guard prevents a standalone work that happens to
    # share a slug prefix with a later, unrelated serial from being
    # folded into it.
    for i, r in enumerate(results):
        if i in seen_indices:
            continue
        bare = _literotica_bare_slug(r)
        if not bare:
            continue
        group_key = (r.get("author") or "", bare)
        if group_key not in groups:
            continue
        existing_parts = {m[2] for m in groups[group_key]}
        if 1 in existing_parts:
            continue
        title = r.get("title") or bare
        groups[group_key].append((i, r, 1, title))

    to_collapse = {}  # anchor index → series row
    hide = set()
    for group_key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[2])
        anchor_i, anchor_r, _, base_title = members[0]
        parts = [m[1] for m in members]
        for i, *_ in members:
            hide.add(i)
        to_collapse[anchor_i] = {
            "title": base_title,
            "author": anchor_r.get("author", ""),
            "url": anchor_r.get("url", ""),
            "summary": anchor_r.get("summary", ""),
            "words": "?",
            "chapters": str(len(parts)),
            "rating": anchor_r.get("rating", "?"),
            "fandom": anchor_r.get("fandom", ""),
            "status": "Series",
            "is_series": True,
            "series_id": f"lit:{group_key[1]}",
            "series_parts": parts,
            # Signal to the download dispatch: iterate series_parts URLs
            # directly rather than trying to resolve a /series/se/<id>.
            "parts_only": True,
        }

    collapsed = []
    for i, r in enumerate(results):
        if i in to_collapse:
            collapsed.append(to_collapse[i])
        elif i in hide:
            continue
        else:
            collapsed.append(r)
    return collapsed


# Lushstories' multi-part convention (per the
# ``ffn_dl.erotica.lushstories`` module docstring): part 1 lives at
# the bare slug, parts 2+ append ``-2``, ``-3``, … to the slug. The
# search scrape returns each part as its own row with no series
# metadata, so we recover the grouping from URL shape alone.
_LUSH_SLUG_URL_RE = re.compile(
    r"lushstories\.com/stories/[^/?#]+/([^/?#]+?)/?(?:\?|#|$)",
    re.IGNORECASE,
)
# Trailing ``-N`` part suffix on a slug. ``N`` ≥ 2 so ``part==1`` is
# only ever introduced by the bare-slug "adopt as part 1" pass below;
# ``N <= 99`` so we don't fold a year-tagged annual story
# (``new-years-eve-2024``) into a phantom 2024-part series.
_LUSH_PART_SUFFIX_RE = re.compile(r"^(.+?)-(\d{1,2})$")

# Lushstories title-based chapter detection — fallback for the many
# cases where the URL slug doesn't follow the ``-N`` convention
# (titles encoded into the slug verbatim: ``schoolgirl-chapter-4-the-
# guidance-counselor``, ``new-beginnings-...-ch-12-2``). Captures the
# part number plus the base title (before the marker) so siblings
# with different trailing subtitles still group together.
_LUSH_CHAPTER_TITLE_RE = re.compile(
    r"^(?P<base>.+?)\s+(?:Ch|Chapter|Pt|Part)\.?\s*(?P<part>\d{1,3})\b",
    re.IGNORECASE,
)


def _lushstories_series_key(result):
    """Return ``(base_slug, part)`` for a Lushstories chapter row, or
    ``None``. ``part`` is the integer chapter / part number (≥ 2).

    Two detection paths:

    * URL-slug ``-N`` suffix — covers the canonical Lush convention
      ("part 2 of <slug>" lives at ``<slug>-2``).
    * Title text — covers titles whose part marker leaks into the
      slug verbatim ("Schoolgirl Chapter 4 The Guidance Counselor"
      ends up at ``schoolgirl-chapter-4-the-guidance-counselor``,
      not ``schoolgirl-4``). The URL slug rule misses these and the
      collapse would silently leave each chapter as its own row.
    """
    url = result.get("url") or ""
    if "lushstories.com" not in url.lower():
        return None
    m = _LUSH_SLUG_URL_RE.search(url)
    if not m:
        return None
    slug = m.group(1).lower()
    # URL-slug path: bare ``-N`` suffix on the slug.
    part_m = _LUSH_PART_SUFFIX_RE.match(slug)
    if part_m:
        base = part_m.group(1)
        part = int(part_m.group(2))
        if part >= 2:
            return base, part
    # Title path: parse a leading or embedded "Ch N" / "Chapter N" /
    # "Pt N" / "Part N" marker out of the visible title. Group key is
    # built from the base title (slugified) rather than the URL slug
    # — siblings under this rule share a title prefix, not a URL
    # prefix.
    title = (result.get("title") or "").strip()
    if title:
        tm = _LUSH_CHAPTER_TITLE_RE.match(title)
        if tm:
            try:
                part = int(tm.group("part"))
            except ValueError:
                return None
            if part >= 2:
                base = re.sub(
                    r"[^a-z0-9]+", "-",
                    tm.group("base").strip().lower(),
                ).strip("-")
                if base:
                    return f"title:{base}", part
    return None


def _lushstories_bare_slug(result):
    """Return the URL slug from a lushstories result, lowercased; None
    when the URL isn't a lushstories story page."""
    url = result.get("url") or ""
    if "lushstories.com" not in url.lower():
        return None
    m = _LUSH_SLUG_URL_RE.search(url)
    return m.group(1).lower() if m else None


def collapse_lushstories_series(results):
    """Group Lushstories results that are ``-2``/``-3``/… siblings of a
    base story slug into one series row.

    Mirrors :func:`collapse_literotica_series` in shape — the search
    scrape doesn't surface author or series metadata, so the group key
    is the base slug alone. The per-site URL prefix in
    :data:`_LUSH_SLUG_URL_RE` keeps this from matching non-lush rows.

    Bare-slug adoption requires the group to already hold **two**
    explicit suffixed siblings (``foo-2`` AND ``foo-3``, not just
    ``foo-2`` alone). A single ``-N`` suffix is too ambiguous —
    real-world titles like ``route-66``, ``area-51``, ``catch-22``
    would otherwise be folded into a fake series alongside any
    standalone ``route`` / ``area`` / ``catch`` listing.
    """
    groups: dict[str, list] = {}  # base_slug → [(index, result, part)]
    seen_indices: set[int] = set()
    for i, r in enumerate(results):
        key = _lushstories_series_key(r)
        if key is None:
            continue
        base_slug, part = key
        groups.setdefault(base_slug, []).append((i, r, part))
        seen_indices.add(i)

    # Adopt a bare-slugged Lushstories result as part 1 when its slug
    # matches an existing group's base — Lush's convention is that part
    # 1 lives at the bare slug. Same guard as Literotica: only adopt
    # when the group doesn't *already* have an explicit part 1, and
    # only when at least two explicit suffixed siblings exist so a
    # numeric-ending title isn't paired up alongside an unrelated
    # standalone work.
    for i, r in enumerate(results):
        if i in seen_indices:
            continue
        slug = _lushstories_bare_slug(r)
        if not slug or slug not in groups:
            continue
        existing = groups[slug]
        if any(part == 1 for _, _, part in existing):
            continue
        if len(existing) < 2:
            continue
        groups[slug].append((i, r, 1))

    to_collapse: dict[int, dict] = {}
    hide: set[int] = set()
    for base_slug, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[2])
        anchor_i, anchor_r, _ = members[0]
        parts = [m[1] for m in members]
        for i, *_ in members:
            hide.add(i)
        to_collapse[anchor_i] = {
            "title": anchor_r.get("title", "") or base_slug,
            "author": anchor_r.get("author", ""),
            "url": anchor_r.get("url", ""),
            "summary": anchor_r.get("summary", ""),
            "words": "?",
            "chapters": str(len(parts)),
            "rating": anchor_r.get("rating", "?"),
            "fandom": anchor_r.get("fandom", ""),
            "status": "Series",
            "site": "lushstories",
            "is_series": True,
            "series_id": f"lush:{base_slug}",
            "series_parts": parts,
            "parts_only": True,
        }

    collapsed = []
    for i, r in enumerate(results):
        if i in to_collapse:
            collapsed.append(to_collapse[i])
        elif i in hide:
            continue
        else:
            collapsed.append(r)
    return collapsed


def collapse_erotica_series(results):
    """Run every per-site erotica series collapser over the merged
    fan-out batch.

    Each per-site collapser scopes its URL pattern to its own host so
    chaining them is order-independent: a Literotica row never reaches
    the Lushstories matcher and vice versa. Adding a new site means
    appending one more call here.

    A final dedup pass drops exact-duplicate rows the source-site
    HTML occasionally emits (Literotica's tag listings sometimes
    render the same work as both a series card and a chapter card,
    both carrying ``itemListElement`` markup — they survive the
    per-site ``seen`` set inside :func:`_parse_literotica_results`
    because the URLs differ, but their visible title + author + site
    identity is identical and showing them twice is noise).
    """
    out = collapse_literotica_series(results)
    out = collapse_lushstories_series(out)
    out = _dedup_erotica_results(out)
    return out


def _dedup_erotica_results(results):
    """Drop rows whose ``(url)`` or ``(title, author, site)`` identity
    repeats a row earlier in the list. The earlier row wins because
    upstream ordering is stable (alphabetic by site, then title) and
    callers expect that order to survive the collapse pipeline."""
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    out = []
    for r in results:
        url = (r.get("url") or "").strip()
        if url and url in seen_urls:
            continue
        title = (r.get("title") or "").strip().lower()
        author = (r.get("author") or "").strip().lower()
        site = (r.get("site") or "").strip().lower()
        # Series rows are aggregates; their constituent parts are
        # already hidden by the collapse pass, so a "second" series
        # row with the same display title is a real second series
        # not a stale duplicate. Skip the identity dedup for those.
        if not r.get("is_series") and title and (title, author, site) in seen_keys:
            continue
        if url:
            seen_urls.add(url)
        if title:
            seen_keys.add((title, author, site))
        out.append(r)
    return out


LIT_CATEGORIES = {
    "any": None,
    # Literotica's top-level category slugs also exist as tag slugs on
    # tags.literotica.com, so we reuse the tag-browse URL rather than
    # fetching /c/<slug> (which is paginated differently and doesn't
    # expose schema.org microdata per card).
    "Anal": "anal",
    "BDSM": "bdsm",
    "Celebrities & Fan Fiction": "celeb",
    "Chain Stories": "chain-story",
    "Erotic Couplings": "erotic-couplings",
    "Erotic Horror": "erotic-horror",
    "Exhibitionist & Voyeur": "exhibitionist",
    "Fetish": "fetish",
    "First Time": "first-time",
    "Gay Male": "gay-male",
    "Group Sex": "group-sex",
    "How To": "how-to",
    "Humor & Satire": "humor",
    "Illustrated": "illustrated",
    "Incest/Taboo": "incest",
    "Interracial Love": "interracial",
    "Lesbian Sex": "lesbian",
    "Letters & Transcripts": "letters",
    "Loving Wives": "loving-wives",
    "Mature": "mature",
    "Mind Control": "mind-control",
    "Non-consent/Reluctance": "non-consent",
    "Non-English": "non-english",
    "Novels and Novellas": "novel",
    "Reviews & Essays": "reviews-essays",
    "Romance": "romance",
    "Sci-Fi & Fantasy": "sci-fi-fantasy",
    "Toys & Masturbation": "toys",
    "Transgender & Crossdressers": "transgender",
}


def search_literotica(query, *, page=1, **filters):
    """Search Literotica by tag or category. `query` is converted to a
    tag slug (lowercased, whitespace → hyphens) and looked up on
    tags.literotica.com — the server-rendered alternative to
    Literotica's JS-only keyword search.

    Keyword filters (optional):
        category: picks one of Literotica's top-level categories from
                  LIT_CATEGORIES. When set, the category slug is used
                  instead of the query — browsing e.g. "Loving Wives"
                  standalone instead of searching for a user-typed tag.

    Unknown tags return no results; the response is still a 200 page,
    just without story cards.

    `page` (keyword-only) selects a specific results page.
    """
    cat_raw = filters.get("category") if filters else None
    slug = None
    if cat_raw:
        cat_str = str(cat_raw).strip()
        for label, cat_slug in LIT_CATEGORIES.items():
            if cat_slug and label.lower() == cat_str.lower():
                slug = cat_slug
                break
        if slug is None and cat_str.lower() not in ("", "any"):
            # Pass-through for users who type a raw slug.
            slug = _literotica_tag_slug(cat_str) or None
    if slug is None:
        slug = _literotica_tag_slug(query)
    if not slug:
        return []
    url = f"{LIT_TAGS_BASE}/{slug}"
    try:
        page_n = int(str(page).strip()) if page else 1
    except (TypeError, ValueError):
        page_n = 1
    if page_n > 1:
        url += f"?page={page_n}"
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Literotica search failed (HTTP {resp.status_code})."
        )
    return _parse_literotica_results(resp.text)


# ── Wattpad ──────────────────────────────────────────────────────

WP_API = "https://api.wattpad.com"
WP_PAGE_SIZE = 20


WP_MATURE = {
    "any": None,
    "exclude": "exclude",
    "only": "only",
}


WP_COMPLETED = {
    "any": None,
    "complete": True,
    "in-progress": False,
}


def search_wattpad(query, *, page=1, **filters):
    """Search Wattpad via the public v4 stories API.

    Wattpad's web search is JS-rendered and rate-limits aggressively, so
    we hit ``api.wattpad.com/v4/stories`` instead — the same endpoint
    the mobile app uses, accepting an unauthenticated GET.

    Keyword filters (optional):
        mature:    any / exclude / only — client-side filter on the
                   mature flag; Wattpad's API doesn't expose a filter
                   param so we filter the returned page.
        completed: any / complete / in-progress — same, client-side.

    The API uses offset-based paging; page=1 → offset=0, page=2 → offset=20.
    Returns a list of result dicts compatible with other search_* sites.
    """
    import json as _json
    from curl_cffi import requests as _curl_requests

    q = (query or "").strip()
    if not q:
        # Empty query would return a generic random set; be explicit.
        return SearchPage([], exhausted=True)

    try:
        page_n = max(1, int(page))
    except (TypeError, ValueError):
        page_n = 1
    offset = (page_n - 1) * WP_PAGE_SIZE

    fields = (
        "stories("
        "id,title,user,description,cover,completed,mature,numParts,"
        "url,tags,language,paidModel,isPaywalled,length"
        ")"
    )
    qs = {
        "query": q,
        "limit": WP_PAGE_SIZE,
        "offset": offset,
        "fields": fields,
    }
    url = f"{WP_API}/v4/stories?" + urlencode(qs)
    session = _curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Wattpad search failed (HTTP {resp.status_code})."
        )
    try:
        data = _json.loads(resp.text)
    except ValueError as exc:
        raise RuntimeError("Wattpad returned non-JSON.") from exc
    stories = data.get("stories") or []

    mature = (filters or {}).get("mature")
    completed = (filters or {}).get("completed")
    # Allow natural-case labels ("Complete", "In-Progress"), the
    # boolean-ish keys from the WP_COMPLETED table (``True``/``False``),
    # and the raw CLI strings. ``_norm`` alone would turn ``True`` into
    # ``"true"`` which never matches the ``"complete"`` arm below, so
    # boolean inputs were silently dropping the filter.
    def _norm(s):
        if s is True:
            return "complete"
        if s is False:
            return "in-progress"
        return str(s).strip().lower() if s is not None else None
    mature = _norm(mature)
    completed = _norm(completed)

    # Distinguish "upstream gave us fewer rows than a full page" (true
    # exhaustion — keep paging would just hit the same empty response)
    # from "page was full upstream but client-side filters threw
    # everything out" (more pages may still have keepers). The former
    # is the SearchPage exhausted=True case; the latter is
    # exhausted=False so fetch_until_limit walks on.
    upstream_exhausted = len(stories) < WP_PAGE_SIZE
    results: list[dict] = []
    for s in stories:
        is_mature = bool(s.get("mature"))
        is_complete = bool(s.get("completed"))
        if mature == "exclude" and is_mature:
            continue
        if mature == "only" and not is_mature:
            continue
        if completed == "complete" and not is_complete:
            continue
        if completed == "in-progress" and is_complete:
            continue

        user = s.get("user") or {}
        author = user.get("name") or user.get("fullname") or ""
        length = s.get("length") or 0
        # Character count → rough word count (5 chars/word). Wattpad
        # doesn't expose a word field, and we want search cards to
        # surface something more useful than "length".
        words_est = f"{max(1, int(length) // 5):,}" if length else ""
        tags = s.get("tags") or []
        fandom_str = ", ".join(tags[:3]) if tags else ""
        paid = s.get("paidModel") or ""
        rating_bits = []
        if is_mature:
            rating_bits.append("Mature")
        if paid:
            rating_bits.append("Paid")
        rating = " / ".join(rating_bits) or "GA"
        results.append({
            "title": s.get("title") or "Untitled",
            "author": author,
            "url": s.get("url") or f"https://www.wattpad.com/story/{s.get('id')}",
            "summary": (s.get("description") or "").strip(),
            "words": words_est,
            "chapters": str(s.get("numParts") or ""),
            "rating": rating,
            "fandom": fandom_str,
            "status": "Complete" if is_complete else "In-Progress",
        })
    return SearchPage(results, exhausted=upstream_exhausted)
