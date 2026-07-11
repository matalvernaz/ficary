"""ScribbleHub scraper tests.

Fixtures are real archived pages (Wayback ``id_`` raw captures) for
series 1000564. The full table of contents is served by ScribbleHub's
AJAX endpoint, which can't be archived, so the TOC parser is exercised
both on the series page (which embeds only recent chapters) and on a
synthetic AJAX fragment matching the site's ``li.toc_w > a.toc_a``
shape.
"""

from pathlib import Path

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
        chapters = sc._fetch_full_toc(1, BeautifulSoup("<html></html>", "lxml"))
        # All three chapters, oldest-first.
        assert [c["id"] for c in chapters] == [10, 20, 30]

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
        chapters = sc._fetch_full_toc(1, embedded)
        assert [c["id"] for c in chapters] == [5]


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
