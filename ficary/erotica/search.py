"""Unified "Erotic Story Search" — fans out across every erotica site.

The existing per-site SearchFrame pattern (see :mod:`ficary.gui_search`)
gives one window per site. Erotica is different: the user's primary
axis is **the kink/tag**, not the site, so a single search window that
queries every erotica archive at once and returns merged results gives
a better experience than eight sidebar entries.

Public entry points:

* ``search_erotica(query, sites=None, tags=None, ...)`` — fan-out to
  every registered erotica site in parallel, merge the results, tag
  each row with its origin ``site`` so the GUI's "Site" column can
  display where each hit came from.
* ``EROTICA_SITE_SLUGS`` / ``EROTICA_TAG_VOCABULARY`` — metadata the
  GUI binds its dropdowns / tag picker to.

Per-site search functions here are deliberately small — Literotica's
native search already lives in :mod:`ficary.search` (imported below),
and the newer sites all expose a category or tag URL we can parse as
a result list without a real search API. Nothing here tries to be a
full-blown search engine: the point is to give the user a one-stop
discovery surface over all eight archives, not to replicate each
site's native filter set.

Tag search is a first-class input (per user feedback — see
``feedback_erotica_search.md`` in auto-memory). The unified vocabulary
below is the *intersection* of tags that appear meaningfully on ≥3 of
the 8 sites; niche per-site tags still work as free-text entries.
"""

from __future__ import annotations

import concurrent.futures
import html as html_module
import logging
import re
import string
import threading
import time
from typing import Callable, Optional

from bs4 import BeautifulSoup

from ..scraper import BaseScraper
from ..search import _parse_literotica_results, search_literotica
from . import tapatalk

logger = logging.getLogger(__name__)

PER_SITE_PAGE_MAX = 200
"""Runaway guard on rows parsed from one site page — not a batch size.
Adapters return each page's natural row count (Literotica ~94, AO3 20,
SOL 10...); this bound only trips on a parser bug or a site serving a
pathological listing. The previous design capped every site at 8 rows
per page and discarded the rest of the already-fetched page; because
Load More advances by *site page*, the discarded rows were skipped
permanently — broad tag searches surfaced a fraction of what the
sites actually held."""


def _single_listing_window(page: int) -> tuple[int, int]:
    """Row window for adapters whose site has one un-paginated listing.
    Maps fan-out page N onto rows ``[(N-1)*PER_SITE_PAGE_MAX,
    N*PER_SITE_PAGE_MAX)`` of that listing so every row is reachable
    via Load More and an off-the-end page returns ``[]`` — the
    fan-out's exhaustion signal."""
    p = max(1, int(page))
    return (p - 1) * PER_SITE_PAGE_MAX, p * PER_SITE_PAGE_MAX

REQUEST_TIMEOUT_S = 25

EROTICA_SITE_SLUGS: list[str] = [
    "all",
    "literotica",
    "ao3",
    "wattpad",
    "aff",
    "storiesonline",
    "nifty",
    "sexstories",
    "mcstories",
    "lushstories",
    "fictionmania",
    "tgstorytime",
    "chyoa",
    "darkwanderer",
    "greatfeet",
    "bdsmlibrary",
    "mousepad",
    "readonlymind",
    "giantessworld",
    "chastitymansion",
    "ticklingforum",
]
"""Site-picker options for the unified search window. The first entry
(``all``) triggers fan-out; everything else scopes to a single site."""

EROTICA_SITE_LABELS: dict[str, str] = {
    "all": "All erotica sites",
    "literotica": "Literotica",
    "ao3": "AO3 (Explicit)",
    "wattpad": "Wattpad",
    "aff": "Adult-FanFiction.org",
    "storiesonline": "StoriesOnline",
    "nifty": "Nifty",
    "sexstories": "SexStories",
    "mcstories": "MCStories",
    "lushstories": "Lushstories",
    "fictionmania": "Fictionmania",
    "tgstorytime": "TGStorytime",
    "chyoa": "Chyoa (interactive)",
    "darkwanderer": "Dark Wanderer",
    "greatfeet": "GreatFeet",
    "bdsmlibrary": "BDSM Library",
    "mousepad": "The Mousepad (forum)",
    "readonlymind": "ReadOnlyMind",
    "giantessworld": "Giantess World",
    "chastitymansion": "Chastity Mansion (forum)",
    "ticklingforum": "TicklingForum (forum)",
}


EROTICA_SORT: dict[str, str] = {
    # GUI dropdown label → sort mode. First entry is the default; the
    # GUI never sends it (index 0 means "no filter"), so
    # ``search_erotica`` only ever receives "Newest first" — or a bare
    # mode string from scripted callers.
    "Site & title": "site",
    "Newest first": "date",
}


def erotica_sort_mode(value) -> str:
    """Resolve a sort dropdown label or bare mode string to a mode
    (``"site"`` / ``"date"``). Unknown or empty input falls back to
    the default site-and-title ordering."""
    text = str(value or "").strip()
    if not text:
        return "site"
    if text in EROTICA_SORT:
        return EROTICA_SORT[text]
    lowered = text.lower()
    if lowered in EROTICA_SORT.values():
        return lowered
    return "site"


def sort_rows_by_updated(rows: list) -> list:
    """Newest-first ordering on each row's ``updated`` ISO stamp.

    Only forum-backed adapters populate ``updated`` (archives don't
    expose listing dates), so undated rows sort after every dated one.
    The sort is stable, so rows tied on date — and the whole undated
    block — keep their incoming site-and-title order.
    """
    return sorted(
        rows, key=lambda r: r.get("updated") or "", reverse=True,
    )

EROTICA_TAG_VOCABULARY: list[str] = [
    # The unified discovery axis. Most entries are general kinks
    # carried by ≥3 sites; a smaller refining-tag set targets
    # specific sub-interests (foot-worship under feet, pegging /
    # tease-and-denial / cfnm / strap-on / female-led / body-worship
    # under femdom, queening under cunnilingus) so users with a
    # narrower interest aren't forced to wade through the umbrella
    # tag's broader catalogue.
    "anal",
    "bdsm",
    "body-worship",
    "bondage",
    "bukkake",
    "celebrity",
    "cfnm",
    "cheating",
    "chastity",
    "cuckold",
    "cunnilingus",
    "dominance-submission",
    "exhibitionism",
    "face-sitting",
    "female-led",
    "femdom",
    "feet",
    "fisting",
    "foot-worship",
    "footjob",
    "futanari",
    "gangbang",
    "gay",
    "group-sex",
    "harem",
    "humiliation",
    "hypnosis",
    "incest",
    "interracial",
    "lactation",
    "lesbian",
    "masturbation",
    "mature",
    "mind-control",
    "non-consent",
    "oral",
    "orgy",
    "pegging",
    "polyamory",
    "pregnancy",
    "public-sex",
    "pussy-eating",
    "queening",
    "roleplay",
    "rough",
    "spanking",
    "squirting",
    "strap-on",
    "swinging",
    "tease-and-denial",
    "teen",
    "threesome",
    "trampling",
    "transgender",
    "voyeur",
    "watersports",
]
"""Tags exposed to the GUI multi-picker. Kept lowercase and
dash-joined so they drop straight into URL paths like
``/stories/bytag/<tag>``."""


# ── HTTP helper ──────────────────────────────────────────────────
#
# Search fetches use :class:`BaseScraper`'s fetch machinery so they
# pick up the same retry + rate-limit + Cloudflare-block detection
# that downloads already benefit from. The singleton sits at module
# scope so successive search calls reuse one HTTP session and one
# AIMD delay state — rate-limit bumps from one fan-out leak through
# to the next rather than resetting on every window open.
#
# Downside: one scraper instance for *all* search traffic means its
# cache_dir isn't useful (we never save anything from search), hence
# ``use_cache=False``. Rate-limit delays are capped lower than a
# download scraper's because search is one request per site per
# page — more requests per wall-second is fine as long as the cap
# catches us if a site starts 429-ing.

_SEARCH_FETCHER = BaseScraper(
    use_cache=False,
    delay_floor=0.0,
    delay_start=0.0,
    delay_ceiling=10.0,
    max_retries=3,
    timeout=REQUEST_TIMEOUT_S,
)


class SearchFetchError(RuntimeError):
    """Raised by :func:`_fetch` when the search HTTP fetch fails.

    The fan-out catches this per-site and marks the site as failed in
    ``site_stats``, surfacing the breakage to the user instead of
    silently reporting "0 results from <site>" the way the previous
    swallow-and-return-None contract did.
    """


def _fetch(url: str) -> str:
    """Return the response body for ``url``.

    Wraps :meth:`BaseScraper._fetch` so search requests get the same
    retry + 429/503 back-off + Cloudflare-block detection that
    downloads do. Raises :class:`SearchFetchError` on failure (the
    fan-out catches per-site, so one broken site doesn't kill the
    rest). Logs at WARNING so users debugging "search returns
    nothing" can actually see which sites' URLs are stale.
    """
    try:
        return _SEARCH_FETCHER._fetch(url)
    except Exception as exc:
        logger.warning("erotica search fetch %s failed: %s", url, exc)
        raise SearchFetchError(url) from exc


