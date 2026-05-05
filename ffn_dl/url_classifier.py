"""Classify any pasted URL as a particular kind of list page.

The "Add from URL list" workflow accepts whatever the user pastes —
an author profile, an AO3 series, a tag listing, a search results
page, an FFN C2 community, a Wattpad reading list — and figures out
which scraper handles it AND which extractor method to call. That
"shape detection" is what this module owns.

Design choices:

* Predicates live on the scrapers (``AO3Scraper.is_search_url`` etc.)
  so each site's URL knowledge stays in one place, alongside the
  matching ``scrape_*_works`` method. This module is the dispatcher,
  not the regex registry.

* Order matters when interrogating predicates. ``author_bookmarks``
  must be checked before ``author_works`` because the bookmarks URL
  is a superset of the author URL pattern on AO3 (``/users/X`` is
  the author root, ``/users/X/bookmarks`` is the bookmarks page).
  The :data:`_PRECEDENCE` tuple pins the order.

* A single :class:`ListPageRef` carries everything the caller needs
  to actually run the extraction: the scraper class, the kind, the
  extractor method name to call. Callers do
  ``getattr(scraper_instance, ref.extractor)(ref.url)``.

Returns ``ListPageRef(kind="story", ...)`` for a single-story URL
(so the same classifier covers both bulk and one-off paste flows
without a separate "is this a single story?" check) and
``ListPageRef(kind="unknown", ...)`` when nothing matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .scraper import BaseScraper
from .sites import ALL_SCRAPERS, detect_scraper, extract_story_url


# Predicates run in this order; first match wins. Bookmarks before
# author so the AO3 user-bookmarks URL doesn't get classified as the
# author root. Single-story comes last because it requires a more
# expensive parse and the list-shape URLs are usually preferred.
_PRECEDENCE: tuple[tuple[str, str, str], ...] = (
    ("is_bookmarks_url", "author_bookmarks", "scrape_bookmark_works"),
    ("is_series_url", "series", "scrape_series_works"),
    ("is_tag_url", "tag", "scrape_tag_works"),
    ("is_search_url", "search", "scrape_search_works"),
    ("is_community_url", "community", "scrape_community_works"),
    ("is_reading_list_url", "reading_list", "scrape_reading_list_works"),
    # Author works is the broadest list-shape match — it catches the
    # bare ``/users/X`` AO3 URL, ``/u/12345`` FFN URL, etc. Run after
    # the more specific predicates so a bookmarks/works subpath
    # doesn't get swallowed.
    ("is_author_url", "author_works", "scrape_author_works"),
)


@dataclass(frozen=True)
class ListPageRef:
    """The result of :func:`classify`.

    ``url`` is the pasted URL, unmodified. ``scraper_cls`` is the
    class that handles this site (callers can ``scraper_cls()`` to
    instantiate). ``kind`` is one of the strings above plus ``story``
    and ``unknown``. ``extractor`` is the method name to call on a
    scraper instance, or empty for ``story`` / ``unknown``.

    ``site_name`` is the lowercase short name (``"ao3"``, ``"ffn"``,
    ``"wattpad"``, ...) the GUI uses for icon lookup and labelling.
    """

    url: str
    scraper_cls: type[BaseScraper]
    kind: str
    extractor: str
    site_name: str = ""


_SCRAPER_CLS_TO_NAME = {
    "AO3Scraper": "ao3",
    "FFNScraper": "ffn",
    "FicWadScraper": "ficwad",
    "RoyalRoadScraper": "royalroad",
    "MediaMinerScraper": "mediaminer",
    "LiteroticaScraper": "literotica",
    "WattpadScraper": "wattpad",
    "AFFScraper": "aff",
    "StoriesOnlineScraper": "storiesonline",
    "NiftyScraper": "nifty",
    "SexStoriesScraper": "sexstories",
    "MCStoriesScraper": "mcstories",
    "LushStoriesScraper": "lushstories",
    "FictionmaniaScraper": "fictionmania",
    "TGStorytimeScraper": "tgstorytime",
    "ChyoaScraper": "chyoa",
    "DarkWandererScraper": "darkwanderer",
    "GreatFeetScraper": "greatfeet",
}


def _site_name_for(scraper_cls: type[BaseScraper]) -> str:
    return _SCRAPER_CLS_TO_NAME.get(scraper_cls.__name__, scraper_cls.__name__)


def classify(url: str) -> Optional[ListPageRef]:
    """Return a :class:`ListPageRef` describing ``url``.

    Returns ``None`` if ``url`` is empty or obviously malformed
    (caller decides whether to surface an error or just no-op).
    Otherwise always returns a ListPageRef — ``kind="unknown"`` is
    a real value, not ``None``, so the caller can branch on it.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None

    for scraper_cls in ALL_SCRAPERS:
        for predicate, kind, extractor in _PRECEDENCE:
            check = getattr(scraper_cls, predicate, None)
            if check is None:
                continue
            try:
                if check(text):
                    return ListPageRef(
                        url=text,
                        scraper_cls=scraper_cls,
                        kind=kind,
                        extractor=extractor,
                        site_name=_site_name_for(scraper_cls),
                    )
            except Exception:
                # A misbehaving predicate must not poison the loop
                # for the rest of the registry.
                continue

    # Single-story URL? extract_story_url's the existing detection.
    extracted = extract_story_url(text)
    if extracted:
        scraper_cls = detect_scraper(extracted)
        return ListPageRef(
            url=text,
            scraper_cls=scraper_cls,
            kind="story",
            extractor="",
            site_name=_site_name_for(scraper_cls),
        )

    # Last resort — punt to detect_scraper's host-prefix match so the
    # caller can at least say "this looks like an FFN URL we don't
    # recognise" instead of complete silence.
    scraper_cls = detect_scraper(text)
    return ListPageRef(
        url=text,
        scraper_cls=scraper_cls,
        kind="unknown",
        extractor="",
        site_name=_site_name_for(scraper_cls),
    )


