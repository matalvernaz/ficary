"""ScribbleHub scraper tests.

Fixtures are real archived pages (Wayback ``id_`` raw captures) for
series 1000564. The full table of contents is served by ScribbleHub's
AJAX endpoint, which can't be archived, so the TOC parser is exercised
both on the series page (which embeds only recent chapters) and on a
synthetic AJAX fragment matching the site's ``li.toc_w > a.toc_a``
shape.
"""

from pathlib import Path

import pytest

from bs4 import BeautifulSoup

from ficary.scribblehub import ScribbleHubScraper
from ficary.sites import detect_scraper

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = (FIXTURES / "scribblehub_series.html").read_text(encoding="utf-8")
CHAPTER = (FIXTURES / "scribblehub_chapter.html").read_text(encoding="utf-8")


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert ScribbleHubScraper.parse_story_id("1000564") == 1000564

    def test_parses_series_url(self):
        assert ScribbleHubScraper.parse_story_id(
            "https://www.scribblehub.com/series/1000564/apokalypsis-lost-magic-awakening/"
        ) == 1000564

    def test_parses_read_chapter_url(self):
        assert ScribbleHubScraper.parse_story_id(
            "https://www.scribblehub.com/read/1000564-apokalypsis-lost-magic-awakening/chapter/1000566/"
        ) == 1000564

    def test_rejects_unrelated_url(self):
        import pytest
        with pytest.raises(ValueError):
            ScribbleHubScraper.parse_story_id("https://example.com/x")

    def test_is_author_url(self):
        assert ScribbleHubScraper.is_author_url(
            "https://www.scribblehub.com/profile/152348/kuyaimbo/"
        )
        assert not ScribbleHubScraper.is_author_url(
            "https://www.scribblehub.com/series/1000564/x/"
        )

    def test_detect_scraper_routes_scribblehub(self):
        assert detect_scraper(
            "https://www.scribblehub.com/series/1000564/x/"
        ) is ScribbleHubScraper
        assert detect_scraper(
            "https://www.scribblehub.com/read/1000564-x/chapter/1000566/"
        ) is ScribbleHubScraper


class TestMetadata:
    def test_extracts_title_author_summary(self):
        soup = BeautifulSoup(SERIES, "lxml")
        meta = ScribbleHubScraper._parse_metadata(soup)
        assert meta["title"] == "APOKALYPSIS: Lost Magic Awakening"
        assert meta["author"] == "KuyaImbo"
        assert meta["summary"]
        assert "/profile/" in meta["author_url"]

    def test_genres_captured_as_fandoms(self):
        soup = BeautifulSoup(SERIES, "lxml")
        meta = ScribbleHubScraper._parse_metadata(soup)
        assert "Fantasy" in meta["extra"]["fandoms"]


class TestChapterList:
    def test_series_page_embeds_recent_chapters(self):
        soup = BeautifulSoup(SERIES, "lxml")
        chapters = ScribbleHubScraper._parse_toc_anchors(soup)
        assert chapters
        for ch in chapters:
            assert isinstance(ch["id"], int)
            assert ch["title"]
            assert "/chapter/" in ch["url"]
            assert ch["url"].startswith("https://www.scribblehub.com/")

    def test_toc_is_chronological_oldest_first(self):
        soup = BeautifulSoup(SERIES, "lxml")
        chapters = ScribbleHubScraper._parse_toc_anchors(soup)
        # Titles carry "Chapter N:"; the parsed order should ascend.
        import re
        nums = []
        for ch in chapters:
            m = re.search(r"Chapter\s+(\d+)", ch["title"])
            if m:
                nums.append(int(m.group(1)))
        assert nums == sorted(nums), "TOC not oldest-first after reverse"

    def test_ajax_fragment_shape_parses(self):
        # The AJAX endpoint returns bare <li class="toc_w"> rows; the same
        # parser must handle them. Newest-first in, oldest-first out.
        fragment = """
        <ol class="toc_ol">
          <li class="toc_w"><a class="toc_a"
             href="https://www.scribblehub.com/read/1-x/chapter/30/">Chapter 3</a></li>
          <li class="toc_w"><a class="toc_a"
             href="https://www.scribblehub.com/read/1-x/chapter/20/">Chapter 2</a></li>
          <li class="toc_w"><a class="toc_a"
             href="https://www.scribblehub.com/read/1-x/chapter/10/">Chapter 1</a></li>
        </ol>
        """
        soup = BeautifulSoup(fragment, "lxml")
        chapters = ScribbleHubScraper._parse_toc_anchors(soup)
        assert [c["id"] for c in chapters] == [10, 20, 30]
        assert [c["title"] for c in chapters] == [
            "Chapter 1", "Chapter 2", "Chapter 3",
        ]

    def test_toc_dedupes_repeated_anchors(self):
        fragment = """
        <div>
          <a class="toc_a" href="/read/1-x/chapter/10/">A</a>
          <a class="toc_a" href="/read/1-x/chapter/10/">A dup</a>
          <a class="toc_a" href="/read/1-x/chapter/20/">B</a>
        </div>
        """
        soup = BeautifulSoup(fragment, "lxml")
        chapters = ScribbleHubScraper._parse_toc_anchors(soup)
        assert [c["id"] for c in chapters] == [20, 10]  # reversed, deduped


