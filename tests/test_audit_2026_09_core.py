"""Regression tests for the 2026-09-08 audit's download and export findings."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ficary.exporters import export_html, resolve_export_path
from ficary.models import Chapter, Story
from ficary.scraper import BaseScraper, chapter_cache_key
from ficary.updater import extract_source_url


def _story(n, title="Same Title", author="Same Author", count=1):
    return Story(
        n, title, author, "", f"https://www.fanfiction.net/s/{n}",
        chapters=[
            Chapter(i, f"Section {i}", f"<p>story {n} chapter {i}</p>")
            for i in range(1, count + 1)
        ],
    )


# ── CORE-01: one story must not overwrite another ──────────────────

def test_two_works_sharing_a_filename_both_survive(tmp_path):
    first = export_html(_story(101), str(tmp_path))
    second = export_html(_story(202), str(tmp_path))
    assert first != second
    assert len(list(tmp_path.glob("*.html"))) == 2
    assert extract_source_url(first).endswith("/101")
    assert extract_source_url(second).endswith("/202")


def test_re_downloading_the_same_story_reuses_its_file(tmp_path):
    first = export_html(_story(101), str(tmp_path))
    again = export_html(_story(101), str(tmp_path))
    assert again == first
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_an_equivalent_url_form_is_the_same_story(tmp_path):
    first = export_html(_story(101), str(tmp_path))
    variant = _story(101)
    variant.url = "http://fanfiction.net/s/101/1/Same-Title"
    assert export_html(variant, str(tmp_path)) == first
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_an_unidentifiable_file_is_not_stranded(tmp_path):
    """Refusing to write would grow a chain of numbered siblings."""
    opaque = tmp_path / "Opaque - Author.html"
    opaque.write_text("<html><body>no source anywhere</body></html>")
    assert resolve_export_path(opaque, _story(303)) == opaque


def test_an_explicit_destination_is_written_exactly(tmp_path):
    target = tmp_path / "My renamed book.html"
    target.write_text("<html></html>")
    assert export_html(_story(101), str(tmp_path), output_path=target) == target
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_a_format_change_keeps_its_own_extension(tmp_path):
    """An HTML export must not land under a .epub name."""
    original = tmp_path / "book.epub"
    original.write_bytes(b"PK\x03\x04 not really an epub")
    written = export_html(_story(101), str(tmp_path), output_path=original)
    assert written.suffix == ".html"
    assert original.read_bytes().startswith(b"PK")


# ── CORE-02: chapter caches key on the chapter, not the ordinal ────

@pytest.fixture
def cached_scraper(tmp_path):
    scraper = BaseScraper(cache_dir=tmp_path / "cache", delay_range=(0, 0))
    scraper.site_name = "audit"
    fetched: list[str] = []

    def fetch(urls):
        fetched.extend(urls)
        return [f"<p>Body for {u}</p>" for u in urls]

    scraper._fetch_parallel = fetch

    def materialise(chapters):
        return scraper._materialise_chapters(
            story_id=1,
            chapter_list=[{"url": u, "title": u} for u in chapters],
            skip_chapters=0, chapter_spec=None,
            parse_chapter=lambda soup: str(soup.p),
            progress_callback=None,
        )

    return SimpleNamespace(materialise=materialise, fetched=fetched)


def test_an_inserted_chapter_is_fetched_not_substituted(cached_scraper):
    cached_scraper.materialise(["A", "B"])
    cached_scraper.fetched.clear()
    out = cached_scraper.materialise(["A", "INSERTED", "B"])
    assert [c.html for c in out] == [
        "<p>Body for A</p>", "<p>Body for INSERTED</p>", "<p>Body for B</p>",
    ]
    assert cached_scraper.fetched == ["INSERTED"]


def test_reordering_and_deletion_serve_the_right_bodies(cached_scraper):
    cached_scraper.materialise(["A", "B"])
    cached_scraper.fetched.clear()
    assert [c.html for c in cached_scraper.materialise(["B", "A"])] == [
        "<p>Body for B</p>", "<p>Body for A</p>",
    ]
    assert [c.html for c in cached_scraper.materialise(["A"])] == [
        "<p>Body for A</p>",
    ]
    assert cached_scraper.fetched == [], "warm chapters must not refetch"


def test_chapter_cache_key_is_stable_and_url_derived():
    key = chapter_cache_key("https://example.invalid/ch/1")
    assert key == chapter_cache_key("https://example.invalid/ch/1")
    assert key != chapter_cache_key("https://example.invalid/ch/2")
    assert chapter_cache_key("") is None


# ── CORE-03: a blocked chapter list is not a short book ────────────

def test_scribblehub_refuses_a_truncated_chapter_list():
    from ficary.scribblehub import (
        PartialTableOfContentsError, ScribbleHubScraper,
    )

    series = (
        '<html><body>'
        '<div class="fic_title">A Long Serial</div>'
        '<span class="auth_name_fic">Someone</span>'
        '<div class="wi_fic_desc">Summary.</div>'
        '<div class="wi_novel_title tags toc">Contents '
        '<span class="cnt_toc">40</span></div>'
        '<a class="toc_a" href="https://www.scribblehub.com/read/1-x/chapter/9/">Chapter 9</a>'
        '<a class="toc_a" href="https://www.scribblehub.com/read/1-x/chapter/8/">Chapter 8</a>'
        '</body></html>'
    )

    class _Blocked:
        def post(self, *a, **k):
            return SimpleNamespace(status_code=503, text="")

    sc = ScribbleHubScraper(use_cache=False, delay_range=(0, 0))
    with patch.object(sc, "_fetch", return_value=series), \
         patch.object(sc, "_session", lambda: _Blocked()), \
         patch.object(sc, "_delay", lambda *a, **k: None), \
         patch.object(sc, "_save_meta_cache", lambda *a, **k: None):
        with pytest.raises(PartialTableOfContentsError) as exc:
            sc.download(1234)
    assert "40" in str(exc.value)


# ── CORE-05 / CORE-06: SubscribeStar identity and escaping ─────────

def test_merged_serials_have_distinct_stable_identities():
    from ficary.subscribestar import SubscribeStarScraper as S

    one = "https://subscribestar.adult/creator/story/A%20Tale"
    two = "https://subscribestar.adult/creator/story/Another%20Tale"
    assert S.synthetic_story_id(one) == S.parse_story_id(one)
    assert S.synthetic_story_id(one) != S.synthetic_story_id(two)
    assert S.synthetic_story_id(one) != 0
    # Stable across calls, so an export can be updated later.
    assert S.synthetic_story_id(one) == S.synthetic_story_id(one)


def test_google_doc_prose_in_angle_brackets_survives():
    from bs4 import BeautifulSoup

    from ficary.subscribestar import SubscribeStarScraper

    doc = (
        '<html><body><p>The literal &lt;secret&gt; vanishes.</p></body></html>'
    )
    sc = SubscribeStarScraper(use_cache=False, delay_range=(0, 0))
    with patch.object(sc, "_fetch", return_value=doc):
        html = sc._fetch_gdoc_html(
            "https://docs.google.com/document/d/abc123/edit",
        )
    assert "secret" in BeautifulSoup(html, "lxml").get_text()


# ── CORE-07: registered sites round-trip through shared helpers ────

@pytest.mark.parametrize("url", [
    "https://www.scribblehub.com/series/1234/a-story/",
    "https://subscribestar.adult/posts/1234",
    "https://subscribestar.adult/creator/story/A%20Tale",
])
def test_newer_sites_are_recognised_by_the_shared_url_registry(url):
    from ficary.sites import extract_story_url
    from ficary.url_classifier import classify

    assert extract_story_url(url), f"{url} is not in the story registry"
    ref = classify(url)
    assert ref is not None and ref.kind == "story"


# ── CORE-09: every entry point dispatches the same way ─────────────

def test_no_arguments_opens_the_gui_and_arguments_run_the_cli():
    from ficary import entrypoint

    launched = []
    with patch("ficary.gui.main", lambda: launched.append("gui")):
        entrypoint.main([])
    assert launched == ["gui"]

    seen = []
    with patch("ficary.cli.main", lambda argv=None: seen.append(argv)):
        entrypoint.main(["--help"])
    assert seen == [["--help"]]


def test_the_console_script_points_at_the_shared_entry_point():
    import tomllib
    from pathlib import Path

    data = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    assert data["project"]["scripts"]["ficary"] == "ficary.entrypoint:main"
    assert data["project"]["requires-python"] == ">=3.10"
