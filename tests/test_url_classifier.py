"""Tests for ffn_dl.url_classifier.

Covers the dispatch table — which URL maps to which scraper + which
extractor — across all the list-page shapes the bulk-import feature
supports. Each row is a real URL form harvested from the live sites.
"""

from __future__ import annotations

import pytest

from ffn_dl import url_classifier
from ffn_dl.ao3 import AO3Scraper
from ffn_dl.scraper import FFNScraper
from ffn_dl.royalroad import RoyalRoadScraper
from ffn_dl.wattpad import WattpadScraper


# (url, expected_scraper_cls, expected_kind, expected_extractor)
TABLE = [
    # AO3 — author root, works tab, bookmarks, series, tag, search
    (
        "https://archiveofourown.org/users/someuser",
        AO3Scraper, "author_works", "scrape_author_works",
    ),
    (
        "https://archiveofourown.org/users/someuser/works",
        AO3Scraper, "author_works", "scrape_author_works",
    ),
    (
        "https://archiveofourown.org/users/someuser/bookmarks",
        AO3Scraper, "author_bookmarks", "scrape_bookmark_works",
    ),
    (
        "https://archiveofourown.org/series/12345",
        AO3Scraper, "series", "scrape_series_works",
    ),
    (
        "https://archiveofourown.org/tags/Harry%20Potter/works",
        AO3Scraper, "tag", "scrape_tag_works",
    ),
    (
        "https://archiveofourown.org/works/search?work_search%5Bquery%5D=foo",
        AO3Scraper, "search", "scrape_search_works",
    ),

    # FFN — author, search, community
    (
        "https://www.fanfiction.net/u/123456/Some-Author",
        FFNScraper, "author_works", "scrape_author_works",
    ),
    (
        "https://www.fanfiction.net/~someauthor",
        FFNScraper, "author_works", "scrape_author_works",
    ),
    (
        "https://www.fanfiction.net/search/?keywords=harry&type=story",
        FFNScraper, "search", "scrape_search_works",
    ),
    (
        "https://www.fanfiction.net/community/Best-Of/12345/",
        FFNScraper, "community", "scrape_community_works",
    ),

    # Wattpad — user, reading list (both shapes)
    (
        "https://www.wattpad.com/user/someuser",
        WattpadScraper, "author_works", "scrape_author_works",
    ),
    (
        "https://www.wattpad.com/user/someuser/lists/12345",
        WattpadScraper, "reading_list", "scrape_reading_list_works",
    ),
    (
        "https://www.wattpad.com/list/12345",
        WattpadScraper, "reading_list", "scrape_reading_list_works",
    ),

    # Royal Road — author profile, search
    (
        "https://www.royalroad.com/profile/12345",
        RoyalRoadScraper, "author_works", "scrape_author_works",
    ),
    (
        "https://www.royalroad.com/fictions/search?title=arcane",
        RoyalRoadScraper, "search", "scrape_search_works",
    ),
]


@pytest.mark.parametrize("url,scraper_cls,kind,extractor", TABLE)
def test_classify_table(url, scraper_cls, kind, extractor):
    ref = url_classifier.classify(url)
    assert ref is not None, f"classify returned None for {url!r}"
    assert ref.url == url
    assert ref.scraper_cls is scraper_cls
    assert ref.kind == kind
    assert ref.extractor == extractor


def test_classify_empty_returns_none():
    assert url_classifier.classify("") is None
    assert url_classifier.classify("   ") is None
    assert url_classifier.classify(None) is None


def test_classify_single_story_returns_story_kind():
    """A bare story URL (not a list page) should classify as
    ``story`` so the GUI can offer the "single fic" path through
    the same dispatch — instead of needing a separate is-it-a-list
    check upstream."""
    ref = url_classifier.classify(
        "https://www.fanfiction.net/s/12345/1/Some-Title",
    )
    assert ref is not None
    assert ref.kind == "story"
    assert ref.extractor == ""


def test_classify_unknown_falls_through_to_unknown():
    ref = url_classifier.classify("https://example.invalid/foo/bar")
    assert ref is not None
    assert ref.kind == "unknown"


def test_bookmarks_takes_precedence_over_author():
    """The AO3 bookmarks URL is a superset of the author URL. The
    classifier must not accidentally match author_works first."""
    ref = url_classifier.classify(
        "https://archiveofourown.org/users/x/bookmarks",
    )
    assert ref is not None
    assert ref.kind == "author_bookmarks"
    assert ref.extractor == "scrape_bookmark_works"


def test_extract_story_kind_returns_one_entry():
    """Single-story URLs round-trip through extract() as a one-row
    list so callers don't need a separate code path."""
    ref = url_classifier.classify(
        "https://www.fanfiction.net/s/12345/1/Some-Title",
    )
    label, works = url_classifier.extract(ref)
    assert label.startswith("https://")
    assert len(works) == 1
    assert works[0]["url"].startswith("https://")


def test_extract_unknown_raises():
    ref = url_classifier.classify("https://example.invalid/foo")
    with pytest.raises(ValueError):
        url_classifier.extract(ref)
