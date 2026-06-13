"""FicHub fast-path backend — EPUB re-ingest, metadata mapping, API
handling, and the FFNScraper fresh-download-only routing guard.

All offline: the EPUB is built in-memory with ebooklib and the HTTP
layer is a fake session, so nothing here touches fichub.net or FFN.
"""

import pytest

from ffn_dl import fichub
from ffn_dl.models import Story, parse_chapter_spec
from ffn_dl.scraper import FFNScraper


# ── Fixtures: a FicHub-shaped EPUB and API JSON ──────────────────────

def _make_fichub_epub(tmp_path, chapters=(1, 2)):
    """Build a minimal EPUB matching FicHub's layout: a non-chapter
    ``introduction.xhtml`` plus one ``chap_<N>.xhtml`` per chapter, each
    with the title in an ``<h2>`` heading followed by the body."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("test-id")
    book.set_title("Test Fic")
    book.set_language("en")
    book.add_author("Test Author")

    intro = epub.EpubHtml(
        title="Title Page", file_name="introduction.xhtml", lang="en"
    )
    intro.content = "<html><body><h1>Test Fic</h1><p>by Test Author</p></body></html>"
    book.add_item(intro)

    spine = ["nav", intro]
    for num in chapters:
        item = epub.EpubHtml(
            title=f"Chapter {num} Title",
            file_name=f"chap_{num}.xhtml",
            lang="en",
        )
        item.content = (
            f"<html><body><h2>Chapter {num} Title</h2>"
            f"<p></p><div><p>Body of chapter {num} here.</p></div>"
            "</body></html>"
        )
        book.add_item(item)
        spine.append(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    out = tmp_path / "fic.epub"
    epub.write_epub(str(out), book)
    return out.read_bytes()


SAMPLE_META = {
    "title": "Test Fic",
    "author": "Test Author",
    "authorUrl": "https://www.fanfiction.net/u/999/Test-Author",
    "description": "<p>A <b>great</b> story.</p>",
    "source": "https://www.fanfiction.net/s/12345/1/Test-Fic",
    "chapters": 2,
    "status": "complete",
    "words": 1234,
    "rawExtendedMeta": {
        "id": "12345",
        "words": "1,234",
        "chapters": "2",
        "status": "Complete",
        "rated": "T",
        "language": "English",
        "genres": "Drama/Humor",
        "characters": "Harry P., Hermione G.",
        "raw_fandom": "Harry Potter",
        "reviews": "100",
        "favorites": "200",
        "follows": "150",
        "published": "1267344759",
        "updated": "1426348782",
    },
}


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Routes the API call vs the EPUB download by URL substring."""

    def __init__(self, api_resp, epub_resp=None):
        self._api_resp = api_resp
        self._epub_resp = epub_resp
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "api/v0/epub" in url:
            return self._api_resp
        return self._epub_resp


# ── EPUB re-ingest ───────────────────────────────────────────────────

