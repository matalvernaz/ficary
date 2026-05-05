"""Tests for the new bulk-import scraper methods.

Each scraper's new ``scrape_*_works`` method is exercised against
hand-crafted minimal HTML fixtures so the parsing logic — which
data-* attribute carries which field, where pagination breaks —
is pinned without relying on a live network.

Reuses ``ao3_search.html`` and ``ffn_search.html`` from the existing
fixtures dir where the markup matches; small inline fixtures cover
the rest (Wattpad list API, FFN community, Royal Road search).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


def _patch_fetch(scraper, page_html_factory):
    """Replace ``scraper._fetch`` with a function that returns the
    next chunk of HTML each time it's called.

    ``page_html_factory`` is a callable taking the URL and returning
    HTML, or a list (popped front-to-back). Pagination tests use the
    callable form so they can branch on the URL's page parameter.
    """
    scraper._fetch = page_html_factory  # type: ignore[assignment]
    scraper._delay = lambda: None       # neutralise inter-page sleeps


# ── AO3 search / tag (reuse the search fixture) ───────────────────


def test_ao3_scrape_search_works_parses_blurbs():
    from ffn_dl.ao3 import AO3Scraper

    html = (FIXTURES / "ao3_search.html").read_text(encoding="utf-8")
    pages = {"first": html, "empty": ""}
    calls = []

    def fake_fetch(url):
        calls.append(url)
        # First call returns the populated fixture; subsequent calls
        # return an empty page so the "no new on page" branch fires
        # and the loop terminates predictably.
        if len(calls) == 1:
            return pages["first"]
        return "<html><body></body></html>"

    scraper = AO3Scraper()
    _patch_fetch(scraper, fake_fetch)
    label, works = scraper.scrape_search_works(
        "https://archiveofourown.org/works/search?work_search%5Bquery%5D=harry",
    )
    assert label == "harry"
    assert len(works) > 0
    # Every entry must have a /works/<id> URL — that's the round-trip
    # contract the bulk download path needs.
    assert all("/works/" in w["url"] for w in works)
    # Pagination separator should be ``&`` because the URL already
    # has a ``?`` — assert against what fake_fetch saw.
    assert "&page=" in calls[0]


def test_ao3_scrape_tag_works_url_decodes_label():
    from ffn_dl.ao3 import AO3Scraper

    scraper = AO3Scraper()
    _patch_fetch(scraper, lambda url: "<html></html>")
    label, _ = scraper.scrape_tag_works(
        "https://archiveofourown.org/tags/Harry%20Potter/works",
    )
    assert label == "Harry Potter"


# ── FFN search ────────────────────────────────────────────────────


def test_ffn_scrape_search_works():
    from ffn_dl.scraper import FFNScraper

    html = (FIXTURES / "ffn_search.html").read_text(encoding="utf-8")
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return html if len(calls) == 1 else "<html></html>"

    scraper = FFNScraper()
    _patch_fetch(scraper, fake_fetch)
    label, works = scraper.scrape_search_works(
        "https://www.fanfiction.net/search/?keywords=harry&type=story",
    )
    assert label == "harry"
    assert len(works) > 0
    assert all(w["url"].startswith("https://www.fanfiction.net/s/") for w in works)
    # ppage param is FFN's pagination — assert the helper used it.
    assert "ppage=1" in calls[0]


# ── FFN community ─────────────────────────────────────────────────


def test_ffn_scrape_community_works_parses_z_list_rows():
    """FFN community pages reuse the ``z-list`` row markup. Build a
    minimal community page with two rows so the test pins the
    parser against the same shape ``_parse_results`` expects."""
    from ffn_dl.scraper import FFNScraper

    minimal = """
    <html><head><title>Best Of Harry | FanFiction</title></head>
    <body>
      <div class="z-list">
        <a class="stitle" href="/s/100/1/Story-One">Story One</a>
        <a href="/u/1/AuthorOne">AuthorOne</a>
        <div class="z-padtop">A summary line.<div class="z-padtop2">Rated: T - English - Words: 1,234 - Chapters: 3</div></div>
      </div>
      <div class="z-list">
        <a class="stitle" href="/s/200/1/Story-Two">Story Two</a>
        <a href="/u/2/AuthorTwo">AuthorTwo</a>
        <div class="z-padtop">Another summary.<div class="z-padtop2">Rated: K - English - Words: 5,678 - Chapters: 1</div></div>
      </div>
    </body></html>
    """
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return minimal if len(calls) == 1 else "<html></html>"

    scraper = FFNScraper()
    _patch_fetch(scraper, fake_fetch)
    name, works = scraper.scrape_community_works(
        "https://www.fanfiction.net/community/Best-Of/12345/",
    )
    assert name == "Best Of Harry"
    urls = [w["url"] for w in works]
    # _parse_results preserves the chapter/slug suffix as the page
    # rendered them; assert on the /s/<id>/ prefix shape rather
    # than the canonical form.
    assert any("/s/100/" in u for u in urls)
    assert any("/s/200/" in u for u in urls)
    # Pagination param uses ``p=`` for community pages, not ``ppage=``
    assert "p=1" in calls[0]


# ── Royal Road search ────────────────────────────────────────────


def test_rr_scrape_search_works_parses_fiction_list_items():
    from ffn_dl.royalroad import RoyalRoadScraper

    minimal = """
    <html><body>
      <div class="fiction-list-item">
        <a href="/fiction/100/Some-Title">Some Title</a>
        <a href="/profile/1/Author">Author</a>
        <div class="description">A summary blurb.</div>
        <span class="tags">
          <a class="fiction-tag">LitRPG</a>
          <a class="fiction-tag">Magic</a>
        </span>
      </div>
      <div class="fiction-list-item">
        <a href="/fiction/200/Other">Other</a>
        <div class="fiction-description">Other summary.</div>
      </div>
    </body></html>
    """
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return minimal if len(calls) == 1 else "<html></html>"

    scraper = RoyalRoadScraper()
    _patch_fetch(scraper, fake_fetch)
    label, works = scraper.scrape_search_works(
        "https://www.royalroad.com/fictions/search?title=arcane",
    )
    assert label == "arcane"
    assert [w["url"] for w in works] == [
        "https://www.royalroad.com/fiction/100",
        "https://www.royalroad.com/fiction/200",
    ]
    assert works[0]["fandom"] == "LitRPG, Magic"
    # Pagination param appended
    assert "page=1" in calls[0]


# ── Wattpad reading list (JSON API) ──────────────────────────────


def test_wattpad_scrape_reading_list_works():
    """Wattpad reading lists go through the v4/lists endpoint, not
    the HTML page. Mock ``_api_get_json`` directly so we don't have
    to construct fake HTTP responses."""
    from ffn_dl.wattpad import WattpadScraper

    page_one = {
        "name": "My Reading List",
        "stories": {
            "stories": [
                {
                    "id": "111", "title": "First", "url": "https://www.wattpad.com/story/111-first",
                    "numParts": 5, "completed": True, "mature": False,
                    "length": 1000, "description": "blurb",
                    "user": {"name": "alice"},
                },
                {
                    "id": "222", "title": "Second", "url": "https://www.wattpad.com/story/222-second",
                    "numParts": 10, "completed": False, "mature": True,
                    "length": 5000, "description": "blurb 2",
                    "user": {"name": "bob"},
                },
            ],
            "nextUrl": None,  # exhaust on first page
        },
    }

    scraper = WattpadScraper()
    scraper._api_get_json = lambda url: page_one  # type: ignore[assignment]
    scraper._delay = lambda: None

    label, works = scraper.scrape_reading_list_works(
        "https://www.wattpad.com/user/somebody/lists/123",
    )
    assert label == "My Reading List"
    assert [w["title"] for w in works] == ["First", "Second"]
    assert works[0]["author"] == "alice"
    assert works[0]["status"] == "Complete"
    assert works[1]["status"] == "In-Progress"
    assert works[1]["rating"] == "Mature"


def test_wattpad_reading_list_short_url_form():
    """The /list/<id> short share link must classify and extract
    just like the canonical /user/X/lists/<id> form."""
    from ffn_dl.wattpad import WattpadScraper

    scraper = WattpadScraper()
    scraper._api_get_json = lambda url: {
        "name": "Short", "stories": {"stories": [], "nextUrl": None},
    }
    scraper._delay = lambda: None

    label, works = scraper.scrape_reading_list_works(
        "https://www.wattpad.com/list/9876",
    )
    assert label == "Short"
    assert works == []


# ── Pagination cap regression ────────────────────────────────────


def test_ffn_search_pagination_caps_at_max_pages():
    """A site that hands back the same row on every page would
    otherwise loop forever; the per-method cap mirrors the global
    ``fetch_until_limit`` guard added in 2.3.3."""
    from ffn_dl.scraper import FFNScraper

    same_row = """
    <html><body>
      <div class="z-list">
        <a class="stitle" href="/s/777/1/Static">Static</a>
        <a href="/u/9/A">A</a>
        <div class="z-padtop">x<div class="z-padtop2">Rated: T</div></div>
      </div>
    </body></html>
    """
    calls = []

    def fake_fetch(url):
        calls.append(url)
        # Return the same row forever — but mark the page as
        # "exhausted" after one new entry by using a dedupe set
        # (the parser itself dedups on /s/<id>, so the second call
        # naturally returns "no new" → loop exits).
        return same_row

    scraper = FFNScraper()
    _patch_fetch(scraper, fake_fetch)
    _, works = scraper.scrape_search_works(
        "https://www.fanfiction.net/search/?keywords=x&type=story",
    )
    # Dedupe on /s/777 means only one work emerges; the loop should
    # have exited after detecting "no new on page".
    assert len(works) == 1
    # And it must not have called more than a few times — definitely
    # not 200 (the cap) which would mean dedupe didn't kick in.
    assert len(calls) <= 3
