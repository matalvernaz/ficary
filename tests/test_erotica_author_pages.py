"""Author-page scraping for the erotica adapters (round-10 F4).

Offline: each scraper's _fetch is monkeypatched to serve the captured
fixture. BDSM Library deliberately has no author scraping — its
author.php pages render empty server-side (see bdsmlibrary.py)."""
from pathlib import Path

import pytest

from ficary import sites, url_classifier
from ficary.erotica.aff import AFFScraper
from ficary.erotica.sexstories import SexStoriesScraper
from ficary.erotica.storiesonline import StoriesOnlineScraper

FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def _serve(scraper, fixture, monkeypatch):
    html = (FIXTURES / fixture).read_text(encoding="utf-8", errors="replace")
    monkeypatch.setattr(scraper, "_fetch", lambda url, **kw: html)
    monkeypatch.setattr(scraper, "_delay", lambda *a, **kw: None)
    return scraper


def _serve_aff(monkeypatch):
    """AFF's own "Stories Written" list is JS-loaded per fandom sub-tab.
    Dispatch the profile page vs the per-subdomain load-user-stories.php
    fragments, so the scraper is exercised against its real data path."""
    import re as _re
    s = AFFScraper(use_cache=False)
    frag = {
        "hp": "aff_userstories_hp.html",
        "anime": "aff_userstories_anime.html",
        "original": "aff_userstories_original.html",
    }

    def fetch(url, **kw):
        u = str(url)
        if "load-user-stories.php" in u:
            m = _re.search(r"subdomain=([a-z0-9-]+)", u)
            name = frag.get(m.group(1)) if m else None
            return ((FIXTURES / name).read_text(encoding="utf-8", errors="replace")
                    if name else "<div class='story-list'></div>")
        return (FIXTURES / "aff_profile.html").read_text(
            encoding="utf-8", errors="replace")

    monkeypatch.setattr(s, "_fetch", fetch)
    monkeypatch.setattr(s, "_delay", lambda *a, **kw: None)
    return s


class TestAFFAuthor:
    URL = "https://members.adult-fanfiction.org/profile.php?id=1296987001"

    def test_is_author_url(self):
        assert AFFScraper.is_author_url(self.URL)
        assert AFFScraper.is_author_url(
            "https://hp.adult-fanfiction.org/authorlinks.php?no=12345")
        assert not AFFScraper.is_author_url(
            "https://hp.adult-fanfiction.org/story.php?no=600100488")

    def test_scrape_author_works(self, monkeypatch):
        s = _serve_aff(monkeypatch)
        author, works = s.scrape_author_works(self.URL)
        assert author == "Wilde_Guess"
        # The member's real works, from the AJAX endpoint (hp 4 + anime 1
        # + original 2) — NOT the profile's Recommendations/Reading.
        assert len(works) == 7
        assert {w["fandom"] for w in works} == {"hp", "anime", "original"}
        assert all("story.php?no=" in w["url"] for w in works)
        assert all(w["author"] == "Wilde_Guess" for w in works)
        # A real authored work is present; a recommendation id from the
        # profile page is not.
        assert any("600100488" in w["url"] for w in works)   # "Reflections."
        assert not any("600096908" in w["url"] for w in works)  # a recommendation
        urls = [w["url"] for w in works]
        assert len(urls) == len(set(urls))  # deduped across subdomains

    def test_max_results_caps(self, monkeypatch):
        s = _serve_aff(monkeypatch)
        _, works = s.scrape_author_works(self.URL, max_results=3)
        assert len(works) == 3

    def test_cli_shape(self, monkeypatch):
        s = _serve_aff(monkeypatch)
        author, urls = s.scrape_author_stories(self.URL)
        assert author == "Wilde_Guess"
        assert len(urls) == 7 and all(isinstance(u, str) for u in urls)

    def test_sites_predicate_and_classifier(self):
        assert sites.is_author_url(self.URL)
        ref = url_classifier.classify(self.URL)
        assert ref is not None
        assert ref.kind == "author_works"
        assert ref.scraper_cls is AFFScraper


class TestSOLAuthor:
    URL = "https://storiesonline.net/a/fan-fiction-man"

    def test_scrape_author_works(self, monkeypatch):
        s = _serve(StoriesOnlineScraper(use_cache=False), "sol_author.html",
                   monkeypatch)
        author, works = s.scrape_author_works(self.URL)
        assert author == "Fan Fiction Man"
        assert len(works) == 10  # one fixture page; page 2 repeats -> stop
        assert all(w["url"].startswith("https://storiesonline.net/s/") for w in works)
        titles = [w["title"] for w in works]
        assert "Anakin’s Redemption" in titles

    def test_pagination_stops_on_no_new(self, monkeypatch):
        calls = []
        html = (FIXTURES / "sol_author.html").read_text(encoding="utf-8",
                                                        errors="replace")
        s = StoriesOnlineScraper(use_cache=False)
        monkeypatch.setattr(s, "_delay", lambda *a, **kw: None)

        def fetch(url, **kw):
            calls.append(url)
            return html

        monkeypatch.setattr(s, "_fetch", fetch)
        s.scrape_author_works(self.URL)
        assert len(calls) == 2  # page 2 added nothing new -> walk stopped

    def test_classifier(self):
        ref = url_classifier.classify(self.URL)
        assert ref is not None and ref.kind == "author_works"
        assert ref.scraper_cls is StoriesOnlineScraper


class TestSexStoriesAuthor:
    """Author scraping was dropped in 2.8.0: a SexStories profile only
    exposes "Favorites of <member>" (other people's stories) — no
    per-author works listing exists — so the v2.7.0 scraper mislabelled
    favourites as the member's works. Now unsupported, like BDSM Library."""

    URL = "https://sexstories.com/profile1176433/"

    def test_author_scraping_unsupported(self):
        # The override was dropped (2.8.0); SexStories now inherits
        # BaseScraper's NotImplementedError default (like BDSM Library)
        # instead of returning the member's favourites as their works. The
        # CLI/GUI author handlers catch NotImplementedError (see cli.py's
        # author/bulk handlers, gui.py:2945) so this is a clean "not
        # supported", not a crash.
        s = SexStoriesScraper(use_cache=False)
        assert SexStoriesScraper.is_author_url(self.URL)  # still recognised
        with pytest.raises(NotImplementedError):
            s.scrape_author_works(self.URL)
        with pytest.raises(NotImplementedError):
            s.scrape_author_stories(self.URL)