def _post(url: str, data: dict) -> str:
    """POST helper for the few sites whose search is form-driven
    (Sexstories). Same surface as :func:`_fetch` — raises
    :class:`SearchFetchError` so per-site failures are visible.
    """
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.post(
            url, data=data, impersonate="chrome", timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise SearchFetchError(f"{url} POST -> HTTP {resp.status_code}")
        return resp.text
    except SearchFetchError:
        raise
    except Exception as exc:
        logger.warning("erotica search POST %s failed: %s", url, exc)
        raise SearchFetchError(url) from exc


def _matches_query(query: str, *fields: str) -> bool:
    """Case-insensitive substring match used for client-side filtering
    of tag/category listings. Empty ``query`` returns True so tag-only
    browses show every row."""
    if not query:
        return True
    q = query.lower()
    for field in fields:
        if field and q in field.lower():
            return True
    return False


# Listing-date formats seen across the erotica archives, tried in
# order. Locale-dependent month/day names are fine here: the sites
# all render English dates.
_LISTING_DATE_FORMATS = (
    "%Y-%m-%d",             # 2026-07-10 (SOL noscript, ROM cards)
    "%b %d, %Y",            # Jul 8, 2026 (AFF)
    "%B %d %Y",             # July 10 2026 (GiantessWorld)
    "%B %d, %Y",            # July 10, 2026
    "%m/%d/%y",             # 07/09/26 (TGStorytime)
    "%d %B %Y",             # 04 July 2026 (MCStories dateline)
    "%A, %B %d, %Y",        # Thursday, April 9, 2026 (GreatFeet)
)


def _iso_date(raw: str) -> str:
    """Normalise a site-rendered listing date to ``YYYY-MM-DD``.

    Result rows carry ``updated`` as an ISO string (the GUI shows
    ``updated[:10]`` and ``--sort date`` compares lexically), so every
    site's native date format has to converge here. Returns ``""``
    when the input doesn't parse — an unknown format must degrade to
    "no date", not crash a whole listing page."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    from datetime import datetime
    for fmt in _LISTING_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


# ── Per-site tag-vocabulary translation ─────────────────────────────
#
# The unified ``EROTICA_TAG_VOCABULARY`` is the user-facing tag list;
# every site uses its own slugs / codes / titles. Each adapter looks
# its tag up here instead of passing the vocab tag through verbatim
# (which would mean every site that doesn't happen to use the same
# slug — most of them — gets a 0-result fallback page silently).
#
# Returning ``None`` from :func:`_translate_tag` is a signal that the
# site has no representation for that tag at all; callers treat that
# as "skip this site for this tag" rather than degrading to a default
# browse that would flood the result set with off-topic rows.
#
# Layout: ``_TAG_SLUGS[site][vocab_tag] = site_specific_slug``.
# Missing entries fall through to :func:`_translate_tag`'s default
# behaviour: vocab tag → passthrough for sites where the slug shape
# usually matches our vocab (literotica), or ``None`` for sites where
# an unmapped tag would never resolve (everything else).

_LITEROTICA_TAG_SLUGS: dict[str, str] = {
    # tags.literotica.com is permissive — most vocab tags are valid
    # slugs verbatim and fall through ``_translate_tag`` via the
    # passthrough path. Only override when Literotica uses a different
    # shape, or when we want to swap to the higher-volume sibling tag.
    # Verified May 2026: ``feet`` returns ~100 cards vs ``foot-fetish``
    # ~34, so we keep the broader slug. All refining tags below
    # confirmed alive with story cards on the live site.
    "body-worship": "body-worship",
    "cfnm": "cfnm",
    "female-led": "female-led-relationship",
    "foot-worship": "foot-worship",
    "footjob": "footjob",
    "pegging": "pegging",
    "pussy-eating": "pussy-eating",
    "queening": "queening",
    "squirting": "squirting",
    "strap-on": "strap-on",
    "tease-and-denial": "tease-and-denial",
    "trampling": "trampling",
}

_LUSH_TAG_SLUGS: dict[str, str] = {
    # Lushstories categories — verified live against
    # /stories/<slug>. Anything not here returns a stub page with
    # zero story anchors, so an explicit allowlist is mandatory.
    "anal": "anal",
    "bdsm": "bdsm",
    "bondage": "bdsm",          # no standalone bondage category
    "cheating": "cheating",
    "cuckold": "cuckold",
    "cunnilingus": "oral-sex",
    "dominance-submission": "bdsm",
    "exhibitionism": "exhibitionism",
    "face-sitting": "facesitting",  # Lush uses no hyphen
    "femdom": "femdom",
    "feet": "fetish",           # no feet-specific category
    "foot-worship": "fetish",   # subset of fetish; no narrower slug
    "footjob": "fetish",        # subset of fetish; no narrower slug
    "gangbang": "group-sex",
    "gay": "gay-male",
    "group-sex": "group-sex",
    "harem": "threesomes",
    "humiliation": "bdsm",
    "incest": "taboo",          # Lush's incest stand-in
    "interracial": "interracial",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "mind-control": "mind-control",
    "non-consent": "reluctance",
    "oral": "oral-sex",
    "pegging": "strap-on-sex",  # closest Lush bucket for pegging
    "polyamory": "wife-lovers",
    "public-sex": "exhibitionism",
    "queening": "facesitting",  # queening is a face-sitting variant
    "roleplay": "fantasy-sci-fi",
    "rough": "hardcore",
    "spanking": "spanking",
    "strap-on": "strap-on-sex",
    "swinging": "swingers",
    "teen": "teen",
    "threesome": "threesomes",
    "transgender": "trans",
    "voyeur": "voyeur",
    "watersports": "watersports",
}

_SOL_TAG_SLUGS: dict[str, str] = {
    # storiesonline.net /stories/bytag/<slug>. SOL uses joined-word
    # slugs (``femaledom``) where most sites use a hyphen, and serves
    # the all-tags index for any unrecognised slug — so passing
    # ``feet`` verbatim returned a 50 KB tag-index page that parsed as
    # zero stories. Refining tags that SOL doesn't carry (foot-worship,
    # tease-and-denial, cfnm, etc.) are absent here; ``_translate_tag``
    # returns ``None`` and the dispatcher skips SOL for those tags.
    "anal": "anal",
    "bdsm": "bdsm",
    "bondage": "bondage",
    "bukkake": "facial",
    "celebrity": "celebrity",
    "cheating": "cheating",
    "chastity": "chastity-belt",
    "cuckold": "cuckold",
    "cunnilingus": "oral-sex",
    "dominance-submission": "domsub",
    "exhibitionism": "exhibitionism",
    "feet": "foot-fetish",
    "foot-worship": "foot-fetish",   # SOL only has the umbrella slug
    "footjob": "foot-fetish",
    "femdom": "femaledom",
    "fisting": "fisting",
    "gangbang": "gangbang",
    "gay": "gay",
    "group-sex": "group-sex",
    "harem": "harem",
    "humiliation": "humiliation",
    "hypnosis": "hypnosis",
    "incest": "incest",
    "interracial": "interracial",
    "lactation": "lactation",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "mind-control": "mind-control",
    "non-consent": "rape",
    "oral": "oral-sex",
    "orgy": "orgy",
    "pegging": "pegging",            # verified live
    "polyamory": "polygamy",
    "pregnancy": "pregnancy",
    "public-sex": "exhibitionism",
    "queening": "oral-sex",          # closest SOL bucket
    "roleplay": "fan-fiction",
    "rough": "rough",
    "spanking": "spanking",
    "squirting": "squirting",        # verified live, 10 rows
    "swinging": "swinging",
    "teen": "school",
    "threesome": "ménage",
    "transgender": "transgender",
    "voyeur": "voyeur",
    "watersports": "water-sports",
}

_AO3_TAG_SLUGS: dict[str, str] = {
    # AO3 freeform tags are Title-Case with spaces. These are the
    # canonical AO3 tag names — passing them via the ``freeform``
    # filter on :func:`search_ao3` lands on the canonical tag page.
    # All refining tags verified live May 2026 — each /tags/<Name>
    # page returns its canonical 20-work batch.
    "anal": "Anal Sex",
    "bdsm": "BDSM",
    "body-worship": "Body Worship",
    "bondage": "Bondage",
    "bukkake": "Bukkake",
    "celebrity": "Celebrity Crush",
    "cfnm": "CFNM",
    "cheating": "Infidelity",
    "chastity": "Chastity Device",
    "cuckold": "Cuckolding",
    "cunnilingus": "Cunnilingus",
    "dominance-submission": "Dom/sub",
    "exhibitionism": "Exhibitionism",
    "face-sitting": "Face-Sitting",
    "female-led": "Female-Led Relationship",
    "femdom": "Femdom",
    "feet": "Foot Fetish",
    "fisting": "Fisting",
    "foot-worship": "Foot Worship",
    "footjob": "Footjob",
    "futanari": "Futanari",
    "gangbang": "Gangbang",
    "gay": "M/M",
    "group-sex": "Group Sex",
    "harem": "Harem",
    "humiliation": "Humiliation",
    "hypnosis": "Hypnotism",
    "incest": "Incest",
    "interracial": "Interracial Relationship(s)",
    "lactation": "Lactation",
    "lesbian": "F/F",
    "masturbation": "Masturbation",
    "mature": "Mature",
    "mind-control": "Mind Control",
    "non-consent": "Non-Consensual",
    "oral": "Oral Sex",
    "orgy": "Orgy",
    "pegging": "Pegging",
    "polyamory": "Polyamory",
    "pregnancy": "Pregnancy",
    "public-sex": "Public Sex",
    "pussy-eating": "Pussy Eating",
    "queening": "Queening",
    "roleplay": "Roleplay",
    "rough": "Rough Sex",
    "spanking": "Spanking",
    "squirting": "Squirting",
    "strap-on": "Strap-On Use",
    "swinging": "Swinging",
    "tease-and-denial": "Tease and Denial",
    "teen": "Underage",
    "threesome": "Threesome",
    "trampling": "Trampling",
    "transgender": "Trans Character",
    "voyeur": "Voyeurism",
    "watersports": "Watersports",
}

_WATTPAD_TAG_SLUGS: dict[str, str] = {
    # Wattpad's ``/stories/<slug>`` tag pages — verified live against
    # the JSON-LD ``ListItem`` embed. Wattpad's catalogue skews
    # romance/female-led; coverage is strong on the femdom side
    # (femdom=19, pegging=10, cfnm=4, female-led=5) but absent on the
    # cunnilingus side (every cunnilingus-adjacent slug 404s). Only
    # tags with verified non-empty pages are mapped here so the
    # adapter doesn't waste a fan-out slot on dead URLs.
    "bdsm": "bdsm",
    "body-worship": "body-worship",
    "bondage": "bondage",
    "celebrity": "celebrity",
    "cfnm": "cfnm",
    "cheating": "cheating",
    "chastity": "chastity",
    "cuckold": "cuckold",
    "dominance-submission": "dom-sub",
    "exhibitionism": "exhibitionism",
    "female-led": "female-led",
    "femdom": "femdom",
    "feet": "foot-fetish",
    "foot-worship": "foot-worship",
    "gangbang": "gangbang",
    "gay": "gay",
    "harem": "harem",
    "humiliation": "humiliation",
    "hypnosis": "hypnosis",
    "interracial": "interracial",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "non-consent": "noncon",
    "pegging": "pegging",
    "polyamory": "polyamory",
    "spanking": "spanking",
    "strap-on": "strap-on",
    "tease-and-denial": "tease",
    "teen": "teen",
    "threesome": "threesome",
    "trampling": "trampling",
    "transgender": "trans",
    "voyeur": "voyeur",
}

_BDSMLIB_TAG_CODES: dict[str, str] = {
    # BDSM Library encodes categories as numeric ``codeforstory[N]``
    # radio buttons on the advanced ``/stories/search.php`` form
    # (Yes/No/Maybe — submit ``yes`` for the targeted tag). The
    # numeric IDs are stable code-IDs in the site's DB. Verified May
    # 2026 by scraping the rendered form. Vocab tag → BDSM Library
    # numeric code ID.
    "bdsm": "71",                       # BDSM
    "bondage": "70",                    # bondage
    "chastity": "79",                   # chastity belt
    "dominance-submission": "38",       # D/s
    "exhibitionism": "21",              # exhibition
    "femdom": "13",                     # F/m — one female dominating one male
    "feet": "41",                       # feet
    "fisting": "23",                    # fisting
    "incest": "22",                     # incest
    "interracial": "24",                # interracial
    "lactation": "25",                  # lactation
    "non-consent": "86",                # Rape
    "spanking": "30",                   # spanking
    "teen": "31",                       # teen
    "transgender": "33",                # transgender
    "voyeur": "35",                     # voyeurism
    "watersports": "36",                # WaterSport
}

_SITE_TAG_SLUGS: dict[str, dict[str, str]] = {
    "literotica": _LITEROTICA_TAG_SLUGS,
    "lushstories": _LUSH_TAG_SLUGS,
    "storiesonline": _SOL_TAG_SLUGS,
    "ao3": _AO3_TAG_SLUGS,
    "wattpad": _WATTPAD_TAG_SLUGS,
    "bdsmlibrary": _BDSMLIB_TAG_CODES,
    # ``mcstories`` is wired in below where ``_MCS_TAG_CODES`` is
    # defined (it predates the translation layer).
}


# Sites whose slug shape is permissive enough that any vocab tag the
# user picks tends to resolve. Literotica's ``tags.literotica.com``
# subdomain happily returns ~100 cards for arbitrary slugs (verified:
# ``cheating``, ``cuckold``, ``transgender``, ``futanari``,
# ``chastity``, all → ~100 cards) so we don't need an exhaustive
# override table — anything not in :data:`_LITEROTICA_TAG_SLUGS`
# falls through to the vocab tag verbatim.
_LITEROTICA_PASSTHROUGH = True


def _translate_tag(site: str, vocab_tag: str) -> Optional[str]:
    """Return the site-specific slug for ``vocab_tag``, or ``None``
    when the site has no representation for the tag.

    Callers use ``None`` as a skip signal — don't fall back to a
    default browse (would flood results with off-topic rows) and
    don't fall back to the raw vocab tag (would build a URL the site
    silently 404s into a stub/index page).
    """
    if not vocab_tag:
        return None
    key = vocab_tag.strip().lower()
    if not key:
        return None
    site_map = _SITE_TAG_SLUGS.get(site)
    if site_map and key in site_map:
        return site_map[key]
    if site == "literotica" and _LITEROTICA_PASSTHROUGH:
        return key
    return None


# ── Per-site searches ────────────────────────────────────────────

def search_aff(query: str, *, page: int = 1, fandom: str = "",
               **_: object) -> list[dict]:
    """AFF has no site-wide search; each fandom subdomain offers a
    paginated ``index.php`` story listing. We grab the listing for
    the chosen fandom and filter client-side by the query.

    AFF retired ``story-list.php`` (404s as of 2025) — pagination
    moved to ``index.php?page=N``.

    AFF's catalog is partitioned by fandom subdomain
    (hp.adult-fanfiction.org, sw.adult-fanfiction.org, etc.) — there
    is no aggregated "all fandoms" view. Without an explicit
    ``fandom``, we'd have to guess which subdomain to query and any
    guess silently biases results to whichever fandom we picked. We
    used to default to ``"hp"`` which leaked Harry Potter results
    into every all-sites erotica search; return ``[]`` instead so the
    site stats panel makes it obvious AFF was skipped and the user
    can fill in the Fandom (AFF) field to opt in.
    """
    fandom = (fandom or "").strip().lower().strip(".")
    if not fandom:
        return []
    url = f"https://{fandom}.adult-fanfiction.org/index.php?page={page}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # index.php renders one div.story-entry card per story: title +
    # author anchors, div.story-description blurb, "Chapters: N" /
    # "Updated: Mon D, YYYY" meta items, a "Located: Fandom » Section"
    # breadcrumb, and WIP/COMPLETE/Oneshot status tags. (The author
    # profile fragments use a different card class — story-card — so
    # don't unify the two parsers.)
    for entry in soup.find_all("div", class_="story-entry"):
        a = entry.find("a", href=re.compile(r"story\.php\?no=\d+"))
        if a is None:
            continue
        href = a.get("href", "")
        m = re.search(r"no=(\d+)", href)
        if not m:
            continue
        story_id = m.group(1)
        title = a.get_text(" ", strip=True) or f"AFF {story_id}"
        full = f"https://{fandom}.adult-fanfiction.org/{href.lstrip('/')}"

        summary = ""
        desc = entry.find("div", class_="story-description")
        if desc:
            summary = desc.get_text(" ", strip=True)

        author = ""
        author_a = entry.find("a", class_="story-author")
        if author_a:
            author = author_a.get_text(" ", strip=True)

        entry_text = entry.get_text(" ", strip=True)
        ch_m = re.search(r"Chapters:\s*(\d+)", entry_text)
        upd_m = re.search(
            r"Updated:\s*([A-Za-z]{3} \d{1,2}, \d{4})", entry_text,
        )
        rating_m = re.search(r"Rated:\s*(Adult\s*\+*|Teen|All)", entry_text)

        status = ""
        tag_texts = [
            t.get_text(strip=True)
            for t in entry.find_all("span", class_="story-tag")
        ]
        if any(t.upper() == "COMPLETE" for t in tag_texts):
            status = "Complete"
        elif any(t.upper() == "WIP" for t in tag_texts):
            status = "In progress"

        location = ""
        loc = entry.find("div", class_="story-location")
        if loc:
            location = re.sub(
                r"^\s*Located\s*:?\s*", "", loc.get_text(" ", strip=True),
            )

        if not _matches_query(query, title, summary):
            continue
        out.append({
            "title": title, "author": author, "url": full,
            "summary": summary, "words": "?",
            "chapters": ch_m.group(1) if ch_m else "?",
            "rating": rating_m.group(1).strip() if rating_m else "M",
            "fandom": location or fandom, "status": status,
            "site": "aff",
            "updated": _iso_date(upd_m.group(1)) if upd_m else "",
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


def search_sol(query: str, *, page: int = 1, tags: Optional[list] = None,
               **_: object) -> list[dict]:
    """StoriesOnline: free-text search is paywalled, but ``/stories/bytag/<tag1:tag2>``
    browses are free and have rich metadata in the result rows. If
    the caller passed one or more tags we translate them through
    :data:`_SOL_TAG_SLUGS` (SOL uses joined-word slugs like
    ``femaledom`` where most sites use a hyphen) and join with ``:``
    — SOL's AND operator. Tags that don't translate are dropped; if
    the resulting list is empty we return ``[]`` rather than falling
    back to the recent-works browse (which would flood results with
    off-topic rows).

    Without any tags, default to ``/library/new_stories.php`` as a
    recent-works browse and apply the query as a client-side title
    filter.
    """
    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if vocab_tags:
        translated = [_translate_tag("storiesonline", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        joined = ":".join(translated)
        url = f"https://storiesonline.net/stories/bytag/{joined}"
        if page > 1:
            url += f"/{page}"
    else:
        url = "https://storiesonline.net/library/new_stories.php"
        if page > 1:
            url += f"?p={page}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen_ids = set()
    # SOL renders rows like:
    #   <div class="entry" id="sr<id>">
    #     <h3 class="sname">N <a href="/n/<id>/<slug>">Title</a>
    #                         by <a href="/a/<author>">Author</a></h3>
    #     <div class="sdesc">[series/universe span.help banners] blurb</div>
    #     <div class="misc"><b>Size:</b> 32KB | 6,022 words | ...</div>
    #   </div>
    # Title hrefs mix two shapes on the same listing — /s/<id>/<slug>
    # and /n/<id>/<slug> (story-page redirect). Accept both: matching
    # only one silently drops the rows using the other (an /n/-only
    # match returned 3 of 10 rows on a bytag page).
    for h3 in soup.find_all("h3", class_="sname"):
        anchors = h3.find_all("a", href=True)
        if len(anchors) < 1:
            continue
        title_a = anchors[0]
        m = re.match(r"^/[ns]/(\d+)/([^/?#\s]+)", title_a.get("href", ""))
        if not m:
            continue
        story_id, slug = m.group(1), m.group(2)
        if story_id in seen_ids:
            continue
        title = title_a.get_text(" ", strip=True)
        if not title or len(title) < 3:
            continue
        author = ""
        if len(anchors) >= 2:
            a_href = anchors[1].get("href", "")
            if a_href.startswith("/a/"):
                author = anchors[1].get_text(" ", strip=True)

        summary = ""
        words = "?"
        status = ""
        updated = ""
        entry = h3.find_parent("div", class_="entry")
        if entry is not None:
            sdesc = entry.find("div", class_="sdesc")
            if sdesc is not None:
                # Serial/universe banners ("A <Series> Story", "Part of
                # the <U> universe") prefix the blurb in span.help —
                # drop them so the summary starts with the synopsis.
                for span in sdesc.find_all("span", class_="help"):
                    span.decompose()
                summary = sdesc.get_text(" ", strip=True)
            misc = entry.find("div", class_="misc")
            if misc is not None:
                misc_text = misc.get_text(" ", strip=True)
                w_m = re.search(r"([\d,]+)\s*words\b", misc_text)
                if w_m:
                    words = w_m.group(1)
                d_m = re.search(r"(\d{4}-\d{2}-\d{2})", misc_text)
                if d_m:
                    updated = d_m.group(1)
            ab = entry.find("span", class_="ab")
            if ab is not None and "progress" in ab.get_text(" ", strip=True).lower():
                status = "In progress"

        if not _matches_query(query, title, slug, summary):
            continue
        seen_ids.add(story_id)
        out.append({
            "title": title, "author": author,
            "url": f"https://storiesonline.net/s/{story_id}/{slug}",
            "summary": summary, "words": words, "chapters": "?",
            "rating": "M", "fandom": "", "status": status,
            "site": "storiesonline",
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


# ── MCStories whole-archive title index (keyword search) ─────────
#
# MCStories has no search endpoint (verified: the homepage carries no
# search form). Its only whole-archive surface is the A-Z title index
# at /Titles/<L>.html — 26 letter pages, each a list of div.story
# blocks with title, author, synopsis, tag codes, and an "Added" date.
# Free-text search walks that index once, caches the parsed rows
# module-level, and filters them client-side. The previous target,
# WhatsNew.html, only carried ~a week of recent additions, so a keyword
# search matched a handful of stories out of the whole archive.

_MCS_TITLE_PAGE_URL = "https://mcstories.com/Titles/{letter}.html"
# The archive only gains stories weekly; a multi-hour TTL keeps the
# cached index fresh without re-crawling ~26 multi-MB pages per search.
_MCS_TITLE_INDEX_TTL_S = 6 * 3600
_MCS_TITLE_ANCHOR_RE = re.compile(
    r"^(?:\.\./)?([A-Z][A-Za-z0-9_-]+)/(?:index\.html)?$",
)
_MCS_CODE_LINE_RE = re.compile(r"^[a-z]{2}(?: [a-z]{2})*$")

_mcs_title_index: dict = {"built_at": 0.0, "rows": []}
_mcs_title_index_lock = threading.Lock()


def _mcs_parse_title_row(div) -> Optional[dict]:
    """Parse one ``div.story`` block from a ``/Titles/<L>.html`` letter
    page. Returns ``None`` for blocks with no story-link anchor (page
    chrome). Letter-page blocks differ from WhatsNew's: hrefs carry a
    ``../`` prefix and the tag codes sit on their own bare line
    (``mc mf fd``) rather than parenthesised in the byline."""
    anchor = next(
        (a for a in div.find_all("a", href=True)
         if _MCS_TITLE_ANCHOR_RE.match(a["href"])),
        None,
    )
    if anchor is None:
        return None
    slug = _MCS_TITLE_ANCHOR_RE.match(anchor["href"]).group(1)

    author = ""
    author_a = div.find("a", href=re.compile(r"(?:\.\./)?Authors/"))
    if author_a is not None:
        author = author_a.get_text(" ", strip=True)

    codes = ""
    for d in div.find_all("div"):
        text = d.get_text(" ", strip=True)
        if _MCS_CODE_LINE_RE.match(text):
            codes = text
            break

    summary = ""
    synopsis = div.find("div", class_="synopsis")
    if synopsis is not None:
        summary = synopsis.get_text(" ", strip=True)

    updated = ""
    ctime = div.find("div", class_="ctime")
    if ctime is not None:
        # "Added 07 February 2015" — drop the label, parse the date.
        updated = _iso_date(
            re.sub(r"^\s*Added\s+", "", ctime.get_text(" ", strip=True)),
        )

    return {
        "slug": slug,
        "title": anchor.get_text(" ", strip=True),
        "author": author,
        "codes": codes,
        "summary": summary,
        "updated": updated,
    }


_MCS_INDEX_FETCH_WORKERS = 8


def _mcs_build_title_index() -> list[dict]:
    """Fetch and parse all 26 A-Z title pages into a deduped row list.
    A letter page that fails to fetch is skipped so one dead page
    doesn't sink the whole index."""
    letters = list(string.ascii_uppercase)

    def _fetch_letter(letter: str) -> Optional[str]:
        url = _MCS_TITLE_PAGE_URL.format(letter=letter)
        try:
            return _fetch(url)
        except SearchFetchError:
            logger.warning("mcstories title index: %s unreachable", url)
            return None

    # The letter pages are static CDN HTML, so fetch them concurrently:
    # a sequential crawl of 26 multi-MB pages is a ~12s one-time stall,
    # and the fan-out blocks on its slowest site. Bounded worker count,
    # and ``map`` preserves A-Z order so the deduped window is stable
    # across Load More pages.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_MCS_INDEX_FETCH_WORKERS,
    ) as ex:
        pages = list(ex.map(_fetch_letter, letters))

    rows: list[dict] = []
    seen: set[str] = set()
    for html in pages:
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for div in soup.find_all("div", class_="story"):
            row = _mcs_parse_title_row(div)
            if row is not None and row["slug"] not in seen:
                seen.add(row["slug"])
                rows.append(row)
    return rows