def extract(ref: ListPageRef) -> tuple[str, list[dict]]:
    """Run the appropriate extractor for ``ref`` and return
    ``(label, [work_dict, ...])``.

    ``label`` is the page's human-readable name — author name for
    author/bookmark pages, series title for series pages, query
    string for search pages, etc. — or empty if the extractor can't
    determine one.

    Single-story refs (``kind="story"``) return ``(url, [{url: ..,
    title: ""}])`` so the caller can treat them uniformly with the
    list shapes; ``unknown`` refs raise ``ValueError``.
    """
    if ref.kind == "unknown":
        raise ValueError(f"Could not classify URL: {ref.url}")
    if ref.kind == "story":
        return ref.url, [{
            "url": ref.url,
            "title": "",
            "author": "",
            "summary": "",
            "words": "",
            "chapters": "",
            "rating": "",
            "fandom": "",
            "status": "",
            "updated": "",
        }]
    scraper = ref.scraper_cls()
    method = getattr(scraper, ref.extractor)
    return method(ref.url)


def _normalise_work(work: dict) -> dict:
    """Pad a work dict with the keys the GUI expects so the
    CheckListBox renders consistently across sites.

    Per-scraper extractors return slightly different shapes — FFN's
    :func:`_ffn_row_to_work` carries ``words`` as a digit string while
    AO3's :func:`_parse_ao3_results` carries it formatted with commas.
    The picker dialog calls this once per row before display."""
    out = dict(work)
    out.setdefault("title", "")
    out.setdefault("url", "")
    out.setdefault("author", "")
    out.setdefault("summary", "")
    out.setdefault("words", "")
    out.setdefault("chapters", "")
    out.setdefault("rating", "")
    out.setdefault("fandom", "")
    out.setdefault("status", "")
    out.setdefault("updated", "")
    return out


__all__ = [
    "ListPageRef",
    "classify",
    "extract",
]
