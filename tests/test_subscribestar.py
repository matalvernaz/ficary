"""SubscribeStar scraper tests.

The creator fixture is a real (script-stripped) authenticated feed page
for one creator; the gdoc fixture is a real link-shared Google Doc HTML
export. Network calls (``_fetch``) are monkeypatched so the parsing,
grouping, and merge logic are exercised offline.
"""

from pathlib import Path

import pytest

from ficary.subscribestar import (
    SubscribeStarScraper,
    _base_title,
    _part_number,
)
from ficary.sites import detect_scraper

FIXTURES = Path(__file__).parent / "fixtures"
CREATOR = (FIXTURES / "subscribestar_creator.html").read_text(encoding="utf-8")
GDOC = (FIXTURES / "subscribestar_gdoc.html").read_text(encoding="utf-8")


class TestURLParsing:
    def test_parse_post_id(self):
        assert SubscribeStarScraper.parse_story_id(
            "https://subscribestar.adult/posts/2563358"
        ) == 2563358
        assert SubscribeStarScraper.parse_story_id("2563358") == 2563358

    def test_parse_rejects_creator_url(self):
        with pytest.raises(ValueError):
            SubscribeStarScraper.parse_story_id(
                "https://subscribestar.adult/fibaro"
            )

    def test_is_author_url_creator_vs_chrome(self):
        assert SubscribeStarScraper.is_author_url(
            "https://subscribestar.adult/fibaro"
        )
        assert not SubscribeStarScraper.is_author_url(
            "https://subscribestar.adult/feed"
        )
        assert not SubscribeStarScraper.is_author_url(
            "https://subscribestar.adult/posts/123"
        )

    def test_detect_scraper_routes_post_url(self):
        assert detect_scraper(
            "https://subscribestar.adult/posts/2563358"
        ) is SubscribeStarScraper

    def test_gdoc_id_matches_both_document_and_file_forms(self):
        from ficary.subscribestar import _GDOC_ID_RE
        assert _GDOC_ID_RE.search(
            "https://docs.google.com/document/d/ABC123_-/edit"
        ).group(1) == "ABC123_-"
        # Uploaded Word files use /file/d/ — must also match.
        assert _GDOC_ID_RE.search(
            "https://docs.google.com/file/d/XYZ789/edit?filetype=msword"
        ).group(1) == "XYZ789"


class TestGrouping:
    def test_base_title_strips_part_marker(self):
        assert _base_title("From Soccer to Sucker Pt.86") == "from soccer to sucker"
        assert _base_title("Wrecking the Homewrecker Pt. 3") == "wrecking the homewrecker"
        assert _base_title("Maid Mommy Chapter 40") == "maid mommy"

    def test_base_title_normalises_punctuation_drift(self):
        # The creator's punctuation drifts between posts; both must group.
        assert _base_title("Goodbye Brother Hello Sis-sy Pt.35") == \
            _base_title("Goodbye Brother, Hello Sis-sy Pt.36")

    def test_part_number(self):
        assert _part_number("From Soccer to Sucker Pt.86") == 86
        assert _part_number("Wrecking the Homewrecker Pt. 3") == 3
        assert _part_number("No number here") is None


class TestPostParsing:
    def test_parse_posts_from_creator_fixture(self):
        from bs4 import BeautifulSoup
        posts = SubscribeStarScraper._parse_posts(BeautifulSoup(CREATOR, "lxml"))
        assert posts
        titled = [p for p in posts if p["title"]]
        assert titled
        # At least one post links a Google Doc via data-href.
        assert any("docs.google.com" in (p["doc_url"] or "") for p in posts)
        # The known story is present.
        assert any("Soccer to Sucker" in p["title"] for p in posts)

    def test_next_page_url_extracted(self):
        from bs4 import BeautifulSoup
        nxt = SubscribeStarScraper._next_page_url(BeautifulSoup(CREATOR, "lxml"))
        assert nxt and "/posts?" in nxt and "slug=fibaro" in nxt

    def test_doc_link_post_has_no_inline_body(self):
        # Fibaro's posts are a title + a bare doc link; the inline-body
        # extractor must NOT treat the URL text as prose.
        from bs4 import BeautifulSoup
        posts = SubscribeStarScraper._parse_posts(BeautifulSoup(CREATOR, "lxml"))
        doc_posts = [p for p in posts if p["doc_url"]]
        assert doc_posts
        assert all(p["body_html"] == "" for p in doc_posts)

    def test_inline_prose_post_captured_as_body(self):
        # The common pattern: prose written straight into the post, no
        # Google Doc. Must be captured as body_html (regression guard for
        # the Fibaro-only bug where inline stories were silently dropped).
        from bs4 import BeautifulSoup
        html = """
        <div class="post" data-id="9">
          <div class="trix-content">
            <h1>Inline Tale Pt.1</h1>
            <p>The first paragraph of real prose, long enough to count.</p>
            <p>A second paragraph so this is unmistakably a chapter body.</p>
          </div>
        </div>
        """
        [post] = SubscribeStarScraper._parse_posts(BeautifulSoup(html, "lxml"))
        assert post["doc_url"] is None
        assert "first paragraph of real prose" in post["body_html"]
        assert "<h1>" not in post["body_html"]  # title heading stripped

    def test_post_without_heading_has_empty_title(self):
        from bs4 import BeautifulSoup
        html = ('<div class="post" data-id="1"><div class="trix-content">'
                '<p>Just an announcement, no heading.</p></div></div>')
        [post] = SubscribeStarScraper._parse_posts(BeautifulSoup(html, "lxml"))
        assert post["title"] == ""

    def test_locked_post_without_trix_is_skipped(self):
        from bs4 import BeautifulSoup
        html = ('<div class="post" data-id="1">'
                '<div class="post-locked">Subscribe to see this</div></div>')
        assert SubscribeStarScraper._parse_posts(BeautifulSoup(html, "lxml")) == []