class TestStoryFromEpub:
    def test_parses_all_chapters(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path, chapters=(1, 2, 3))
        story = fichub._story_from_epub(
            epub_bytes, ffn_url="https://www.fanfiction.net/s/12345/",
            meta=SAMPLE_META,
        )
        assert story is not None
        assert [c.number for c in story.chapters] == [1, 2, 3]
        assert story.chapters[0].title == "Chapter 1 Title"

    def test_strips_heading_keeps_body(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path, chapters=(1,))
        story = fichub._story_from_epub(
            epub_bytes, ffn_url="x", meta=SAMPLE_META,
        )
        html = story.chapters[0].html
        # The exporter renders its own heading, so FicHub's <h2> title
        # must be gone but the body text retained.
        assert "<h2>" not in html
        assert "Chapter 1 Title" not in html
        assert "Body of chapter 1 here." in html

    def test_respects_chapter_spec(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path, chapters=(1, 2, 3, 4))
        story = fichub._story_from_epub(
            epub_bytes, ffn_url="x", meta=SAMPLE_META,
            chapters_spec=parse_chapter_spec("2-3"),
        )
        assert [c.number for c in story.chapters] == [2, 3]

    def test_no_chapter_docs_returns_none(self, tmp_path):
        # An EPUB whose only document is the non-matching intro page.
        epub_bytes = _make_fichub_epub(tmp_path, chapters=())
        assert fichub._story_from_epub(
            epub_bytes, ffn_url="x", meta=SAMPLE_META,
        ) is None

    def test_garbage_bytes_returns_none(self):
        assert fichub._story_from_epub(
            b"not a real epub", ffn_url="x", meta=SAMPLE_META,
        ) is None

    def test_missing_ebooklib_returns_none(self, tmp_path, monkeypatch):
        # Simulate the [epub] extra not being installed: the fast-path
        # must degrade to None (caller scrapes) rather than crash.
        import sys
        epub_bytes = _make_fichub_epub(tmp_path)
        monkeypatch.setitem(sys.modules, "ebooklib", None)
        assert fichub._story_from_epub(
            epub_bytes, ffn_url="x", meta=SAMPLE_META,
        ) is None

    def test_story_fields_from_meta(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path)
        story = fichub._story_from_epub(
            epub_bytes, ffn_url="https://www.fanfiction.net/s/12345/",
            meta=SAMPLE_META,
        )
        assert story.id == 12345
        assert story.title == "Test Fic"
        assert story.author == "Test Author"
        assert story.author_url.endswith("/u/999/Test-Author")
        # Description HTML is flattened to plain text, matching a scrape.
        assert story.summary == "A great story."

    def test_progress_callback_fires_per_chapter(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path, chapters=(1, 2))
        seen = []
        fichub._story_from_epub(
            epub_bytes, ffn_url="x", meta=SAMPLE_META,
            progress_callback=lambda *a: seen.append(a),
        )
        assert len(seen) == 2
        assert seen[0][0] == 1 and seen[0][1] == 2  # (current, total, ...)


# ── Metadata mapping ─────────────────────────────────────────────────

class TestBuildMetadata:
    def test_maps_raw_extended_fields(self):
        extra = fichub._build_metadata(SAMPLE_META)
        assert extra["words"] == "1,234"
        assert extra["status"] == "Complete"
        assert extra["rating"] == "T"
        assert extra["language"] == "English"
        assert extra["genre"] == "Drama/Humor"
        assert extra["characters"] == "Harry P., Hermione G."
        assert extra["category"] == "Harry Potter"
        assert extra["reviews"] == "100"
        assert extra["favs"] == "200"
        assert extra["follows"] == "150"
        assert extra["date_published"] == 1267344759
        assert extra["date_updated"] == 1426348782

    def test_falls_back_to_top_level(self):
        extra = fichub._build_metadata(
            {"words": 99, "chapters": 5, "status": "ongoing"}
        )
        assert extra["words"] == 99
        assert extra["status"] == "ongoing"

    def test_bad_epoch_dropped(self):
        extra = fichub._build_metadata(
            {"rawExtendedMeta": {"published": "0", "updated": "notanumber"}}
        )
        assert "date_published" not in extra
        assert "date_updated" not in extra


def test_plain_text_strips_html():
    assert fichub._plain_text("<p>hi <b>there</b></p>") == "hi there"
    assert fichub._plain_text(None) == ""


class TestFfnStoryId:
    def test_from_raw_meta(self):
        assert fichub._ffn_story_id("x", SAMPLE_META) == 12345

    def test_from_url_when_meta_absent(self):
        assert fichub._ffn_story_id(
            "https://www.fanfiction.net/s/777/1", {}
        ) == 777

    def test_zero_when_unparseable(self):
        assert fichub._ffn_story_id("not-a-url", {}) == 0


# ── API layer ────────────────────────────────────────────────────────