class TestTocPaging:
    def test_full_toc_accumulates_across_ajax_pages(self, monkeypatch):
        """A long series whose AJAX TOC is paginated must be walked to the
        end, not truncated at page 1."""
        from bs4 import BeautifulSoup

        def page(ids):
            lis = "".join(
                f'<a class="toc_a" href="https://www.scribblehub.com/read/1-x/'
                f'chapter/{i}/">Chapter {i}</a>' for i in ids
            )
            return f"<ol>{lis}</ol>"

        # Two populated pages then an empty one (loop should stop there).
        pages = {
            "1": page([30, 20]),   # newest first
            "2": page([10]),
            "3": "",
        }

        class FakeResp:
            status_code = 200
            def __init__(self, text): self.text = text

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=None):
                return FakeResp(pages.get(data["pagenum"], ""))

        sc = ScribbleHubScraper()
        monkeypatch.setattr(sc, "_session", lambda: FakeSession())
        monkeypatch.setattr(sc, "_delay", lambda *a, **k: None)
        chapters, enumerated = sc._fetch_full_toc(
            1, BeautifulSoup("<html></html>", "lxml"),
        )
        # All three chapters, oldest-first.
        assert [c["id"] for c in chapters] == [10, 20, 30]
        assert enumerated is True

    def test_full_toc_falls_back_to_embedded_when_ajax_empty(self, monkeypatch):
        from bs4 import BeautifulSoup
        embedded = BeautifulSoup(
            '<a class="toc_a" href="https://www.scribblehub.com/read/1-x/'
            'chapter/5/">Chapter 5</a>', "lxml",
        )

        class FakeResp:
            status_code = 200
            text = ""

        class FakeSession:
            def post(self, *a, **k): return FakeResp()

        sc = ScribbleHubScraper()
        monkeypatch.setattr(sc, "_session", lambda: FakeSession())
        monkeypatch.setattr(sc, "_delay", lambda *a, **k: None)
        chapters, enumerated = sc._fetch_full_toc(1, embedded)
        assert [c["id"] for c in chapters] == [5]
        # An immediately-empty AJAX response is a complete enumeration:
        # the endpoint had nothing to add to the embedded list.
        assert enumerated is True