class TestGoogleDocFetch:
    def test_fetch_gdoc_html_cleans_export(self, monkeypatch):
        sc = SubscribeStarScraper()
        monkeypatch.setattr(sc, "_fetch", lambda url, session=None: GDOC)
        html = sc._fetch_gdoc_html(
            "https://docs.google.com/document/d/10vITtt1Svx4rdojlOjkVGcuBsyfHEsc0/edit"
        )
        assert html and "<p>" in html
        assert "<style" not in html.lower()
        # Leading bare "Chapter N" heading is dropped as a duplicate.
        assert not html.lstrip().lower().startswith("<p>chapter ")

    def test_fetch_gdoc_html_none_on_bad_url(self):
        sc = SubscribeStarScraper()
        assert sc._fetch_gdoc_html("https://example.com/not-a-doc") is None


class TestStoryMerge:
    def test_download_creator_story_merges_in_part_order(self, monkeypatch):
        sc = SubscribeStarScraper()
        # Interleaved feed: two stories, parts out of order, plus a
        # decoy story that must not be included.
        fake_posts = [
            {"id": "5", "title": "My Tale Pt.3", "doc_url": "d3"},
            {"id": "4", "title": "Other Story Pt.9", "doc_url": "dX"},
            {"id": "3", "title": "My Tale Pt.1", "doc_url": "d1"},
            {"id": "2", "title": "My Tale Pt.2", "doc_url": "d2"},
            {"id": "1", "title": "My Tale Pt.4", "doc_url": None},  # no doc → skip
        ]
        monkeypatch.setattr(sc, "_enumerate_posts", lambda handle: fake_posts)
        monkeypatch.setattr(
            sc, "_fetch_gdoc_html", lambda url: f"<p>body {url}</p>",
        )
        story = sc.download_creator_story(
            "https://subscribestar.adult/someone", "My Tale",
        )
        # Only My Tale parts with docs, in ascending part order.
        assert [c.number for c in story.chapters] == [1, 2, 3]
        assert [c.title for c in story.chapters] == ["Part 1", "Part 2", "Part 3"]
        assert story.title == "My Tale"
        assert "body d1" in story.chapters[0].html

    def test_download_creator_story_raises_when_no_match(self, monkeypatch):
        from ficary.scraper import StoryNotFoundError
        sc = SubscribeStarScraper()
        monkeypatch.setattr(sc, "_enumerate_posts", lambda handle: [])
        with pytest.raises(StoryNotFoundError):
            sc.download_creator_story("https://subscribestar.adult/x", "Nope")

    def test_download_creator_story_uses_inline_when_no_doc(self, monkeypatch):
        # A creator who writes inline (no Google Docs) must still merge —
        # this is the pattern Fibaro-only code silently dropped.
        sc = SubscribeStarScraper()
        fake_posts = [
            {"id": "2", "title": "Inline Tale Pt.2", "doc_url": None,
             "body_html": "<p>part two prose</p>"},
            {"id": "1", "title": "Inline Tale Pt.1", "doc_url": None,
             "body_html": "<p>part one prose</p>"},
        ]
        monkeypatch.setattr(sc, "_enumerate_posts", lambda h: fake_posts)
        story = sc.download_creator_story(
            "https://subscribestar.adult/someone", "Inline Tale",
        )
        assert [c.number for c in story.chapters] == [1, 2]
        assert "part one prose" in story.chapters[0].html

    def test_resolve_body_prefers_doc_over_inline(self, monkeypatch):
        sc = SubscribeStarScraper()
        monkeypatch.setattr(sc, "_fetch_gdoc_html", lambda u: "<p>from doc</p>")
        # doc present → doc wins
        assert sc._resolve_body(
            {"doc_url": "https://docs.google.com/document/d/X/edit",
             "body_html": "<p>inline</p>"}
        ) == "<p>from doc</p>"
        # doc fetch fails → fall back to inline
        monkeypatch.setattr(sc, "_fetch_gdoc_html", lambda u: None)
        assert sc._resolve_body(
            {"doc_url": "https://docs.google.com/document/d/X/edit",
             "body_html": "<p>inline</p>"}
        ) == "<p>inline</p>"
        # neither → None
        assert sc._resolve_body({"doc_url": None, "body_html": ""}) is None
