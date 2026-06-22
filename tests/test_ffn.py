"""FFN scraper — URL parsing, metadata, search, author URL variants."""

from unittest import mock

from bs4 import BeautifulSoup

import pytest

from ffn_dl.scraper import FFNScraper, StoryNotFoundError
from ffn_dl.search import _parse_results


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert FFNScraper.parse_story_id("12345") == 12345

    def test_parses_story_url(self):
        assert (
            FFNScraper.parse_story_id("https://www.fanfiction.net/s/12345/1/Title")
            == 12345
        )

    def test_is_author_url_matches_canonical(self):
        assert FFNScraper.is_author_url(
            "https://www.fanfiction.net/u/12345/SomeName"
        )

    def test_is_author_url_matches_vanity(self):
        assert FFNScraper.is_author_url(
            "https://www.fanfiction.net/~plums"
        )

    def test_is_author_url_rejects_story_url(self):
        assert not FFNScraper.is_author_url(
            "https://www.fanfiction.net/s/12345"
        )


class TestMetadataParsing:
    def test_metadata_has_title_author_chapters(self, ffn_story_html):
        soup = BeautifulSoup(ffn_story_html, "lxml")
        meta = FFNScraper._parse_metadata(soup)
        assert meta["title"]
        assert meta["author"] != "Unknown Author"
        assert meta["num_chapters"] >= 1
        # Every chapter dropdown entry must produce a title entry
        assert len(meta["chapter_titles"]) == meta["num_chapters"]


class TestDeletedStoryDetection:
    """FFN's deleted-story page used to carry ``<title>Story Not
    Found</title>``; current (2026+) deployments keep the generic
    ``<title>FanFiction</title>`` and put the message in a
    ``<div class=panel_warning>`` → ``<span class='gui_warning'>``
    block instead. ``_check_for_blocks`` must catch both shapes so
    probes on dead stories raise ``StoryNotFoundError`` and the
    library-update path can stamp them as definitively gone."""

    def test_current_panel_warning_shape_raises(
        self, ffn_story_not_found_html,
    ):
        scraper = FFNScraper(use_cache=False)
        with pytest.raises(StoryNotFoundError):
            scraper._check_for_blocks(ffn_story_not_found_html)

    def test_legacy_title_shape_still_raises(self):
        scraper = FFNScraper(use_cache=False)
        legacy = (
            "<html><head><title>Story Not Found</title></head>"
            "<body>gone</body></html>"
        )
        with pytest.raises(StoryNotFoundError):
            scraper._check_for_blocks(legacy)

    def test_live_story_passes_cleanly(self, ffn_story_html):
        scraper = FFNScraper(use_cache=False)
        # Must not raise — a real story page has no Story-Not-Found marker.
        scraper._check_for_blocks(ffn_story_html)