class TestQueryMeta:
    def test_success_returns_data(self):
        data = {"err": 0, "urls": {"epub": "/cache/x.epub"}}
        sess = _FakeSession(_FakeResponse(200, json_data=data))
        assert fichub.query_meta("http://ffn/x", session=sess) == data

    def test_err_flag_returns_none(self):
        sess = _FakeSession(_FakeResponse(200, json_data={"err": 1}))
        assert fichub.query_meta("http://ffn/x", session=sess) is None

    def test_non_200_returns_none(self):
        sess = _FakeSession(_FakeResponse(503))
        assert fichub.query_meta("http://ffn/x", session=sess) is None

    def test_non_json_returns_none(self):
        sess = _FakeSession(_FakeResponse(200, json_data=None))
        assert fichub.query_meta("http://ffn/x", session=sess) is None


class TestFetchStory:
    def test_full_path_builds_story(self, tmp_path):
        epub_bytes = _make_fichub_epub(tmp_path, chapters=(1, 2))
        api = _FakeResponse(200, json_data={
            "err": 0, "meta": SAMPLE_META,
            "urls": {"epub": "/cache/epub/abc.epub?h=1"},
        })
        sess = _FakeSession(api, _FakeResponse(200, content=epub_bytes))
        story = fichub.fetch_story("https://www.fanfiction.net/s/12345/", session=sess)
        assert story is not None
        assert [c.number for c in story.chapters] == [1, 2]
        # Two requests: the API query then the EPUB download.
        assert len(sess.calls) == 2

    def test_api_miss_returns_none(self):
        sess = _FakeSession(_FakeResponse(200, json_data={"err": 1}))
        assert fichub.fetch_story("http://ffn/x", session=sess) is None

    def test_missing_epub_url_returns_none(self):
        sess = _FakeSession(_FakeResponse(200, json_data={"err": 0, "urls": {}}))
        assert fichub.fetch_story("http://ffn/x", session=sess) is None


# ── FFNScraper routing guard ─────────────────────────────────────────

class TestScraperFastPathGuard:
    """use_fichub only diverts a *fresh* download; updates and the
    flag-off case must fall through to a direct scrape."""

    def _patch(self, monkeypatch):
        fichub_calls, scrape_calls = [], []

        def fake_fetch(url, **kw):
            fichub_calls.append(url)
            return Story(id=1, title="T", author="A", summary="", url=url)

        class _Sentinel(Exception):
            pass

        def fake_fetch_page(self, url, session=None):
            scrape_calls.append(url)
            raise _Sentinel()

        monkeypatch.setattr(fichub, "fetch_story", fake_fetch)
        monkeypatch.setattr(FFNScraper, "_fetch", fake_fetch_page)
        return fichub_calls, scrape_calls, _Sentinel

    def test_fresh_download_uses_fichub(self, monkeypatch):
        fichub_calls, scrape_calls, _ = self._patch(monkeypatch)
        story = FFNScraper(use_fichub=True).download(
            "https://www.fanfiction.net/s/12345/", skip_chapters=0
        )
        assert story.title == "T"
        assert len(fichub_calls) == 1
        assert scrape_calls == []

    def test_update_skips_fichub(self, monkeypatch):
        fichub_calls, scrape_calls, sentinel = self._patch(monkeypatch)
        with pytest.raises(sentinel):
            FFNScraper(use_fichub=True).download(
                "https://www.fanfiction.net/s/12345/", skip_chapters=3
            )
        assert fichub_calls == []
        assert len(scrape_calls) == 1

    def test_flag_off_skips_fichub(self, monkeypatch):
        fichub_calls, scrape_calls, sentinel = self._patch(monkeypatch)
        with pytest.raises(sentinel):
            FFNScraper(use_fichub=False).download(
                "https://www.fanfiction.net/s/12345/", skip_chapters=0
            )
        assert fichub_calls == []
        assert len(scrape_calls) == 1