def _mcs_title_index_rows() -> list[dict]:
    """Return the cached whole-archive title index, rebuilding when it's
    empty or older than :data:`_MCS_TITLE_INDEX_TTL_S`. Serialised on a
    lock so two concurrent MCStories searches build the index once, not
    twice; a rebuild that comes back empty (transient site failure)
    keeps the previous good rows rather than clearing them."""
    now = time.time()
    with _mcs_title_index_lock:
        cache = _mcs_title_index
        if cache["rows"] and (now - cache["built_at"]) < _MCS_TITLE_INDEX_TTL_S:
            return cache["rows"]
        rows = _mcs_build_title_index()
        if rows or not cache["rows"]:
            cache["rows"] = rows
            cache["built_at"] = now
        return cache["rows"]


def _mcs_keyword_match(query: str, haystack: str) -> bool:
    """AND-of-terms: every whitespace-separated term in ``query`` must
    appear (case-insensitive substring) somewhere in ``haystack``.

    The shared :func:`_matches_query` demands the query as one
    contiguous substring, so a topical multi-word search like
    ``college sorority`` — whose words are real but never adjacent —
    returned nothing. Matching each term independently against the
    row's combined text (title + author + codes + synopsis) is what
    users mean by a keyword search."""
    terms = query.lower().split()
    if not terms:
        return True
    hay = haystack.lower()
    return all(term in hay for term in terms)