class TestAuthorPageScoping:
    def test_own_stories_excludes_favourites(self):
        """Regression: FFN author pages list own stories in #st_inside
        and favourites in #fs_inside. scrape_author_stories must not
        pick up favourites."""
        html = """
        <html><body>
          <title>SomeAuthor | FanFiction</title>
          <div id="st_inside">
            <a href="/s/111/1/Mine-1">Mine 1</a>
            <a href="/s/222/1/Mine-2">Mine 2</a>
          </div>
          <div id="fs_inside">
            <a href="/s/999/1/Fav-1">Fav 1</a>
            <a href="/s/888/1/Fav-2">Fav 2</a>
          </div>
          <div id="fa"><a href="/u/42">Other Author</a></div>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            name, stories = scraper.scrape_author_stories(
                "https://www.fanfiction.net/u/1"
            )
        ids = [u.rsplit("/", 1)[-1] for u in stories]
        assert ids == ["111", "222"]
        assert "999" not in ids
        assert "888" not in ids

    def test_falls_back_to_full_page_when_container_missing(self):
        """Older or malformed author pages without #st_inside still work
        — we don't want to silently return zero stories."""
        html = """
        <html><body>
          <title>Old Author | FanFiction</title>
          <a href="/s/777/1/Only-Story">Only</a>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            name, stories = scraper.scrape_author_stories(
                "https://www.fanfiction.net/u/2"
            )
        assert len(stories) == 1
        assert stories[0].endswith("/s/777")


class TestAuthorWorks:
    def test_lifts_data_attributes_from_rows(self):
        html = """
        <html><body>
          <title>Writer | FanFiction</title>
          <div id="st_inside">
            <div class="z-list mystories"
                 data-storyid="1" data-title="First Tale"
                 data-wordcount="5000" data-chapters="3" data-statusid="2"
                 data-category="Harry Potter"
                 data-dateupdate="1700000000">
              <a class="stitle" href="/s/1/1">First Tale</a>
              <div class="z-padtop2">Harry Potter - Rated: T - English</div>
            </div>
          </div>
          <div id="fs_inside">
            <div class="z-list"
                 data-storyid="99" data-title="Fave Tale"
                 data-wordcount="1200" data-chapters="1" data-statusid="1">
              <a class="stitle" href="/s/99/1">Fave Tale</a>
              <a href="/u/42">Another Writer</a>
              <div class="z-padtop2">Pokémon - Rated: K</div>
            </div>
          </div>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            name, works = scraper.scrape_author_works(
                "https://www.fanfiction.net/u/1", include_favorites=True,
            )
        assert name == "Writer"
        assert len(works) == 2

        own = next(w for w in works if w["section"] == "own")
        assert own["title"] == "First Tale"
        assert own["url"].endswith("/s/1")
        assert own["words"] == "5000"
        assert own["chapters"] == "3"
        assert own["status"] == "Complete"
        assert own["fandom"] == "Harry Potter"
        assert own["rating"] == "T"
        assert own["updated"]  # ISO date set

        fav = next(w for w in works if w["section"] == "favorites")
        assert fav["title"] == "Fave Tale"
        assert fav["status"] == "In-Progress"
        assert fav["author"] == "Another Writer"

    def test_summary_extracted_from_row(self):
        html = """
        <html><body>
          <title>Writer | FanFiction</title>
          <div id="st_inside">
            <div class="z-list mystories"
                 data-storyid="1" data-title="First"
                 data-wordcount="100" data-chapters="1" data-statusid="1"
                 data-category="X">
              <a class="stitle" href="/s/1/1">First</a>
              <div class="z-indent z-padtop">
                A thrilling blurb about the story.
                <div class="z-padtop2">X - Rated: T</div>
              </div>
            </div>
          </div>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            _, works = scraper.scrape_author_works("https://www.fanfiction.net/u/1")
        assert works[0]["summary"] == "A thrilling blurb about the story."


class TestSearchParsing:
    def test_results_extract_expected_shape(self, ffn_search_html):
        results = _parse_results(ffn_search_html)
        assert results, "search fixture should contain results"
        r0 = results[0]
        expected_keys = {
            "title", "author", "url", "summary", "words",
            "chapters", "rating", "fandom", "status",
        }
        assert expected_keys.issubset(r0.keys())
        # Every result should link to an FFN story URL
        for r in results:
            assert r["url"].startswith("https://www.fanfiction.net/s/")


class TestFFNFandomBrowse:
    """Fandom-browse — the FFN parallel to erotica's tag-browse (round-7
    audit feature). Slug derivation, category resolution, URL build,
    and the search_ffn dispatch path."""

    def test_slug_derivation_canonical_cases(self):
        from ffn_dl.search import _ffn_fandom_slug
        assert _ffn_fandom_slug("Harry Potter") == "Harry-Potter"
        assert _ffn_fandom_slug("Lord of the Rings") == "Lord-of-the-Rings"
        assert (
            _ffn_fandom_slug("Percy Jackson and the Olympians")
            == "Percy-Jackson-and-the-Olympians"
        )
        # Trailing single letter from apostrophe stays lowercase
        # ("Freddy's" → "Freddy-s") — matches FFN's canonical slug.
        assert (
            _ffn_fandom_slug("Five Nights at Freddy's")
            == "Five-Nights-at-Freddy-s"
        )
        # "at" / "by" etc. stay lowercase as connectors
        assert _ffn_fandom_slug("Day at the Beach") == "Day-at-the-Beach"
        assert _ffn_fandom_slug("") == ""

    def test_resolve_uses_curated_index_when_no_category(self):
        from ffn_dl.search import _resolve_fandom
        # "Harry Potter" is in the curated index → pinned book category
        assert _resolve_fandom("Harry Potter", None) == ("book", "Harry-Potter")
        assert _resolve_fandom("Harry Potter", "") == ("book", "Harry-Potter")
        assert _resolve_fandom("Harry Potter", "any") == ("book", "Harry-Potter")

    def test_resolve_picker_bracket_hint(self):
        from ffn_dl.search import _resolve_fandom
        # Picker annotates as "Naruto [anime]" — bracket hint maps to category
        assert _resolve_fandom("Naruto [anime]", None) == ("anime", "Naruto")

    def test_resolve_user_category_overrides_curated(self):
        from ffn_dl.search import _resolve_fandom
        # User explicitly pinned category → overrides the curated entry.
        # Harry Potter as a game (hypothetical) — honour the override.
        assert _resolve_fandom("Harry Potter", "game") == ("game", "Harry-Potter")

    def test_resolve_uncurated_name_auto_detect(self):
        from ffn_dl.search import _resolve_fandom
        # Empty category for an uncurated name → caller must auto-detect
        cat, slug = _resolve_fandom("My Custom Fandom", None)
        assert cat == ""
        assert slug == "My-Custom-Fandom"

    def test_resolve_strips_multi_picker_extras(self):
        from ffn_dl.search import _resolve_fandom
        # Picker joins multi-select with comma — take first.
        # Annotation stripping works for both [] and ().
        assert _resolve_fandom(
            "Harry Potter [book], Naruto [anime]", None,
        ) == ("book", "Harry-Potter")
        assert _resolve_fandom("Bleach (anime)", None) == ("anime", "Bleach")

    def test_resolve_empty_returns_none(self):
        from ffn_dl.search import _resolve_fandom
        assert _resolve_fandom("", "book") is None
        assert _resolve_fandom(None, None) is None
        assert _resolve_fandom("   ", "any") is None

    def test_build_fandom_url_full_filters(self):
        from ffn_dl.search import _build_ffn_fandom_url
        # FFN fandom URLs use short param names: r/srt/g1/g2/len/s/lan/p.
        # Word length uses the ``len`` param (NOT ``words``): "50k+" maps
        # to the nearest fandom bucket, >40K = len 40.
        url = _build_ffn_fandom_url(
            "book", "Harry-Potter",
            {
                "sort": "reviews", "rating": "M", "genre": "romance",
                "genre2": "angst", "min_words": "50k+",
                "status": "complete", "language": "english",
            },
            page=2,
        )
        assert url.startswith("https://www.fanfiction.net/book/Harry-Potter/?")
        # Order isn't guaranteed; check each param appears.
        for fragment in ("srt=3", "r=4", "g1=2", "g2=10", "len=40", "s=2", "lan=1", "p=2"):
            assert fragment in url, f"missing {fragment} in {url}"

    def test_build_fandom_url_no_filters(self):
        from ffn_dl.search import _build_ffn_fandom_url
        # Bare URL when no filters supplied
        url = _build_ffn_fandom_url("anime", "Naruto", {}, page=1)
        assert url == "https://www.fanfiction.net/anime/Naruto/"

    def test_search_ffn_dispatches_to_fandom_when_set(self, monkeypatch):
        """search_ffn should hit the fandom URL path when fandom is set
        and skip the keyword /search/ endpoint."""
        from ffn_dl import search as search_mod

        fetched_urls = []

        def fake_fetch(url):
            fetched_urls.append(url)
            return "<html><body></body></html>"

        monkeypatch.setattr(search_mod, "_fetch_search_page", fake_fetch)
        # Curated fandom → book category
        search_mod.search_ffn("", fandom="Harry Potter")
        assert fetched_urls
        assert "/book/Harry-Potter/" in fetched_urls[-1]
        # Picker output with bracket hint
        fetched_urls.clear()
        search_mod.search_ffn("", fandom="Naruto [anime]")
        assert "/anime/Naruto/" in fetched_urls[-1]

    def test_search_ffn_backfills_fandom_column(self, monkeypatch):
        """Fandom-browse parses the same z-list as keyword search but
        the per-card metadata div omits the fandom name (the whole page
        IS the fandom). Verify _search_ffn_fandom backfills it."""
        from ffn_dl import search as search_mod

        # Minimal valid z-list HTML so _parse_results returns one row
        html = """
        <html><body>
          <div class="z-list">
            <a class="stitle" href="/s/123/1/Title">Title</a>
            <a href="/u/1/Author">Author</a>
            <div class="z-indent">A summary
              <div class="z-padtop2">- Rated: T - English - Romance - Words: 10,000 - Complete</div>
            </div>
          </div>
        </body></html>
        """
        monkeypatch.setattr(
            search_mod, "_fetch_search_page", lambda url: html,
        )
        results = search_mod.search_ffn("", fandom="Harry Potter")
        assert results
        # Slug "Harry-Potter" → display "Harry Potter"
        assert results[0]["fandom"] == "Harry Potter"


class TestSearchFetchRetries:
    """`_fetch_search_page` used to be a one-shot ``Session.get``; a
    transient Cloudflare 403 aborted the whole search. The hardened
    version mirrors the chapter scraper's retry/rotation loop —
    verify a 403→200 sequence resolves instead of raising."""

    def _make_resp(self, status, text="<html></html>"):
        return mock.Mock(status_code=status, text=text)

    def test_recovers_from_transient_403(self, monkeypatch):
        from ffn_dl import search as search_mod
        responses = [
            self._make_resp(403, "forbidden"),
            self._make_resp(200, "<html><body>ok</body></html>"),
        ]
        get_calls = []

        def fake_get(url, timeout=30):
            get_calls.append(url)
            return responses.pop(0)

        def fake_new_session(browser=None):
            sess = mock.Mock()
            sess.get = fake_get
            return sess

        monkeypatch.setattr(search_mod, "_new_search_session", fake_new_session)
        monkeypatch.setattr(
            search_mod, "_seed_search_cf_cookies", lambda sess, url: False,
        )
        monkeypatch.setattr(search_mod.time, "sleep", lambda s: None)

        html = search_mod._fetch_search_page(
            "https://www.fanfiction.net/book/Harry-Potter/?srt=1",
        )
        assert "ok" in html
        assert len(get_calls) == 2

    def test_404_raises_immediately(self, monkeypatch):
        """Fandom auto-detect depends on 404 being terminal so the
        loop can try the next category slug instead of burning the
        retry budget on a wrong URL."""
        from ffn_dl import search as search_mod
        attempts = []

        def fake_get(url, timeout=30):
            attempts.append(url)
            return self._make_resp(404, "not found")

        def fake_new_session(browser=None):
            sess = mock.Mock()
            sess.get = fake_get
            return sess

        monkeypatch.setattr(search_mod, "_new_search_session", fake_new_session)
        monkeypatch.setattr(
            search_mod, "_seed_search_cf_cookies", lambda sess, url: False,
        )
        monkeypatch.setattr(search_mod.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="404"):
            search_mod._fetch_search_page(
                "https://www.fanfiction.net/book/Nonexistent-Fandom/",
            )
        assert len(attempts) == 1

    def test_exhausted_retries_raises_with_last_status(self, monkeypatch):
        from ffn_dl import search as search_mod
        attempts = []

        def fake_get(url, timeout=30):
            attempts.append(url)
            return self._make_resp(403, "still forbidden")

        def fake_new_session(browser=None):
            sess = mock.Mock()
            sess.get = fake_get
            return sess

        monkeypatch.setattr(search_mod, "_new_search_session", fake_new_session)
        monkeypatch.setattr(
            search_mod, "_seed_search_cf_cookies", lambda sess, url: False,
        )
        monkeypatch.setattr(search_mod.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="HTTP 403"):
            search_mod._fetch_search_page(
                "https://www.fanfiction.net/book/Harry-Potter/?srt=1",
            )
        assert len(attempts) == search_mod._SEARCH_FETCH_MAX_RETRIES