class TestChapterBody:
    def test_extracts_chapter_prose(self):
        soup = BeautifulSoup(CHAPTER, "lxml")
        html = ScribbleHubScraper._parse_chapter_html(soup)
        assert "<p" in html
        assert len(html) > 500

    def test_missing_body_raises(self):
        import pytest
        soup = BeautifulSoup("<html><body>no body here</body></html>", "lxml")
        with pytest.raises(ValueError):
            ScribbleHubScraper._parse_chapter_html(soup)

    def test_author_notes_stripped_from_body(self):
        html = """
        <div id="chp_raw">
          <p>Real prose.</p>
          <div class="wi_authornotes"><p>Thanks for reading, subscribe!</p></div>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        out = ScribbleHubScraper._parse_chapter_html(soup)
        assert "Real prose." in out
        assert "subscribe" not in out.lower()


# ── 2026-11-23: the slug-less series address answers 404 ─────────


class TestSeriesAddress:
    """ScribbleHub stopped serving ``/series/<id>/`` (it answers 404, where
    it used to redirect), so every download and update check said "Not
    found". Pages are fetched from ``/?p=<id>``, which WordPress redirects
    to the series page whatever its slug. The address recorded for the
    story stays the slug-less one: it is the story's identity in the
    library."""

    def _scraper_recording_fetches(self, monkeypatch):
        sc = ScribbleHubScraper(use_cache=False)
        fetched = []

        def fetch(url, *args, **kwargs):
            fetched.append(url)
            return SERIES

        monkeypatch.setattr(sc, "_fetch", fetch)
        return sc, fetched

    def test_the_chapter_count_fetches_the_address_that_redirects(self, monkeypatch):
        sc, fetched = self._scraper_recording_fetches(monkeypatch)
        monkeypatch.setattr(sc, "_fetch_full_toc", lambda story_id, soup: ([{"id": 1}] * 3, True))
        sc.get_chapter_count("https://www.scribblehub.com/series/1000564/apokalypsis-lost-magic-awakening/")
        assert fetched == ["https://www.scribblehub.com/?p=1000564"]

    def test_a_download_fetches_it_too_and_keeps_the_story_address(self, monkeypatch):
        sc, fetched = self._scraper_recording_fetches(monkeypatch)
        chapters = [{"id": number, "title": f"Chapter {number}", "url": f"https://www.scribblehub.com/read/1000564-x/chapter/{number}/"} for number in (1, 2)]
        monkeypatch.setattr(sc, "_fetch_full_toc", lambda story_id, soup: (chapters, True))
        monkeypatch.setattr(sc, "_parse_metadata", lambda soup: {"title": "T", "author": "A", "summary": "", "declared_chapters": 2})
        monkeypatch.setattr(sc, "_materialise_chapters", lambda **kwargs: [])
        story = sc.download("https://www.scribblehub.com/series/1000564/apokalypsis-lost-magic-awakening/")
        assert fetched == ["https://www.scribblehub.com/?p=1000564"]
        assert story.url == "https://www.scribblehub.com/series/1000564/"

    def test_the_chapter_list_request_comes_from_the_page_itself(self, monkeypatch):
        sc = ScribbleHubScraper(use_cache=False)
        sent = []

        class FakeResp:
            status_code = 200
            text = ""
            headers = {}

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=None):
                sent.append(headers)
                return FakeResp()

        monkeypatch.setattr(sc, "_session", lambda: FakeSession())
        monkeypatch.setattr(sc, "_delay", lambda: None)
        sc._fetch_full_toc(1000564, BeautifulSoup(SERIES, "lxml"))
        assert sent[0]["Referer"] == "https://www.scribblehub.com/series/1000564/apokalypsis-lost-magic-awakening/"


class TestBlockedChapterList:
    def _download_with_toc_answer(self, monkeypatch, status, headers):
        from ficary.scribblehub import PartialTableOfContentsError

        sc = ScribbleHubScraper(use_cache=False)
        monkeypatch.setattr(sc, "_fetch", lambda url, *a, **k: SERIES)
        monkeypatch.setattr(sc, "_delay", lambda: None)

        class FakeResp:
            status_code = status
            text = "<html><title>Just a moment...</title></html>"

        FakeResp.headers = headers

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=None):
                return FakeResp()

        monkeypatch.setattr(sc, "_session", lambda: FakeSession())
        with pytest.raises(PartialTableOfContentsError) as error:
            sc.download("https://www.scribblehub.com/series/1000564/apokalypsis-lost-magic-awakening/")
        return str(error.value)

    def test_a_cloudflare_challenge_says_what_gets_past_it(self, monkeypatch):
        """Waiting does not get past a challenge; the cookie does."""
        message = self._download_with_toc_answer(monkeypatch, 403, {"cf-mitigated": "challenge", "server": "cloudflare"})
        assert "Cloudflare" in message
        assert "Preferences > Downloads, ScribbleHub cookie" in message
        assert "Wait a moment" not in message

    def test_another_failure_still_says_to_try_again(self, monkeypatch):
        message = self._download_with_toc_answer(monkeypatch, 503, {"server": "nginx"})
        assert "Wait a moment and try again" in message