def search_mcstories(query: str, *, page: int = 1,
                     tags: Optional[list] = None, **_: object) -> list[dict]:
    """MCStories indexes every story by Dublin Core tag codes at
    ``/Tags/<code>.html``. The first query-supplied tag is translated
    through :data:`_MCS_TAG_CODES` (which mirrors MCStories' 26 real
    codes — earlier revisions silently mapped to wrong codes like
    ``cb`` = comic-book instead of cheating). Tag-only searches for
    tags MCStories doesn't carry return ``[]`` rather than the full
    Titles index — the dispatcher's tag-capability filter already
    drops MCS from a fan-out for unsupported tags, but defending the
    adapter keeps direct callers honest too.

    A free-text ``query`` (no tag) filters the cached whole-archive
    title index (:func:`_mcs_title_index_rows`); a bare browse with
    neither query nor tag falls back to the recent-additions page.
    """
    # MCStories serves one un-paginated listing; window it per page.
    window_start, window_end = _single_listing_window(page)
    first_tag = next((t for t in (tags or []) if t), "") or ""
    code = _MCS_TAG_CODES.get(first_tag.lower())
    if code:
        url = f"https://mcstories.com/Tags/{code}.html"
    elif first_tag and not query:
        return []
    elif query:
        # Free-text search: filter the cached whole-archive title index.
        # MCStories has no search endpoint and WhatsNew only lists the
        # last ~week, so anything but the most recent stories was
        # unfindable by keyword before.
        out = []
        for r in _mcs_title_index_rows():
            haystack = f"{r['title']} {r['author']} {r['codes']} {r['summary']}"
            if not _mcs_keyword_match(query, haystack):
                continue
            out.append({
                "title": r["title"], "author": r["author"],
                "url": f"https://mcstories.com/{r['slug']}/",
                "summary": r["summary"], "words": "?", "chapters": "?",
                "rating": "M", "fandom": r["codes"], "status": "",
                "site": "mcstories", "updated": r["updated"],
            })
            if len(out) >= window_end:
                break
        return out[window_start:]
    else:
        # Bare browse (no query, no tag): recent additions.
        url = "https://mcstories.com/WhatsNew.html"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen_slugs: set[str] = set()

    # WhatsNew.html renders one ``div.story`` block per story — title
    # anchor, "by <author>" line with the tag-code string, and a real
    # ``div.synopsis`` blurb, grouped under ``h3.dateline`` sections.
    # (The old anchor-regex walk also matched the page's nav links —
    # Titles/Authors/Tags/ReadersPicks — and emitted them as four
    # bogus rows at the top of every bare browse.)
    story_divs = soup.find_all("div", class_="story")
    if story_divs:
        for div in story_divs:
            a = div.find(
                "a", href=re.compile(r"^([A-Z][A-Za-z0-9_-]+)/(?:index\.html)?$"),
            )
            if not a:
                continue
            m = re.match(r"^([A-Z][A-Za-z0-9_-]+)/", a.get("href", ""))
            if not m:
                continue
            slug = m.group(1)
            if slug in seen_slugs:
                # Stories updated in both weekly sections appear twice;
                # the first (newest) block wins.
                continue
            title = a.get_text(" ", strip=True)

            summary = ""
            syn = div.find("div", class_="synopsis")
            if syn:
                summary = syn.get_text(" ", strip=True)

            author = ""
            author_a = div.find("a", href=re.compile(r"^Authors/"))
            if author_a:
                author = author_a.get_text(" ", strip=True)

            codes = ""
            if author_a is not None and author_a.parent is not None:
                codes_m = re.search(
                    r"\(([a-z][a-z ]*)\)\s*$",
                    author_a.parent.get_text(" ", strip=True),
                )
                if codes_m:
                    codes = codes_m.group(1)

            updated = ""
            section = div.find_parent("section")
            if section is not None:
                dateline = section.find("h3", class_="dateline")
                if dateline is not None:
                    updated = _iso_date(dateline.get_text(" ", strip=True))

            if not _matches_query(query, title, codes, summary):
                continue
            seen_slugs.add(slug)
            out.append({
                "title": title, "author": author,
                "url": f"https://mcstories.com/{slug}/",
                "summary": summary, "words": "?", "chapters": "?",
                "rating": "M", "fandom": codes, "status": "",
                "site": "mcstories",
                "updated": updated,
            })
            if len(out) >= window_end:
                break
        return out[window_start:]

    # Tag pages render bare two-column ``<tr>`` rows — title anchor
    # (``../Slug/`` href) and the code string. No synopsis, author, or
    # date columns exist there; the codes are the ceiling, so they
    # stand in for the summary.
    for row in soup.find_all("tr"):
        a = row.find(
            "a", href=re.compile(r"^\.\./([A-Z][A-Za-z0-9_-]+)/"),
        )
        if a is None:
            a = row.find(
                "a", href=re.compile(r"^([A-Z][A-Za-z0-9_-]+)/"),
            )
        if not a:
            continue
        href = a.get("href", "")
        m = re.match(r"^(?:\.\./)?([A-Z][A-Za-z0-9_-]+)/", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen_slugs:
            continue
        title = a.get_text(" ", strip=True)
        codes = ""
        tds = row.find_all("td")
        if len(tds) >= 2:
            codes = tds[1].get_text(" ", strip=True)
        if not _matches_query(query, title, codes):
            continue
        seen_slugs.add(slug)
        out.append({
            "title": title, "author": "",
            "url": f"https://mcstories.com/{slug}/",
            "summary": codes, "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "mcstories",
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


_MCS_TAG_CODES = {
    # ficary's unified tag vocabulary ↔ MCStories' two-letter codes.
    # Re-verified May 2026 against mcstories.com/Tags/index.html.
    # MCStories has only 26 codes; tags not represented here have no
    # MCStories tag at all and are dropped from the fan-out via
    # :func:`_site_handles_any_tag`.
    #
    # Earlier revisions silently mapped to the wrong code (``cb`` =
    # comic-book NOT cheating, ``ft`` = clothing-fetish NOT feet,
    # ``hu`` = humor NOT humiliation, ``gr`` = growth NOT group-sex,
    # ``hm`` = humiliation NOT hypnosis, ``la`` = lactation NOT
    # interracial, ``ma`` = masturbation NOT TG/futanari). Those
    # mappings sent users to wholly unrelated tag pages.
    "bondage": "bd", "bdsm": "bd",     # bd = bondage and/or discipline
    "dominance-submission": "ds",       # ds = dominance and/or submission
    "exhibitionism": "ex",              # ex = exhibitionism
    "femdom": "fd",                     # fd = female dominant
    "gay": "mm",                        # mm = male/male sex
    "lesbian": "ff",                    # ff = female/female sex
    "humiliation": "hm",                # hm = humiliation
    "hypnosis": "mc",                   # closest MCS tag (no hypnosis-specific code)
    "incest": "in",                     # in = incest
    "lactation": "la",                  # la = lactation
    "masturbation": "ma",               # ma = masturbation
    "mind-control": "mc",               # mc = mind control
    "non-consent": "nc",                # nc = non-consensual
    "watersports": "ws",                # ws = watersports
}

# Late-bind MCStories into the unified translation layer. Predates
# the layer, so it's structured as its own table; this just gives
# ``_translate_tag("mcstories", ...)`` access to the same lookup the
# adapter already uses.
_SITE_TAG_SLUGS["mcstories"] = _MCS_TAG_CODES


def search_lushstories(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       category: str = "", **_: object) -> list[dict]:
    """Lushstories is category-driven — every URL is ``/stories/<category>/...``.

    The unified tag → Lush category translation lives in
    :data:`_LUSH_TAG_SLUGS` (verified live against /stories/<slug>):
    Lush uses ``facesitting`` (no hyphen), ``oral-sex`` (not ``oral``),
    ``fetish`` as the umbrella for feet/foot interest, and has no
    standalone feet category. A vocab tag that doesn't translate
    returns ``[]`` rather than falling through to ``/stories/<vocab>``,
    which serves a 404-shaped 200 page (Next.js stub with no story
    anchors).

    An explicit ``category`` filter overrides tag translation — the
    GUI's Category text box is for users who already know Lush's
    slug. With neither tag nor category, default to the newest-
    stories listing.
    """
    cat = (category or "").strip().lower().strip("/").replace(" ", "-")
    if not cat and tags:
        first_tag = tags[0].strip().lower()
        translated = _translate_tag("lushstories", first_tag)
        if translated is None:
            return []
        cat = translated
    if cat:
        url = f"https://www.lushstories.com/stories/{cat}"
    else:
        # Bare browse: the /stories root is the recent-stories index
        # (the old ``/stories/new`` default is a Next.js shell with
        # zero story anchors).
        url = "https://www.lushstories.com/stories"
    if page > 1:
        url += f"?page={page}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen = set()
    # Each story is an <article> card: h2 title, author profile link
    # in the header, the author's one-liner in <em>, a footer stat
    # whose title attribute is the site's rounded count ("2.6k words"),
    # and a <time> whose title attribute is the absolute date. The
    # page-top carousel repeats a few stories as bare image anchors —
    # anchoring on <article> excludes it (it also produced the old
    # slug-derived Title Cased titles with no metadata).
    for card in soup.find_all("article"):
        link = card.find("a", href=re.compile(
            r"^/stories/([a-z0-9-]+)/([a-z0-9][a-z0-9-]+)$",
        ))
        if link is None:
            continue
        m = re.match(
            r"^/stories/([a-z0-9-]+)/([a-z0-9][a-z0-9-]+)$", link["href"],
        )
        found_cat, slug = m.group(1), m.group(2)
        if slug in seen:
            continue

        h2 = card.find("h2")
        title = (
            h2.get_text(" ", strip=True) if h2
            else slug.replace("-", " ").title()
        )

        author = ""
        header = card.find("header")
        if header is not None:
            for a in header.find_all("a", href=re.compile(r"^/profile/")):
                text = a.get_text(" ", strip=True)
                if text:  # first profile link is the text-empty avatar
                    author = text
                    break

        summary = ""
        em = card.find("em")
        if em is not None:
            summary = em.get_text(" ", strip=True)

        words = "?"
        updated = ""
        footer = card.find("footer")
        if footer is not None:
            for div in footer.find_all(attrs={"title": True}):
                w_m = re.match(
                    r"([\d.,]+k?)\s*words$", div["title"].strip(), re.I,
                )
                if w_m:
                    words = w_m.group(1)
                    break
        time_tag = card.find("time")
        if time_tag is not None and time_tag.get("title"):
            # title attr is US-locale "7/6/2026, 3:35:16 PM".
            t_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", time_tag["title"])
            if t_m:
                updated = (
                    f"{t_m.group(3)}-{int(t_m.group(1)):02d}"
                    f"-{int(t_m.group(2)):02d}"
                )

        if not _matches_query(query, title, slug, summary):
            continue
        seen.add(slug)
        out.append({
            "title": title,
            "author": author,
            "url": f"https://www.lushstories.com{link['href']}",
            "summary": summary, "words": words, "chapters": "?",
            "rating": "M", "fandom": found_cat, "status": "",
            "site": "lushstories",
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


def search_sexstories(query: str, *, page: int = 1,
                      tags: Optional[list] = None, **_: object) -> list[dict]:
    """SexStories (XNXX Stories) drives its search via a POST form at
    ``/search/`` (field name ``search``); the previous GET form
    ``?search_story=...`` returns 404 as of 2025. An empty query
    falls back to the homepage grid sorted by most-recent.

    Tags supplied through the unified vocabulary land in the query
    string — SexStories' tag-vocabulary is fuzzy enough that including
    them as search terms gives reasonable relevance without us
    maintaining a per-tag URL table.
    """
    query_terms = [query] if query else []
    if tags:
        query_terms.extend(tags[:3])  # top 3 tags — beyond that hits
        # SexStories' relevance noise floor.
    combined = " ".join(t for t in query_terms if t).strip()
    # The POST search serves one un-paginated result set (page
    # navigation isn't exposed in the form) — window it per fan-out
    # page. The tag-less homepage browse paginates natively, so its
    # window stays at page 1's shape.
    window_start, window_end = _single_listing_window(page)
    if combined:
        # POST to /search/ with the form's actual field names.
        html = _post(
            "https://www.sexstories.com/search/",
            data={"search": combined, "type": "story"},
        )
    else:
        window_start, window_end = _single_listing_window(1)
        url = "https://www.sexstories.com/"
        if page > 1:
            url += f"?pd_page={page}"
        html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen = set()
    # Both the homepage grid and the POST results render one <li> per
    # story inside ul.stories_list: an <h4> holding the title anchor
    # and the author's /profile link, an optional «guillemet-wrapped»
    # author blurb in <p>, "Rated <strong>96.6%</strong>" / "Posted
    # <strong>date</strong>" stats, and the category+tag line after a
    # <br>. Section "More..." rows carry no h4 and are skipped.
    for li in soup.select("ul.stories_list > li"):
        link = li.select_one("h4 a[href^='/story/']")
        if link is None:
            continue
        m = re.match(r"^/story/(\d+)/([a-z0-9_-]+)", link.get("href", ""))
        if not m:
            continue
        story_id, slug = m.group(1), m.group(2)
        if story_id in seen:
            continue
        title = (
            link.get_text(" ", strip=True)
            or slug.replace("_", " ").replace("-", " ").title()
        )

        author = ""
        author_a = li.select_one("h4 a[href^='/profile']")
        if author_a is not None:
            author = author_a.get_text(" ", strip=True)

        summary = ""
        p = li.find("p")
        if p is not None:
            summary = p.get_text(" ", strip=True).strip("\xab\xbb ").strip()

        li_text = li.get_text(" ", strip=True)
        rating = "M"
        r_m = re.search(r"Rated\s+([\d.]+%)", li_text)
        if r_m:
            rating = r_m.group(1)
        updated = ""
        d_m = re.search(
            r"Posted\s+(?:\w{3}\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+of\s+"
            r"(\w+)\s+(\d{4})", li_text,
        )
        if d_m:
            updated = _iso_date(
                f"{d_m.group(2)} {d_m.group(1)}, {d_m.group(3)}",
            )

        category = ""
        br = li.find("br")
        if br is not None:
            tail = " ".join(
                s.strip() for s in br.find_next_siblings(string=True)
                if s.strip()
            )
            category = tail.split(",")[0].strip()

        # With a combined query we already filtered server-side, so
        # accept every result. Without one (tag-less browse) keep the
        # old client-side filter to avoid mixing in every row the
        # homepage happens to carry.
        if not combined and not _matches_query(query, slug, title, summary):
            continue
        seen.add(story_id)
        out.append({
            "title": title, "author": author,
            "url": f"https://www.sexstories.com/story/{story_id}/{slug}",
            "summary": summary, "words": "?", "chapters": "?",
            "rating": rating, "fandom": category, "status": "",
            "site": "sexstories",
            "updated": updated,
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


_NIFTY_TAG_CATEGORIES = {
    "gay": "gay", "lesbian": "lesbian", "bisexual": "bisexual",
    "transgender": "transgender",
}


def search_nifty(query: str, *, page: int = 1,
                 tags: Optional[list] = None,
                 category: str = "", **_: object) -> list[dict]:
    """Nifty doesn't have full-text search. The category directory
    at ``/nifty/<category>/`` is a plain-HTML link list of story
    subdirectories. Only a few sexuality categories map cleanly to
    our unified tag vocabulary (see :data:`_NIFTY_TAG_CATEGORIES`);
    other tags return ``[]`` so a tag-only ``bdsm`` search doesn't
    silently default to the ``/gay/`` directory."""
    # One un-paginated directory listing; window it per page.
    window_start, window_end = _single_listing_window(page)
    cat = (category or "").strip().strip("/").lower()
    if not cat and tags:
        cat = _NIFTY_TAG_CATEGORIES.get(tags[0].lower(), "")
        if not cat and not query:
            return []
    if not cat:
        cat = "gay"
    url = f"https://www.nifty.org/nifty/{cat}/"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Nifty switched its directory listings from relative hrefs
    # (``college/``) to absolute (``/nifty/gay/college/``) in mid-2026;
    # accept both shapes and key on the final path segment.
    href_re = re.compile(
        rf"^(?:/nifty/{re.escape(cat)}/)?[a-z0-9_-]+/$", re.I,
    )
    for a in soup.find_all("a", href=href_re):
        href = a.get("href", "")
        slug = href.strip("/").rsplit("/", 1)[-1]
        title = a.get_text(" ", strip=True) or slug.replace("-", " ").title()
        if not _matches_query(query, title, slug):
            continue
        out.append({
            "title": title, "author": "",
            "url": f"https://www.nifty.org/nifty/{cat}/{slug}/",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": cat, "status": "",
            "site": "nifty",
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


_FM_RSS_TITLE_RE = re.compile(
    # "Jul10 - <image marker> A Perfect Housewife [Pollymeric]" —
    # strip the date prefix, any non-title marker glyphs, and the
    # trailing [Author] (the <author> element carries it cleanly).
    r"^(?:[A-Z][a-z]{2}\d{1,2}\s*-\s*)?(?P<t>.*?)(?:\s*\[[^\]]*\])?$",
)


def search_fictionmania(query: str, *, page: int = 1,
                        **_: object) -> list[dict]:
    """Fictionmania's 2026 rework retired every server-side listing we
    used: ``recent.html`` and the ``searchdisplay`` WebDNA endpoint
    both return one-byte HTTP-200 stubs, and ``enter.html`` only
    renders five stories. The RSS feed (``fm.xml``) is the real
    "newest stories" surface — 25 items with title, author, synopsis,
    a ``readhtmlstory.html`` reader link (large-int ids work
    unchanged), and a pubDate that feeds the date sort. Queries filter
    it client-side."""
    # One un-paginated result listing; window it per page.
    window_start, window_end = _single_listing_window(page)
    xml = _fetch("https://fictionmania.tv/fm.xml")
    if not xml:
        return []
    out: list[dict] = []
    seen = set()
    for item in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = item.group(1)

        def _field(tag: str) -> str:
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
            return (m.group(1) if m else "").strip()

        link = _field("link")
        id_m = re.search(r"storyID=(\d+)", link)
        if not id_m:
            continue
        story_id = id_m.group(1)
        raw_title = _field("title")
        title_m = _FM_RSS_TITLE_RE.match(raw_title)
        title = (title_m.group("t") if title_m else raw_title).strip()
        # ISO-8859-1 marker glyphs (the "story with images" flag)
        # survive decoding as junk — drop non-ASCII leaders.
        title = re.sub(r"^[^\w\"']+", "", title) or raw_title
        author = _field("author")
        summary = _field("description")
        updated = ""
        pub = _field("pubDate")
        if pub:
            try:
                from email.utils import parsedate_to_datetime
                updated = parsedate_to_datetime(pub).strftime(
                    "%Y-%m-%dT%H:%M:%S",
                )
            except (TypeError, ValueError):
                pass
        if not _matches_query(query, title, author, summary):
            continue
        if story_id in seen:
            continue
        seen.add(story_id)
        out.append({
            "title": title or f"Fictionmania {story_id}",
            "author": author,
            "url": (
                f"https://fictionmania.tv/stories/readhtmlstory.html"
                f"?storyID={story_id}"
            ),
            "summary": summary, "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "fictionmania",
            "updated": updated,
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


def search_tgstorytime(query: str, *, page: int = 1,
                       **_: object) -> list[dict]:
    """TGStorytime category browse. No free-text search endpoint —
    the newest-stories page is the easiest-to-scrape starting point;
    we filter by query client-side.

    Each story link is wrapped in an ``onclick="confirm(age_consent)"``
    JavaScript shim for anonymous visitors; our age-consent query
    params bypass it cleanly at fetch time so we just match the
    bare ``viewstory.php?sid=<N>`` pattern."""
    # The age-consent interstitial gates the homepage and index.php
    # for anonymous visitors too — without these query params the
    # listing HTML is the consent gate itself, not the story rows.
    age_qs = "ageconsent=ok&warning=3"
    if page > 1:
        url = f"https://www.tgstorytime.com/index.php?page={page}&{age_qs}"
    else:
        url = f"https://www.tgstorytime.com/?{age_qs}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    # The newest-stories block renders one div per story — class
    # ``showcase`` (completed) or ``inprogress`` — whose div.content
    # holds two span.sublabel runs (title+author links; date/rating/
    # category/status meta) with the summary as bare text nodes
    # between them. Anchoring on those classes also drops the
    # sidebar "Random Story" block that the old flat anchor walk
    # emitted as a bogus first row.
    story_rows = soup.select("div.showcase, div.inprogress")
    for row in story_rows:
        content = row.find("div", class_="content")
        if content is None:
            continue
        a = content.find("a", href=re.compile(r"viewstory\.php\?sid=\d+"))
        if a is None:
            continue
        m = re.search(r"sid=(\d+)", a.get("href") or "")
        if not m:
            continue
        sid = m.group(1)
        title = a.get_text(" ", strip=True)
        if sid in seen or not title or len(title) < 3:
            continue

        author = ""
        author_a = content.find("a", href=re.compile(r"viewuser\.php\?uid=\d+"))
        if author_a is not None:
            author = author_a.get_text(" ", strip=True)

        # Summary = the text nodes that are direct children of
        # div.content (between the two sublabel spans).
        summary = re.sub(
            r"\s+", " ",
            " ".join(content.find_all(string=True, recursive=False)),
        ).strip()

        rating = "M"
        updated = ""
        fandom = ""
        labels = content.find_all("span", class_="sublabel")
        if len(labels) >= 2:
            meta_text = labels[1].get_text(" ", strip=True)
            d_m = re.match(r"\s*(\d{2}/\d{2}/\d{2})", meta_text)
            if d_m:
                updated = _iso_date(d_m.group(1))
            parts = [p.strip() for p in meta_text.split(",")]
            if len(parts) >= 2 and parts[1]:
                rating = parts[1]
            cat_a = labels[1].find("a", href=re.compile(r"type=categories"))
            if cat_a is not None:
                fandom = cat_a.get_text(" ", strip=True)

        status = (
            "Complete" if "showcase" in (row.get("class") or [])
            else "In progress"
        )

        if not _matches_query(query, title, summary):
            continue
        seen.add(sid)
        out.append({
            "title": title, "author": author,
            "url": (
                f"https://www.tgstorytime.com/viewstory.php"
                f"?sid={sid}&ageconsent=ok&warning=3"
            ),
            "summary": summary, "words": "?", "chapters": "?",
            "rating": rating, "fandom": fandom, "status": status,
            "site": "tgstorytime",
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    if story_rows:
        return out
    # Deeper index.php pages may not use the showcase/inprogress
    # homepage blocks — fall back to the flat anchor walk (titles
    # only) rather than reporting a silently empty page.
    for a in soup.find_all("a", href=re.compile(r"viewstory\.php\?sid=\d+")):
        m = re.search(r"sid=(\d+)", a.get("href") or "")
        if not m:
            continue
        sid = m.group(1)
        title = a.get_text(" ", strip=True)
        if sid in seen or not title or len(title) < 3:
            continue
        if not _matches_query(query, title):
            continue
        seen.add(sid)
        out.append({
            "title": title, "author": "",
            "url": (
                f"https://www.tgstorytime.com/viewstory.php"
                f"?sid={sid}&ageconsent=ok&warning=3"
            ),
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "tgstorytime",
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


_CHYOA_TAG_CATEGORIES: dict[str, str] = {
    # Unified vocab tag → chyoa /category/<slug>. Only tags with a
    # native category page; everything else browses trending.
    "mind-control": "mind-control",
    "hypnosis": "mind-control",
    "bdsm": "bdsm",
}


def search_chyoa(query: str, *, page: int = 1,
                 tags: Optional[list] = None, **_: object) -> list[dict]:
    """Chyoa's ``/search/<q>`` endpoint returns chapter-level hits for
    a query; without a query we default to the browse-popular page
    filtered client-side.

    Story and chapter URLs share the numeric-id namespace (story .14
    and chapter .14 can be different works), so the dedup key is the
    ``(kind, numeric)`` pair — keying on the number alone would drop
    the second hit incorrectly."""
    listing_window = None
    if query:
        # Chyoa runs on Symfony — its router expects pagination as a
        # path segment, not a query parameter. The previous ``?page=N``
        # form was silently ignored, so every "page 2+" request returned
        # page 1 and the load-more button looped on identical results.
        # Quote spaces (and other unsafe chars) properly rather than
        # collapsing to ``+``, which Chyoa encodes literally as ``%2B``.
        from urllib.parse import quote
        url = f"https://chyoa.com/search/{quote(query, safe='')}"
        if page > 1:
            url += f"/page/{page}"
    else:
        # ``/browse/popular`` 404s since the mid-2026 relaunch. Tag
        # browses hit the native ``/category/<slug>`` pages where our
        # vocabulary maps to one; everything else lands on the
        # trending listing. Neither paginates, so both get the
        # single-listing window treatment.
        listing_window = _single_listing_window(page)
        cat = ""
        for t in tags or []:
            cat = _CHYOA_TAG_CATEGORIES.get(t.strip().lower(), "")
            if cat:
                break
        if cat:
            url = f"https://chyoa.com/category/{cat}"
        else:
            url = "https://chyoa.com/trending-sex-stories"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # The 2026 redesign renders one <article> card per story with
    # shared ``redesign-item__*`` parts: title anchor, byline link,
    # a real description <p>, and an icon-keyed meta row. Card-scoping
    # matters beyond the metadata: the old flat href regex also
    # matched the sidebar spotlight/guide links and footer chapter
    # links, which leaked site chrome into title-less browses.
    soup = BeautifulSoup(html, "lxml")
    cards = [
        card for card in soup.find_all("article")
        if card.select_one(
            ".redesign-item__title a[href*='/story/'], "
            ".redesign-item__title a[href*='/chapter/']",
        )
        and card.find_parent("aside") is None
    ]
    for card in cards:
        a = card.select_one(".redesign-item__title a")
        href = a.get("href") or ""
        m = re.search(r"/(story|chapter)/[^\"]*?\.(\d+)", href)
        if not m:
            continue
        kind, numeric = m.group(1), m.group(2)
        key = (kind, numeric)
        title = a.get_text(" ", strip=True)
        if key in seen or not title:
            continue

        author = ""
        byline = card.select_one(".redesign-item__byline a")
        if byline is not None:
            author = byline.get_text(" ", strip=True)

        summary = ""
        desc = card.find("p", class_="redesign-item__description")
        if desc is not None:
            summary = desc.get_text(" ", strip=True)

        chapters = "?"
        fandom = ""
        updated = ""
        meta = card.select_one(".redesign-item__meta")
        if meta is not None:
            # Trending cards state the story's total ("22 chapters");
            # search cards only give branch depth ("17 Chapters Deep"),
            # a different metric — don't map it to chapters.
            ch_m = re.search(
                r"([\d,]+)\s+chapters?\b(?!\s+deep)",
                meta.get_text(" ", strip=True), re.I,
            )
            if ch_m:
                chapters = ch_m.group(1)
            folder_icon = meta.find(attrs={"data-lucide": "folder"})
            if folder_icon is not None and folder_icon.parent is not None:
                fandom = folder_icon.parent.get_text(" ", strip=True)
        stamp = card.find(attrs={"data-livestamp": True})
        if stamp is not None:
            updated = str(stamp["data-livestamp"])[:10]

        # No client-side re-filter on the card path: with a query the
        # /search endpoint already filtered server-side (its relevance
        # hits routinely lack the literal term), and the browse paths
        # arrive query-less. Chrome links never reach here — cards are
        # structurally scoped.
        seen.add(key)
        full_url = (
            href if href.startswith("http") else f"https://chyoa.com{href}"
        )
        out.append({
            "title": title, "author": author,
            "url": full_url,
            "summary": summary, "words": "?", "chapters": chapters,
            "rating": "M", "fandom": fandom, "status": "",
            "site": "chyoa",
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break

    if not cards:
        # Unverified listing shapes (e.g. /category/<slug>) fall back
        # to the flat href walk rather than returning a silently empty
        # page. Chyoa renders search-result links as full URLs
        # (``href="https://chyoa.com/story/..."``), not relative
        # paths, so the host part stays optional in the regex.
        for m in re.finditer(
            r'href="(?:https?://chyoa\.com)?(/(story|chapter)/[^"]+?\.(\d+))"'
            r'[^>]*>([^<]+)<',
            html,
        ):
            href, kind, numeric, title = (
                m.group(1), m.group(2), m.group(3), m.group(4).strip(),
            )
            key = (kind, numeric)
            if key in seen or not title:
                continue
            if not _matches_query(query, title):
                continue
            seen.add(key)
            out.append({
                "title": title, "author": "",
                "url": f"https://chyoa.com{href}",
                "summary": "", "words": "?", "chapters": "?",
                "rating": "M", "fandom": "", "status": "",
                "site": "chyoa",
            })
            if len(out) >= PER_SITE_PAGE_MAX:
                break
    if listing_window is not None:
        start, end = listing_window
        return out[start:end]
    return out


def _search_xenforo_forum(
    query: str,
    page: int,
    *,
    site: str,
    listing_url: str,
    thread_href_re: re.Pattern,
    thread_url_template: str,
    meta_slug_prefixes: tuple[str, ...] = (),
    fandom: str = "",
) -> list[dict]:
    """Walk one page of a XenForo story-forum listing and filter
    client-side. Shared by Dark Wanderer, Chastity Mansion, and
    TicklingForum — guest keyword search is disabled or unreliable on
    all three boards, and the story forums paginate natively as
    ``page-N``. Rows come from the ``structItem--thread`` blocks
    (real titles, author, last-activity date); the flat href walk
    with slug-derived titles remains as a markup-drift fallback.

    Returns a :class:`SparsePage` when the query filtered out a page
    that still had threads, so the fan-out keeps paging.
    """
    suffix = f"page-{page}" if page > 1 else ""
    html = _fetch(listing_url + suffix)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    fetched_any = False

    # Preferred path: one div.structItem--thread per row. It carries
    # the REAL thread title (data-tp-primary anchor — the old slug
    # ``.title()`` reconstruction lost apostrophes, ampersands, and
    # casing), the author (row's data-author attribute), and ISO
    # timestamps. Thread listings expose no synopsis or word count —
    # the only preview is an AJAX endpoint, one request per thread,
    # which a listing walk must not multiply into.
    soup = BeautifulSoup(html, "lxml")
    for row in soup.select("div.structItem--thread"):
        classes = row.get("class") or []
        if any("sticky" in c for c in classes) or row.select_one(
            ".structItem-status--sticky",
        ):
            # Pinned housekeeping threads (FAQs, author notes) — the
            # slug blocklists caught only the known ones.
            continue
        title_a = row.select_one(".structItem-title a[data-tp-primary]")
        if title_a is None:
            title_a = row.select_one(".structItem-title a[href*='threads/']")
        if title_a is None:
            continue
        href = title_a.get("href") or ""
        m = re.search(r"threads/(?P<slug>[a-z0-9%-]+)\.(?P<tid>\d+)", href)
        if not m:
            continue
        slug, tid = m.group("slug"), m.group("tid")
        if tid in seen:
            continue
        seen.add(tid)
        if slug.startswith(meta_slug_prefixes):
            continue
        fetched_any = True
        title = title_a.get_text(" ", strip=True) or slug.replace("-", " ").title()

        author = row.get("data-author") or ""
        updated = ""
        latest = row.select_one("time.structItem-latestDate")
        if latest is not None and latest.get("datetime"):
            updated = str(latest["datetime"])[:10]

        if not _matches_query(query, title, slug):
            continue
        out.append({
            "title": title, "author": author,
            "url": thread_url_template.format(slug=slug, tid=tid),
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": fandom, "status": "",
            "site": site,
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break

    if not fetched_any:
        # Markup drift fallback: the old flat href walk over the raw
        # HTML (slug-derived titles, no metadata). XenForo emits both
        # bare thread links and per-post permalinks; dedupe by tid so
        # each thread counts once.
        for m in re.finditer(thread_href_re, html):
            slug, tid = m.group("slug"), m.group("tid")
            if tid in seen:
                continue
            seen.add(tid)
            if slug.startswith(meta_slug_prefixes):
                continue
            fetched_any = True
            title = slug.replace("-", " ").title()
            if not _matches_query(query, title, slug):
                continue
            out.append({
                "title": title, "author": "",
                "url": thread_url_template.format(slug=slug, tid=tid),
                "summary": "", "words": "?", "chapters": "?",
                "rating": "M", "fandom": fandom, "status": "",
                "site": site,
            })
            if len(out) >= PER_SITE_PAGE_MAX:
                break
    if not out and fetched_any:
        return SparsePage()
    return out


_DW_META_SLUG_PREFIXES = (
    # Pinned/housekeeping threads that live among the stories.
    "faq-for-authors", "note-for-authors", "anyone-looking-for",
    "looking-for-authors", "story-waiting-for-moderation",
    "stories-in-need-of",
)


def search_darkwanderer(query: str, *, page: int = 1,
                        **_: object) -> list[dict]:
    """Dark Wanderer: walk the Author's Den story forum (guest search
    is disabled server-side; the old ``/forums/`` browse leaked
    community threads)."""
    return _search_xenforo_forum(
        query, page,
        site="darkwanderer",
        listing_url="https://darkwanderer.net/forums/authors-den.5/",
        thread_href_re=re.compile(
            r'href="/threads/(?P<slug>[a-z0-9-]+)\.(?P<tid>\d+)'
            r'(?:/(?:post-\d+)?)?"',
            re.IGNORECASE,
        ),
        thread_url_template="https://darkwanderer.net/threads/{slug}.{tid}/",
        meta_slug_prefixes=_DW_META_SLUG_PREFIXES,
        fandom="cuckold",
    )


def search_chastitymansion(query: str, *, page: int = 1,
                           **_: object) -> list[dict]:
    """The Chastity Mansion: walk the Member Fiction forum. The board
    runs without friendly URLs, so listing and thread links take the
    ``index.php?threads/...`` form."""
    return _search_xenforo_forum(
        query, page,
        site="chastitymansion",
        listing_url=(
            "https://chastitymansion.com/forums/index.php"
            "?forums/member-fiction.19/"
        ),
        thread_href_re=re.compile(
            r'href="/forums/index\.php\?threads/'
            r'(?P<slug>[a-z0-9-]+)\.(?P<tid>\d+)(?:/(?:post-\d+)?)?"',
            re.IGNORECASE,
        ),
        thread_url_template=(
            "https://chastitymansion.com/forums/index.php"
            "?threads/{slug}.{tid}/"
        ),
        fandom="chastity",
    )


_TMF_META_SLUG_PREFIXES = (
    "story-posting-rules", "about-minors", "the-decorum-of",
    "story-index", "welcome-to",
)


def search_ticklingforum(query: str, *, page: int = 1,
                         **_: object) -> list[dict]:
    """TicklingForum (TMF): walk the main Tickling Stories forum."""
    return _search_xenforo_forum(
        query, page,
        site="ticklingforum",
        listing_url="https://www.ticklingforum.com/forums/tickling-stories.12/",
        thread_href_re=re.compile(
            r'href="/threads/(?P<slug>[a-z0-9%-]+)\.(?P<tid>\d+)'
            r'(?:/(?:post-\d+)?)?"',
            re.IGNORECASE,
        ),
        thread_url_template=(
            "https://www.ticklingforum.com/threads/{slug}.{tid}/"
        ),
        meta_slug_prefixes=_TMF_META_SLUG_PREFIXES,
        fandom="tickling",
    )


def search_greatfeet(query: str, *, page: int = 1,
                     tags: Optional[list] = None,
                     **_: object) -> list[dict]:
    """GreatFeet: ``/tickles.htm`` lists recent stories by ``ts<N>.htm``
    href; older issues at ``/archiveN.htm`` (weekly issues 1..484+).
    The page is 1997-era HTML (unclosed ``<a>`` tags, inline font
    styling) so we lean on BeautifulSoup to let it tolerate the
    malformed markup, then read the link text as the story title.

    Tag-only searches for anything other than ``feet`` return ``[]``
    — GreatFeet's whole catalogue is the feet tag, so a tag-only
    ``bdsm`` lookup would otherwise leak the entire homepage as
    noise.

    We decompose inline ``<img>`` tags inside the link before reading
    the text — the "new!" / "hot!" marker images carry alt attributes
    like ``"Foot Fetish Offering"`` that would otherwise pollute the
    title."""
    # tickles.htm is a single un-paginated page (archive pages handle
    # older stories via a separate ``/archive<N>.htm`` route); window
    # it per fan-out page.
    window_start, window_end = _single_listing_window(page)
    if (
        tags and not query
        and not any(t.strip().lower() == "feet" for t in tags)
    ):
        return []
    url = "https://www.greatfeet.com/tickles.htm"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen = set()
    for a in soup.find_all(
        "a", href=re.compile(r"/stories/ts(\d+)\.htm", re.I),
    ):
        m = re.search(r"/stories/ts(\d+)\.htm", a.get("href", ""))
        if not m:
            continue
        sid = m.group(1)
        if sid in seen:
            continue
        # Strip marker images (``<img src="new.gif">``, ``<img
        # src="hot.gif">``) before reading link text — their alt
        # attributes are not part of the story title.
        for img in list(a.find_all("img")):
            img.decompose()
        title = a.get_text(" ", strip=True)
        # Links that only carry a teaser image (no text after img
        # decompose) fall back to the id-derived placeholder so the
        # row still has a title the reader can scan.
        if not title:
            title = f"GreatFeet story {sid}"
        if not _matches_query(query, title):
            continue
        seen.add(sid)
        # No synopsis or word count exists anywhere in the listing —
        # each row's only other text is the formulaic "This story was
        # published for the update of <date>." sentence. Lift the date.
        updated = ""
        parent_p = a.find_parent("p")
        if parent_p is not None:
            d_m = re.search(
                r"for the update of\s+([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},"
                r"\s+\d{4})",
                parent_p.get_text(" ", strip=True), re.I,
            )
            if d_m:
                updated = _iso_date(d_m.group(1))
        out.append({
            "title": title, "author": "Anonymous",
            "url": f"https://www.greatfeet.com/stories/ts{sid}.htm",
            "summary": "", "words": "?", "chapters": "1",
            "rating": "M", "fandom": "feet", "status": "",
            "site": "greatfeet",
            "updated": updated,
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


def search_literotica_wrapped(query: str, *, page: int = 1,
                              tags: Optional[list] = None,
                              **_: object) -> list[dict]:
    """Thin wrapper around :func:`ficary.search.search_literotica` that
    maps our unified ``tags`` input onto Literotica's ``category``
    argument and tags every row with ``site='literotica'``.

    Query + tag together: Literotica's tag-browse endpoint
    (``tags.literotica.com/<tag>``) ignores any free-text query — the
    URL IS the tag listing. ``search_literotica`` therefore drops the
    query whenever ``category`` is set, which silently returned
    unfiltered tag results for a "Harry Potter + bdsm" search.
    Post-filter the tag page on title/author/summary/fandom locally
    so query+tag behaves like "tag-browse narrowed by query".

    Tag translation goes through :func:`_translate_tag` — for
    Literotica this is mostly passthrough (the ``tags.literotica.com``
    subdomain is permissive about slug shape), but
    :data:`_LITEROTICA_TAG_SLUGS` overrides specific vocab tags to
    Literotica's preferred slug where they diverge.
    """
    category = ""
    if tags:
        first_tag = tags[0].strip().lower()
        translated = _translate_tag("literotica", first_tag)
        if translated is None:
            return []
        category = translated

    if not query and not category:
        # Bare site browse: literotica.com/new is server-rendered with
        # the newest ~80 stories (the JS search needs a query and the
        # tag endpoints need a slug, so neither covers "just show me
        # the site"). One un-paginated listing — window it per page.
        #
        # /new uses the same story-card markup as the tag-browse
        # pages, so reuse the production card parser — it yields the
        # real summary/author/rating/category per row, decodes
        # entities, and skips the sidebar-widget links the old inline
        # href regex emitted as title-only noise rows.
        window_start, window_end = _single_listing_window(page)
        html = _fetch("https://www.literotica.com/new")
        if not html:
            return []
        out = _parse_literotica_results(html)
        for r in out:
            r["site"] = "literotica"
        return out[window_start:window_end]

    kwargs: dict = {}
    if category:
        kwargs["category"] = category
    # Fetch/HTTP errors propagate to the fan-out, which records them in
    # ``site_stats`` — swallowing them here showed "0 results, ok" for
    # a site that actually failed.
    results = search_literotica(query, page=page, **kwargs)
    if query and category:
        results = [
            r for r in results
            if _matches_query(
                query,
                r.get("title", ""), r.get("author", ""),
                r.get("summary", ""), r.get("fandom", ""),
            )
        ]
    for r in results[:PER_SITE_PAGE_MAX]:
        r["site"] = "literotica"
    return results[:PER_SITE_PAGE_MAX]


def search_ao3_erotica(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       **_: object) -> list[dict]:
    """AO3 adapter for the erotica fan-out — restricts to Explicit
    rating and translates the first vocab tag to AO3's canonical
    freeform tag (see :data:`_AO3_TAG_SLUGS`).

    Why this lives in the erotica module instead of the existing
    :func:`ficary.search.search_ao3`: the general AO3 search returns
    every rating, every fandom, every length. For the erotica
    discovery surface we want the same Explicit-only narrow that
    Literotica's tag-browse gives us by construction. The thin
    wrapper here pins ``rating='explicit'`` and routes vocab tags
    through the freeform field so e.g. ``feet`` → ``Foot Fetish``
    rather than landing in a free-text query that AO3's relevance
    sort can't make tight enough.

    Tags AO3 doesn't have a canonical name for return ``[]`` so the
    fan-out doesn't paper over noise with off-rating fandom hits.
    """
    from ..search import search_ao3

    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if vocab_tags:
        translated = [_translate_tag("ao3", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        # AO3's freeform_names is a free-text field; comma-separated
        # values are AND-combined by the AO3 search engine.
        freeform_arg = ", ".join(translated)
    else:
        freeform_arg = ""

    # A bare call (no query, no tag) is a deliberate site browse now —
    # "every Explicit work, best first" — so it proceeds instead of
    # returning []. The old firehose worry is handled upstream: the
    # tag-capability filter keeps AO3 out of unrelated tag fan-outs,
    # and an all-sites bare browse legitimately wants AO3's slice.
    kwargs = {"rating": "explicit", "sort": "kudos"}
    if freeform_arg:
        kwargs["freeform"] = freeform_arg
    # Fetch/HTTP errors (e.g. AO3's anti-bot 403) propagate to the
    # fan-out, which records them in ``site_stats`` — swallowing them
    # here showed "0 results, ok" for a site that actually failed.
    results = search_ao3(query or "", page=page, **kwargs)
    for r in results[:PER_SITE_PAGE_MAX]:
        r["site"] = "ao3"
    return results[:PER_SITE_PAGE_MAX]


def search_wattpad_erotica(query: str, *, page: int = 1,
                           tags: Optional[list] = None,
                           **_: object) -> list[dict]:
    """Wattpad adapter for the erotica fan-out — uses Wattpad's
    ``/stories/<tag>`` HTML tag page rather than the v4 API search
    that :func:`ficary.search.search_wattpad` hits, because the tag
    page returns a directly-targeted JSON-LD ``ItemList`` block whose
    relevance is far tighter than an API free-text query for the same
    tag string.

    Translates vocab tags via :data:`_WATTPAD_TAG_SLUGS` (Wattpad
    uses ``foot-fetish`` / ``noncon`` / ``trans`` / ``dom-sub`` —
    slug shapes that don't match our vocab). Tags not in the table
    return ``[]`` rather than landing on Wattpad's stub 404 page.

    Wattpad's tag pages embed up to ~20 ``ListItem`` JSON-LD entries
    and don't paginate — multi-page browses would require a different
    (auth-gated) Wattpad endpoint — so the single listing is windowed
    per fan-out page like the other one-listing sites.
    """
    window_start, window_end = _single_listing_window(page)
    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    slug = None
    if vocab_tags:
        translated = [_translate_tag("wattpad", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        slug = translated[0]

    if not slug and not query:
        # Bare browse: Wattpad's ``adult`` tag page is the closest
        # live umbrella listing (``erotica``/``mature`` 404 as tag
        # pages).
        slug = "adult"
    if not slug:
        # Free-text fallback uses Wattpad's slug-shape on the query
        # (lowercase, hyphens). Wattpad serves a search-results page
        # for arbitrary text inputs, but the tag-page format is what
        # this adapter parses; for a bare query, fold it into the
        # slug and let Wattpad decide whether to redirect or 404.
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        if not slug:
            return []

    url = f"https://www.wattpad.com/stories/{slug}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    # JSON-LD ``ListItem`` blocks embed name + description + url for
    # each story in the tag listing. Parsing the JSON segment is more
    # durable than scraping rotating CSS classes. Tolerate optional
    # whitespace between fields — Wattpad's live response is
    # contiguous, but pretty-printed copies (and our test fixtures)
    # break across lines.
    list_item_re = re.compile(
        r'"@type":\s*"ListItem"\s*,'
        r'\s*"name":\s*"([^"]+)"\s*,'
        r'\s*"description":\s*"([^"]*)"\s*,'
        r'\s*"url":\s*"(https://www\.wattpad\.com/story/(\d+)-[^"]+)"',
        re.DOTALL,
    )
    for m in list_item_re.finditer(html):
        title = m.group(1).strip()
        summary = m.group(2).strip().replace("\\n", " ").replace("\\\"", '"')
        story_url = m.group(3)
        sid = m.group(4)
        if sid in seen or not title:
            continue
        if not _matches_query(query, title, summary):
            continue
        seen.add(sid)
        out.append({
            "title": title,
            "author": "",
            "url": story_url,
            "summary": summary[:500],
            "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "wattpad",
        })
        if len(out) >= window_end:
            break
    return out[window_start:]


def search_bdsmlibrary(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       **_: object) -> list[dict]:
    """BDSM Library (bdsmlibrary.com) adapter.

    The site speaks plain HTTP only — HTTPS serves an expired
    certificate. Two endpoints feed this adapter:

    * Tag/code searches POST through the advanced search form at
      ``/stories/search.php`` with ``search=advanced``,
      ``codeforstory[<id>]=yes`` for the desired tag, and the
      site's standard ``orderby`` / ``arrange`` pagination knobs.
      The numeric IDs live in :data:`_BDSMLIB_TAG_CODES` —
      femdom resolves to code 13 (F/m, "one female dominating one
      male"), feet to code 41, etc.
    * Plain free-text or tag-less browses GET
      ``/stories/list.php?pos=N&sortby1=moddate&arrange1=DESC`` for
      the recent-stories view.

    Tag-only queries that don't translate return ``[]`` (the site's
    advanced search ignores the form submission with all codes set
    to Maybe and would return every story, swamping the result set).

    Story permalinks are ``/stories/story.php?storyid=N``.
    Position-based pagination steps by 10 rows per page; this
    adapter caps at :data:`PER_SITE_PAGE_MAX` of those rows.
    """
    base = "http://www.bdsmlibrary.com"
    per_page = 10
    pos = max(0, (max(1, int(page)) - 1) * per_page)

    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    code_id = None
    if vocab_tags:
        translated = [_translate_tag("bdsmlibrary", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        code_id = translated[0]

    if code_id:
        # Build the advanced-search GET URL. The form normally POSTs
        # but the same parameters work via GET, which is friendlier
        # to our retry / cache layer in BaseScraper._fetch.
        from urllib.parse import urlencode
        params = {
            "title": "", "author": "", "synopsis": "",
            "lowsize": "", "highsize": "",
            "orderby": "moddate", "arrange": "DESC",
            "search": "advanced",
            "searchit": "1",
            f"codeforstory[{code_id}]": "yes",
            "pos": pos,
        }
        url = f"{base}/stories/search.php?{urlencode(params)}"
    else:
        url = (
            f"{base}/stories/list.php"
            f"?pos={pos}&sortby1=moddate&arrange1=DESC"
        )

    html = _fetch(url)
    if not html:
        return []
    if "storyid=" not in html and 'name="term"' in html:
        # The endpoint served a row-less page (the header search form
        # is present but no story permalinks). Advanced code search
        # died first (observed 2026-07); by July the list.php browse
        # renders an empty table skeleton too — "Story - (Total
        # Stories)" with blank dynamic slots. Raise so the fan-out
        # reports a site failure instead of a silent "0 results".
        raise SearchFetchError(
            "bdsmlibrary: listing returned no story rows — the site's "
            "public listing backend is down (dead since ~2026-07)"
        )
    out: list[dict] = []
    seen = set()
    # Each story row carries ``story.php?storyid=N`` for the title
    # and ``author.php?authorid=N`` immediately after it. Pair them
    # in document order.
    pattern = re.compile(
        r'<a\s+href="/stories/story\.php\?storyid=(\d+)"[^>]*>([^<]+)</a>'
        r'(?:.*?<a\s+href="/stories/author\.php\?authorid=\d+"[^>]*>([^<]+)</a>)?',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        story_id, title, author = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        if story_id in seen or not title:
            continue
        if not _matches_query(query, title, author):
            continue
        seen.add(story_id)
        out.append({
            "title": title,
            "author": author,
            "url": f"{base}/stories/story.php?storyid={story_id}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "bdsmlibrary",
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


_ROM_TAG_SLUGS: dict[str, str] = {
    # Unified vocab tag → ReadOnlyMind hashtag. ROM hashtags use
    # underscores; only tags verified to exist on the site are mapped
    # (an unmapped tag falls back to a plain-text search of the tag
    # word, which ROM's search handles sensibly).
    "femdom": "femdom",
    "feet": "feet",
    "foot-worship": "footplay",
    "footjob": "footjob",
    "cunnilingus": "cunnilingus",
    "pussy-eating": "cunnilingus",
    "hypnosis": "hypnosis",
    "mind-control": "mind_control",
    "humiliation": "humiliation",
}

_ROM_CARD_RE = re.compile(
    # The word-count div wraps three optional parts: "[Ongoing]"
    # status, "N chapters," and the count itself — "(6 chapters, 9232
    # words)". The old pattern only matched the bare "(N words)" form,
    # so every serial or Ongoing card lost its count. The description
    # <p> follows the count div.
    r'story-card-publication-date">(?P<date>\d{4}-\d{2}-\d{2})</div>'
    r'.*?<a href="(?P<href>/@[^"]+/[^"/]+/)">(?P<title>[^<]+)</a>'
    r'.*?story-card-authors">\s*by <a[^>]*>(?P<author>[^<]*)</a>'
    r'(?:.*?story-card-word-count">\s*(?:\[(?P<status>[^\]]+)\]\s*)?'
    r'\((?:(?P<chapters>\d+)\s+chapters?,\s+)?(?P<words>[\d,]+)\s+words\))?'
    r'(?:.*?<p class="story-card-description">\s*(?P<desc>.*?)\s*</p>)?',
    re.S,
)


def search_readonlymind(query: str, *, page: int = 1,
                        tags: Optional[list] = None,
                        **_: object) -> list[dict]:
    """ReadOnlyMind: server-side search at ``/search/?q=``, ordered by
    last update — which makes the EMPTY query a proper newest-first
    site browse, so bare per-site browsing works here natively. Tags
    become hashtag queries via :data:`_ROM_TAG_SLUGS`. Cards carry a
    publication date (feeds the date sort) and a real word count.
    """
    from urllib.parse import quote_plus

    q = ""
    if query:
        q = query
    elif tags:
        first = (tags[0] or "").strip().lower()
        slug = _ROM_TAG_SLUGS.get(first)
        q = f"#{slug}" if slug else first
    url = f"https://readonlymind.com/search/?q={quote_plus(q)}&page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    for chunk in html.split("story-card-large")[1:]:
        m = _ROM_CARD_RE.search(chunk)
        if not m:
            continue
        title = html_module.unescape(m.group("title").strip())
        author = html_module.unescape((m.group("author") or "").strip())
        summary = ""
        if m.group("desc"):
            summary = html_module.unescape(
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group("desc"))),
            ).strip()
        # No client-side re-filter: ``q`` went to the server, and ROM
        # relevance hits routinely lack the literal query string in
        # their title/author (re-filtering used to empty every
        # keyword search).
        words = (m.group("words") or "?").replace(",", "")
        chapters = m.group("chapters") or ("1" if m.group("words") else "?")
        status = ""
        if (m.group("status") or "").strip().lower() == "ongoing":
            status = "In progress"
        out.append({
            "title": title, "author": author,
            "url": f"https://readonlymind.com{m.group('href')}",
            "summary": summary, "words": words or "?", "chapters": chapters,
            "rating": "M", "fandom": "", "status": status,
            "site": "readonlymind",
            "updated": m.group("date"),
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break
    return out


_GW_LISTING_PAGE_ROWS = 20
_GW_CHROME_LINK_TEXT = {"table of contents", "report this"}

_GW_TAGS = frozenset({"feet", "foot-worship", "footjob", "trampling"})
"""Tags the giantess niche plausibly satisfies wholesale. Like the
GreatFeet/Mousepad rule: tag-only browses outside this set return
``[]`` rather than leaking 48k giantess stories into unrelated
fan-outs."""


def search_giantessworld(query: str, *, page: int = 1,
                         tags: Optional[list] = None,
                         **_: object) -> list[dict]:
    """Giantess World: eFiction recent-stories browse
    (``browse.php?type=recent&offset=K``, 20 rows/page, newest
    first), filtered client-side. The site's own search is a POST
    form, and recency + title filtering covers the discovery need.

    Returns a :class:`SparsePage` when the query filtered out a
    still-populated page.
    """
    if (
        tags and not query
        and not any(t.strip().lower() in _GW_TAGS for t in tags)
    ):
        return []
    offset = (max(1, int(page)) - 1) * _GW_LISTING_PAGE_ROWS
    html = _fetch(
        f"https://giantessworld.net/browse.php?type=recent&offset={offset}",
    )
    if not html:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    fetched_any = False

    # eFiction renders one div.listbox per story: div.title (title +
    # author links, "Rated: X"), div.content (summary text after a
    # "Summary:" span.label, then "Word count: N" / "Chapters: N" /
    # "Completed: Yes|No" in the same text run), div.tail (Published/
    # Updated dates).
    soup = BeautifulSoup(html, "lxml")
    listboxes = soup.find_all("div", class_="listbox")
    for box in listboxes:
        title_a = box.select_one(
            "div.title a[href*='viewstory.php?sid=']",
        )
        if title_a is None:
            continue
        m = re.search(r"sid=(\d+)", title_a.get("href") or "")
        if not m:
            continue
        sid = m.group(1)
        if sid in seen:
            continue
        seen.add(sid)
        fetched_any = True
        title = title_a.get_text(" ", strip=True)
        if not title or title.lower() in _GW_CHROME_LINK_TEXT:
            continue

        title_div = box.find("div", class_="title")
        author = ""
        rating = "M"
        if title_div is not None:
            author_a = title_div.find(
                "a", href=re.compile(r"viewuser\.php\?uid=\d+"),
            )
            if author_a is not None:
                author = author_a.get_text(" ", strip=True)
            r_m = re.search(
                r"Rated:\s*([^\[\]]+?)\s*\[",
                title_div.get_text(" ", strip=True),
            )
            if r_m:
                rating = r_m.group(1).strip()

        summary = ""
        words = "?"
        chapters = "?"
        status = ""
        content = box.find("div", class_="content")
        if content is not None:
            label = next(
                (
                    lb for lb in content.find_all("span", class_="label")
                    if lb.get_text(" ", strip=True).startswith("Summary")
                ),
                None,
            )
            if label is not None:
                # The summary is the sibling run after the label, up
                # to the next span.label. Walking siblings (not
                # find_next("p")) handles the occasional row whose
                # summary is bare text rather than a <p>.
                parts: list[str] = []
                for sib in label.next_siblings:
                    if getattr(sib, "name", None) == "span" and \
                            "label" in (sib.get("class") or []):
                        break
                    text = (
                        sib.get_text(" ", strip=True)
                        if hasattr(sib, "get_text") else str(sib).strip()
                    )
                    if text:
                        parts.append(text)
                summary = re.sub(r"\s+", " ", " ".join(parts)).strip()
            content_text = content.get_text(" ", strip=True)
            w_m = re.search(r"Word count:\s*([\d,]+)", content_text)
            if w_m:
                words = w_m.group(1)
            ch_m = re.search(r"Chapters:\s*(\d+)", content_text)
            if ch_m:
                chapters = ch_m.group(1)
            c_m = re.search(r"Completed:\s*(Yes|No)", content_text)
            if c_m:
                status = "Complete" if c_m.group(1) == "Yes" else "In progress"

        updated = ""
        tail = box.find("div", class_="tail")
        if tail is not None:
            u_m = re.search(
                r"Updated:\s*([A-Za-z]+ \d{1,2} \d{4})",
                tail.get_text(" ", strip=True),
            )
            if u_m:
                updated = _iso_date(u_m.group(1))

        if not _matches_query(query, title, summary):
            continue
        out.append({
            "title": title, "author": author,
            "url": f"https://giantessworld.net/viewstory.php?sid={sid}",
            "summary": summary, "words": words, "chapters": chapters,
            "rating": rating, "fandom": "giantess", "status": status,
            "site": "giantessworld",
            "updated": updated,
        })
        if len(out) >= PER_SITE_PAGE_MAX:
            break

    if not listboxes:
        # Markup-drift fallback: the old flat anchor walk over raw
        # HTML (titles only).
        for m in re.finditer(
            r'href="viewstory\.php\?sid=(?P<sid>\d+)[^"]*"[^>]*>'
            r'(?P<text>[^<]{2,80})<',
            html,
        ):
            sid, text = m.group("sid"), m.group("text").strip()
            if not text or text.lower() in _GW_CHROME_LINK_TEXT:
                continue
            if sid in seen:
                continue
            seen.add(sid)
            fetched_any = True
            if not _matches_query(query, text):
                continue
            out.append({
                "title": text, "author": "",
                "url": f"https://giantessworld.net/viewstory.php?sid={sid}",
                "summary": "", "words": "?", "chapters": "?",
                "rating": "M", "fandom": "giantess", "status": "",
                "site": "giantessworld",
            })
            if len(out) >= PER_SITE_PAGE_MAX:
                break
    if not out and fetched_any:
        return SparsePage()
    return out


_MOUSEPAD_STORY_FORUMS: tuple[str, ...] = ("72", "97")
"""The Mousepad's fiction sections: Stories (f72, ~5k topics) and the
Classic Story Library (f97). Story Requests (f94) and Experiences
(f96) are deliberately out — one is want-ads, the other blog-style
anecdotes."""

_MOUSEPAD_TAGS = frozenset({
    "feet", "foot-worship", "footjob", "femdom", "trampling",
})
"""Tags the whole board plausibly satisfies. Mirrors the GreatFeet
rule: The Mousepad is a foot-fetish forum end to end, so a tag-only
browse for anything outside this set returns ``[]`` instead of
leaking the entire topic listing into an unrelated fan-out."""


def search_mousepad(query: str, *, page: int = 1,
                    tags: Optional[list] = None,
                    **_: object) -> list[dict]:
    """The Mousepad: Tapatalk-hosted phpBB foot-fetish story forum.

    A forum, not an archive — so browse and query share one path:
    window the story forums' topic listings (newest activity first,
    :data:`tapatalk.TOPIC_WINDOW` rows per forum per page) and filter
    client-side with :func:`_matches_query` over title, author and
    the last-post teaser. The board's native ``search`` method is
    unusable (5-row cap, offset ignored — see
    :mod:`ficary.erotica.tapatalk`).

    Every row carries ``updated`` (last-activity ISO stamp straight
    off the listing), which is what makes the "Newest first" sort
    meaningful for this site.

    Returns a :class:`SparsePage` when a query filtered out an entire
    still-populated window, so the fan-out keeps the site eligible
    for deeper pages instead of marking it exhausted.
    """
    if (
        tags and not query
        and not any(t.strip().lower() in _MOUSEPAD_TAGS for t in tags)
    ):
        return []
    window_start = (max(1, int(page)) - 1) * tapatalk.TOPIC_WINDOW
    out: list[dict] = []
    fetched_any = False
    for forum_id in _MOUSEPAD_STORY_FORUMS:
        resp = tapatalk.mobiquo_call(
            "get_topic", forum_id,
            window_start, window_start + tapatalk.TOPIC_WINDOW - 1,
        )
        rows = resp.get("topics") or []
        # The server clamps an out-of-range offset to the listing's
        # tail instead of returning nothing, so a walked-past-the-end
        # page would re-serve the last rows forever. Bound the window
        # against the reported topic count ourselves.
        total_topics = int(resp.get("total_topic_num") or 0)
        if window_start >= total_topics:
            rows = []
        if rows:
            fetched_any = True
        for t in rows:
            title = tapatalk.decode_value(t.get("topic_title"))
            author = tapatalk.decode_value(t.get("topic_author_name"))
            teaser = tapatalk.decode_value(t.get("short_content"))
            if not title:
                continue
            if not _matches_query(query, title, author, teaser):
                continue
            out.append({
                "title": title,
                "author": author,
                "url": tapatalk.topic_url(
                    tapatalk.decode_value(t.get("topic_id")),
                ),
                "summary": teaser,
                "words": "?", "chapters": "?",
                "rating": "M", "fandom": "", "status": "",
                "site": "mousepad",
                "updated": tapatalk.iso_datetime(t.get("post_time")),
            })
    if not out and fetched_any:
        return SparsePage()
    return out


# ── Fan-out ──────────────────────────────────────────────────────

_SITE_FNS: dict[str, Callable[..., list[dict]]] = {
    "literotica": search_literotica_wrapped,
    "ao3": search_ao3_erotica,
    "wattpad": search_wattpad_erotica,
    "aff": search_aff,
    "storiesonline": search_sol,
    "nifty": search_nifty,
    "sexstories": search_sexstories,
    "mcstories": search_mcstories,
    "lushstories": search_lushstories,
    "fictionmania": search_fictionmania,
    "tgstorytime": search_tgstorytime,
    "chyoa": search_chyoa,
    "darkwanderer": search_darkwanderer,
    "greatfeet": search_greatfeet,
    "bdsmlibrary": search_bdsmlibrary,
    "mousepad": search_mousepad,
    "readonlymind": search_readonlymind,
    "giantessworld": search_giantessworld,
    "chastitymansion": search_chastitymansion,
    "ticklingforum": search_ticklingforum,
}


TAG_SITE_COVERAGE: dict[str, list[str]] = {
    # Which sites carry each tag as a first-class category or
    # well-represented kink. Used by the tag picker to annotate each
    # option with its site-count (see :func:`tag_site_count`) so users
    # can see at a glance whether a tag is well-covered or niche.
    # Entries only list sites where the tag has a native URL slug /
    # category / code (verified May 2026 — see :data:`_LITEROTICA_TAG_SLUGS`,
    # :data:`_LUSH_TAG_SLUGS`, :data:`_SOL_TAG_SLUGS`,
    # :data:`_MCS_TAG_CODES`, :data:`_AO3_TAG_SLUGS`,
    # :data:`_BDSMLIB_TAG_CODES`); sites where the tag would have to
    # ride along inside a free-text query don't count.
    "anal": [
        "literotica", "storiesonline", "lushstories", "sexstories", "ao3",
    ],
    "bdsm": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "wattpad", "bdsmlibrary",
    ],
    "body-worship": ["literotica", "ao3", "wattpad"],
    "bondage": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "bukkake": ["literotica", "sexstories", "ao3"],
    "celebrity": ["literotica", "storiesonline", "sexstories", "ao3", "wattpad"],
    "cfnm": ["literotica", "ao3", "wattpad"],
    "cheating": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3",
    ],
    "chastity": [
        "literotica", "storiesonline", "ao3", "bdsmlibrary",
        "chastitymansion",
    ],
    "cuckold": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3",
    ],
    "cunnilingus": [
        "literotica", "lushstories", "storiesonline", "ao3",
        "readonlymind",
    ],
    "dominance-submission": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary", "chastitymansion",
    ],
    "exhibitionism": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "face-sitting": ["literotica", "lushstories", "ao3"],
    "femdom": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "wattpad", "bdsmlibrary", "mousepad", "readonlymind",
        "chastitymansion",
    ],
    "feet": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
        "wattpad", "bdsmlibrary", "mousepad", "readonlymind",
        "giantessworld", "ticklingforum",
    ],
    "female-led": ["literotica", "ao3", "wattpad", "chastitymansion"],
    "foot-worship": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
        "wattpad", "mousepad", "readonlymind", "giantessworld",
        "ticklingforum",
    ],
    "footjob": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
        "mousepad", "readonlymind", "giantessworld",
    ],
    "fisting": [
        "literotica", "storiesonline", "sexstories", "ao3", "bdsmlibrary",
    ],
    "futanari": ["literotica", "storiesonline", "tgstorytime", "ao3"],
    "gangbang": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
    ],
    "gay": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff", "mcstories", "ao3",
    ],
    "group-sex": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
    ],
    "harem": ["literotica", "storiesonline", "lushstories", "ao3"],
    "humiliation": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "readonlymind",
    ],
    "hypnosis": [
        "mcstories", "storiesonline", "literotica", "chyoa", "ao3",
        "readonlymind",
    ],
    "incest": [
        "literotica", "storiesonline", "sexstories", "aff", "mcstories",
        "ao3", "bdsmlibrary",
    ],
    "interracial": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3", "bdsmlibrary",
    ],
    "lactation": [
        "literotica", "storiesonline", "sexstories", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "lesbian": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff", "mcstories", "ao3",
    ],
    "masturbation": [
        "literotica", "lushstories", "sexstories", "mcstories", "ao3",
    ],
    "mature": ["literotica", "lushstories", "storiesonline", "ao3"],
    "mind-control": [
        "mcstories", "storiesonline", "literotica", "chyoa", "ao3",
        "readonlymind",
    ],
    "non-consent": [
        "literotica", "storiesonline", "mcstories", "sexstories", "ao3",
        "bdsmlibrary",
    ],
    "oral": ["literotica", "lushstories", "sexstories", "ao3"],
    "orgy": ["literotica", "storiesonline", "sexstories", "ao3"],
    "pegging": [
        "literotica", "lushstories", "storiesonline", "ao3", "wattpad",
    ],
    "polyamory": ["literotica", "storiesonline", "lushstories", "ao3"],
    "pregnancy": ["literotica", "storiesonline", "sexstories", "ao3"],
    "public-sex": ["literotica", "lushstories", "storiesonline", "ao3"],
    "pussy-eating": ["literotica", "ao3", "readonlymind"],
    "queening": ["literotica", "lushstories", "storiesonline", "ao3"],
    "roleplay": ["literotica", "lushstories", "chyoa", "ao3"],
    "rough": ["literotica", "lushstories", "sexstories", "ao3"],
    "spanking": [
        "literotica", "lushstories", "storiesonline", "ao3", "wattpad",
        "bdsmlibrary",
    ],
    "squirting": ["literotica", "storiesonline", "ao3"],
    "strap-on": ["literotica", "lushstories", "ao3", "wattpad"],
    "swinging": [
        "literotica", "lushstories", "storiesonline", "darkwanderer", "ao3",
    ],
    "tease-and-denial": [
        "literotica", "ao3", "wattpad", "chastitymansion",
    ],
    "teen": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
        "bdsmlibrary",
    ],
    "threesome": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
        "wattpad",
    ],
    "trampling": [
        "literotica", "greatfeet", "ao3", "wattpad", "mousepad",
        "giantessworld",
    ],
    "transgender": [
        "literotica", "fictionmania", "tgstorytime", "storiesonline",
        "lushstories", "ao3",
    ],
    "voyeur": [
        "literotica", "lushstories", "storiesonline", "ao3", "bdsmlibrary",
    ],
    "watersports": [
        "literotica", "storiesonline", "sexstories", "mcstories", "ao3",
        "bdsmlibrary",
    ],
}


def tag_site_count(tag: str) -> int:
    """How many sites meaningfully cover this tag. Used by the GUI
    multi-picker to annotate each entry with ``[N sites]`` so users
    can tell femdom (many sites) from chastity (three sites) before
    running a fruitless query."""
    return len(TAG_SITE_COVERAGE.get(tag.lower(), []))


def tag_sites_for(tag: str) -> list[str]:
    """Site slugs that meaningfully cover ``tag``. Empty list means
    the tag isn't in the unified vocabulary."""
    return list(TAG_SITE_COVERAGE.get(tag.lower(), []))


# Sites that fold ``tags`` into their full-text search payload rather
# than having a native tag URL. ``search_sexstories`` extends its query
# terms with ``tags[:3]`` so any tag — including niche ones outside
# :data:`TAG_SITE_COVERAGE` — narrows the result set in a sensible way.
# Listing the site here lets the dispatcher include it for arbitrary
# tags instead of dropping it because our pre-computed coverage map
# happens to omit the tag.
_TAG_TEXT_FOLD_SITES: set[str] = {"sexstories"}


def _site_handles_any_tag(site: str, tags: list[str]) -> bool:
    """True when ``site`` can do *something useful* with at least one
    tag in ``tags``.

    Source of truth: :data:`TAG_SITE_COVERAGE` (which sites natively
    carry each vocab tag). The earlier slug-passthrough escape hatch
    for Lushstories has been retired now that each adapter routes
    tags through :func:`_translate_tag` — Lush returns ``[]`` for
    untranslatable tags rather than 200-ing a stub category page, so
    a passthrough flag here would only let off-topic dead-page calls
    into the fan-out.

    :data:`_TAG_TEXT_FOLD_SITES` still applies: SexStories folds tags
    into its full-text query and so handles arbitrary tags sensibly
    even outside :data:`TAG_SITE_COVERAGE`.

    The gate continues to skip sites whose scrapers genuinely ignore
    the ``tags`` kwarg (Fictionmania, TGStorytime, DarkWanderer, etc.)
    so a tag-only ``feet`` search doesn't include those archives'
    default browses as noise.
    """
    if not tags:
        return True
    for t in tags:
        tag_lower = t.strip().lower()
        if not tag_lower:
            continue
        if site in TAG_SITE_COVERAGE.get(tag_lower, []):
            return True
        if site in _TAG_TEXT_FOLD_SITES:
            return True
    return False


# Public alias for older callers / tests. Both names point at the new
# OR-semantics helper above; the older name simply describes the
# round-1 contract that no longer matches reality.
_site_supports_all_tags = _site_handles_any_tag


def tags_for_site(site: str) -> list[str]:
    """Vocab tags the given site can actually search, in vocabulary
    order. ``"all"`` (or empty/unknown) returns the full vocabulary.

    Source of truth is :func:`_site_handles_any_tag`, so the GUI tag
    picker offers exactly the tags the fan-out will run for that site —
    no drift between what the user can pick and what the search does.
    """
    slug = (site or "").strip().lower()
    if not slug or slug == "all":
        return list(EROTICA_TAG_VOCABULARY)
    return [t for t in EROTICA_TAG_VOCABULARY if _site_handles_any_tag(slug, [t])]


_SITE_LABEL_SLUG_RE = re.compile(r"\(([a-z][a-z0-9_-]*)\)\s*$")


def _extract_slug(label_or_slug: str) -> str:
    """Pull the slug from a ``"Label (slug)"`` formatted dropdown
    choice or pass a bare slug through unchanged. The GUI's Site
    dropdown shows friendly labels with a trailing ``(slug)`` marker
    so the user sees ``Adult-FanFiction.org (aff)`` instead of just
    ``aff``; this helper recovers the slug for the fan-out without
    requiring a separate label-to-slug map at the call site.

    Bare slugs (CLI, tests, scripted callers) round-trip unchanged.
    A full label without a slug marker also round-trips through the
    reverse-lookup of :data:`EROTICA_SITE_LABELS` for safety.
    """
    s = str(label_or_slug).strip()
    if not s:
        return s
    m = _SITE_LABEL_SLUG_RE.search(s)
    if m:
        return m.group(1)
    for slug, label in EROTICA_SITE_LABELS.items():
        if s == label:
            return slug
    return s


def _normalise_sites(sites, sites_choice) -> Optional[list]:
    """GUI passes ``sites_choice`` (a single string from the dropdown);
    CLI / tests pass ``sites`` (a list). Fold both into the list form
    the fan-out expects, or ``None`` for "search every site"."""
    if sites:
        if isinstance(sites, str):
            sites = [sites]
        slugs = [_extract_slug(s) for s in sites if s]
        return [s for s in slugs if s and s != "all"] or None
    if sites_choice:
        slug = _extract_slug(sites_choice)
        if slug and slug not in ("", "all"):
            return [slug]
    return None


_TAG_COVERAGE_SUFFIX_RE = re.compile(r"\s*\[\d+\s+sites?\]\s*$", re.I)
"""Strips the "[5 sites]" annotation the GUI tag picker appends to
each option. Keeps the scraper-facing tag list clean."""


def _normalise_tags(tags) -> list[str]:
    """Accept either a Python list or the comma-separated string the
    multi-picker dialog writes into its text control. Drop empties
    and strip the GUI's ``[N sites]`` coverage annotation."""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        raw = [str(t).strip() for t in tags if str(t).strip()]
    return [_TAG_COVERAGE_SUFFIX_RE.sub("", t).strip() for t in raw if t]


class SparsePage(list):
    """An empty result page whose source listing still has rows.

    Windowed adapters that filter client-side (Mousepad's query path)
    can have every row of a window fail the filter while thousands of
    topics remain beyond it. A plain empty list tells the fan-out
    "walked past the tail — stop polling this site"; returning this
    subclass instead keeps the site eligible so deeper pages still get
    scanned.
    """

    more_available = True


class ErotiCAResults(list):
    """``list`` subclass that carries per-site stats alongside the
    merged result rows.

    Rationale: the GUI's SearchFrame treats search results as a plain
    ``list[dict]`` (so ``fetch_until_limit`` can iterate without
    knowing which site it's talking to), but the unified search also
    needs to surface *per-site* health — how many hits each archive
    returned, which ones failed, and which ones are exhausted. Rather
    than break the contract or thread a second return value through
    ``fetch_until_limit``, we subclass ``list`` and hang the stats
    dict off an attribute. Callers that don't care keep seeing a list
    of dicts; the erotica SearchFrame peeks at ``.site_stats`` for
    the summary panel and ``.exhausted_sites`` for load-more gating.

    ``total_sites`` is the set of every site the initial search
    considered eligible — the canonical denominator for "have we
    exhausted everything?". The GUI used to compute this from
    ``len(site_stats)``, but on Load More the new call's
    ``site_stats`` only includes the currently-active sites (the ones
    not yet exhausted via ``skip_sites``), so the comparison would
    falsely trigger end-of-results. Tracking the canonical set on the
    result object keeps the GUI's exhaustion check honest.
    """

    site_stats: dict
    exhausted_sites: set
    total_sites: set
    more_available: bool

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.site_stats = {}
        self.exhausted_sites = set()
        self.total_sites = set()
        # True when at least one site returned a SparsePage this call —
        # tells the multi-page driver an empty merged page is worth
        # following with a deeper one.
        self.more_available = False


def search_erotica(
    query: str = "",
    *,
    page: int = 1,
    sites: Optional[list] = None,
    sites_choice: str = "",
    tags: Optional[object] = None,
    tags_picked: Optional[object] = None,
    min_words: str = "",
    category: str = "",
    fandom: str = "",
    sort: str = "",
    skip_sites: Optional[set] = None,
    **_: object,
) -> "ErotiCAResults":
    """Fan-out search across every registered erotica site.

    Args:
        query: Free-text search string. Passed to each site's search
            function; most sites treat it as a case-insensitive title
            filter because they lack a real full-text API.
        page: Result page. Paginating sites (Literotica, SOL, AO3,
            Lush...) fetch their native page N; single-listing sites
            (MCStories, Nifty, Wattpad...) window their one listing
            by :data:`PER_SITE_PAGE_MAX` rows and return ``[]`` past
            its end.
        sites: Restrict the fan-out to this list of site slugs. When
            ``None`` or ``["all"]``, every site in :data:`_SITE_FNS`
            is queried.
        tags: Unified tag list. Each site maps tags onto its own
            vocabulary (:data:`_MCS_TAG_CODES` for MCStories, category
            slug for Lushstories, ``bytag`` URL for SOL, etc.).
        min_words: Optional minimum word-count string like ``"5k+"``;
            applied client-side after fetch (sites don't universally
            expose word counts in their listings).
        category, fandom: Passed through to sites that accept them
            (Lushstories, AFF respectively).
        sort: ``"Newest first"`` / ``"date"`` orders this batch by each
            row's ``updated`` stamp, newest first, undated rows last
            (see :func:`sort_rows_by_updated`). Anything else keeps
            the default site-then-title grouping. Applied per batch —
            the GUI re-sorts the accumulated list on Load More.

    Returns:
        Merged list of result dicts. Every dict carries a ``site`` key
        (one of :data:`EROTICA_SITE_SLUGS`) so the caller can render a
        "Site" column / filter results by origin.
    """
    resolved_sites = _normalise_sites(sites, sites_choice)
    tag_list = _normalise_tags(tags_picked if tags_picked is not None else tags)
    skip_set = set(skip_sites or ())

    # Query-to-tag promotion: when the user typed a word that *is* a
    # known erotica tag and didn't pick any tags from the multi-picker,
    # promote the query to a tag search. This is exact-match only —
    # "feet" gets promoted; "feet fetish" does not — so the rule never
    # surprises a user who genuinely wanted a free-text search. Tag-
    # capable sites then use their native tag URL instead of falling
    # back to title filtering, which is dramatically better for broad
    # discovery searches like ``feet`` or ``femdom``.
    if not tag_list and query:
        normalised_query = query.strip().lower()
        if normalised_query in EROTICA_TAG_VOCABULARY:
            tag_list = [normalised_query]
            # Clear the query: the word is now represented by the tag,
            # so tag-capable adapters take their native tag-URL path.
            # Leaving it set would ALSO apply it as a client-side
            # substring filter over those tag-page rows — whose text is
            # site codes/slugs (MCStories ``fd mc``, SOL ``femaledom``),
            # not the English vocab word — decimating the results
            # (MCStories 200 -> 7, SOL 10 -> 0 for ``femdom``).
            query = ""
            logger.info(
                "erotica: promoting bare query %r to tag-search",
                normalised_query,
            )

    # Explicit single-site scope + tags the site doesn't carry: browse
    # the site bare instead of returning nothing. The user picking one
    # site has opted into that site's catalogue; the tag gate exists
    # to keep off-topic sites out of *fan-outs*, and the search window
    # restores last session's tag box, so a stale tag here otherwise
    # turns "browse The Mousepad, newest first" into a silent zero.
    if (
        resolved_sites and len(resolved_sites) == 1 and tag_list
        and not _site_handles_any_tag(resolved_sites[0], tag_list)
    ):
        logger.info(
            "erotica: %s doesn't carry %s — browsing the site bare",
            resolved_sites[0], tag_list,
        )
        tag_list = []

    if resolved_sites is None:
        active = [s for s in _SITE_FNS if s not in skip_set]
    else:
        active = [
            s for s in resolved_sites if s in _SITE_FNS and s not in skip_set
        ]

    # Tag-capability filter: drop sites that don't meaningfully cover
    # the requested tag(s) from this fan-out. Without this, sites
    # whose adapter ignores the ``tags`` kwarg (Fictionmania /
    # TGStorytime / DarkWanderer / GreatFeet for non-feet tags — they
    # accept ``**_: object``) silently fall back to a default browse
    # and flood the result set with unrelated rows.
    #
    # Semantics: OR across selected tags. A site stays in the fan-out
    # if it handles *any* selected tag — most per-site scrapers only
    # consume ``tags[0]`` anyway, so claiming AND semantics would
    # misrepresent what the scrapers actually deliver. See
    # :func:`_site_handles_any_tag` for the per-tag handling lookup.
    #
    # Only applied when the caller didn't restrict ``sites`` to a
    # specific slug — a user who explicitly picks one site has
    # already opted into whatever that site returns.
    if tag_list and resolved_sites is None:
        active = [
            s for s in active
            if _site_handles_any_tag(s, tag_list)
        ]
    # ``total_sites`` is captured BEFORE any further filtering so the
    # GUI's "all sites exhausted?" check has a stable denominator on
    # Load More — the per-call ``site_stats`` keys shrink as exhausted
    # sites are removed from subsequent fan-outs.
    initial_eligible = set(active)
    if not active:
        empty = ErotiCAResults()
        empty.total_sites = initial_eligible
        return empty

    min_words_val = "" if min_words in ("", "any") else min_words

    kwargs = {
        "page": page,
        "tags": tag_list,
        "category": category,
        "fandom": fandom,
    }

    merged = ErotiCAResults()
    # Per-site stats: count of hits, ok flag, error message (or None).
    # The GUI summary panel reads this to show "MCStories: 8, SOL:
    # 12, SoFurry: FAIL (timeout)" so users know which sites contributed
    # versus which silently failed vs. returned zero hits.
    site_stats: dict[str, dict] = {
        s: {"count": 0, "ok": True, "error": None, "exhausted": False}
        for s in active
    }

    # ThreadPoolExecutor — every site's search is network-bound, so
    # concurrent HTTP requests complete in about as long as the
    # slowest one. Each site function swallows its own errors, but we
    # also catch here to flip the per-site ``ok`` flag so the GUI can
    # surface the failure instead of silently omitting a site.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as ex:
        futures = {ex.submit(_SITE_FNS[s], query, **kwargs): s for s in active}
        for fut in concurrent.futures.as_completed(futures):
            site_slug = futures[fut]
            try:
                # ``or []`` would flatten an empty SparsePage into a
                # plain list and lose its ``more_available`` marker.
                site_results = fut.result()
                if site_results is None:
                    site_results = []
                site_stats[site_slug]["count"] = len(site_results)
                # An empty page means we've walked past this site's
                # tail (single-listing adapters return [] for any
                # window beyond their one page) — mark it exhausted so
                # Load More stops polling it. A partial page stays
                # eligible; its next poll returns [] and flips it,
                # costing one wasted fetch at each site's tail.
                # Exception: a SparsePage is "nothing survived the
                # site's client-side filter but the listing continues"
                # — keep the site eligible for deeper pages.
                if not site_results:
                    if getattr(site_results, "more_available", False):
                        merged.more_available = True
                    else:
                        site_stats[site_slug]["exhausted"] = True
                        merged.exhausted_sites.add(site_slug)
            except Exception as exc:
                logger.warning("erotica search (%s) failed: %s", site_slug, exc)
                site_stats[site_slug].update(
                    ok=False, error=str(exc) or exc.__class__.__name__,
                    exhausted=True,
                )
                merged.exhausted_sites.add(site_slug)
                site_results = []
            for r in site_results:
                r.setdefault("site", site_slug)
            merged.extend(site_results)

    if min_words_val:
        filtered = _filter_by_min_words(merged, min_words_val)
        # Preserve the stats across the filter rebuild.
        new_merged = ErotiCAResults(filtered)
        new_merged.site_stats = site_stats
        new_merged.exhausted_sites = merged.exhausted_sites
        new_merged.total_sites = initial_eligible
        new_merged.more_available = merged.more_available
        merged = new_merged
    else:
        merged.site_stats = site_stats
        merged.total_sites = initial_eligible

    # Stable ordering: by site first (alphabetical) then by title — so
    # users can scan results grouped by archive without the run order
    # of the ThreadPool shuffling things between searches.
    merged.sort(key=lambda r: (r.get("site", ""), r.get("title", "").lower()))
    if erotica_sort_mode(sort) == "date":
        resorted = ErotiCAResults(sort_rows_by_updated(merged))
        resorted.site_stats = merged.site_stats
        resorted.exhausted_sites = merged.exhausted_sites
        resorted.total_sites = merged.total_sites
        resorted.more_available = merged.more_available
        merged = resorted
    return merged


def _filter_by_min_words(results: list[dict], min_words: str) -> list[dict]:
    """Drop rows whose word-count is known to be under ``min_words``.

    ``min_words`` accepts either a plain integer ("5000") or one of
    the FFN-style shorthand labels ("1k", "5k+", "30k+", "150k+").
    Rows whose ``words`` field is unknown ("?") pass through — we'd
    rather keep a possibly-large story than hide it behind a guess."""
    threshold = _parse_word_threshold(min_words)
    if threshold <= 0:
        return results
    kept = []
    for r in results:
        raw = str(r.get("words") or "").replace(",", "").strip()
        if not raw or raw == "?" or not raw[0].isdigit():
            kept.append(r)
            continue
        # Sites publish either exact counts ("6022") or rounded
        # k-format ("2.6k", Lushstories) — _parse_word_threshold
        # already speaks both.
        value = _parse_word_threshold(raw)
        if value <= 0 or value >= threshold:
            kept.append(r)
    return kept


def _parse_word_threshold(value: str) -> int:
    """Convert a ``min_words`` input into an integer threshold. Returns
    0 when the input is empty or unparseable."""
    if not value:
        return 0
    s = str(value).strip().lower().rstrip("+")
    if not s:
        return 0
    multiplier = 1
    if s.endswith("k"):
        multiplier = 1000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return 0
