"""Regressions for the 2026-07-11 roundtable audit fixes (non-GUI).

Each test pins one audit finding so the fix can't silently regress.
GUI-side fixes (#1 GUI path, #3 Add Story focus) live in
test_gui_smoke.py.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from ficary import cli, scraper
from ficary.scraper import BaseScraper, RateLimitError


def _args(**over):
    """A complete argparse-shaped namespace for _build_scraper."""
    base = dict(
        max_retries=5, no_cache=False, delay_min=None, delay_max=None,
        chunk_size=None, use_wayback=False, cf_solve=False, fichub=False,
        refetch_all=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── #1 fresh-copy re-pull must not use the chapter cache ──────────────

def test_build_scraper_refetch_all_disables_cache():
    url = "https://www.fanfiction.net/s/12345/1/"
    assert cli._build_scraper(url, _args(refetch_all=False)).use_cache is True
    assert cli._build_scraper(url, _args(refetch_all=True)).use_cache is False


# ── #2 an unwritable cache dir degrades to cache-off, never aborts ────

def test_scraper_survives_uncreatable_cache_dir(monkeypatch):
    monkeypatch.setattr(scraper, "_default_cache_dir", lambda: None)
    s = BaseScraper(use_cache=True)  # must not raise
    assert s.use_cache is False
    assert s.cache_dir is None


def test_default_cache_dir_returns_none_on_mkdir_failure(monkeypatch):
    import pathlib

    def boom(self, *a, **k):
        raise PermissionError("read-only home")

    monkeypatch.setattr(pathlib.Path, "mkdir", boom)
    # Not frozen in tests, so it takes the ~/.cache branch and swallows.
    assert scraper._default_cache_dir() is None


# ── #7 cached CF cookies are seeded at most once per fetch ────────────

def test_cf_cookie_seed_fires_at_most_once(monkeypatch):
    monkeypatch.setattr("ficary.scraper.time.sleep", lambda s: None)
    s = BaseScraper(use_cache=False, max_retries=5)
    monkeypatch.setattr(s, "_rotate_browser", lambda: s._session())

    seed_calls = {"n": 0}

    def fake_seed(sess, url):
        seed_calls["n"] += 1
        return True  # pretend a cached cookie exists every time

    monkeypatch.setattr(s, "_maybe_seed_cf_cookies", fake_seed)

    class Always403:
        def get(self, url, timeout=None):
            return type("R", (), {"status_code": 403, "text": "", "headers": {}})()

    with pytest.raises(RateLimitError):
        s._fetch("https://example.invalid/x", session=Always403())
    # Pre-fix this was one seed per attempt (5). The known-bad cookie is
    # now replayed only once; the rest of the budget rotates/solves.
    assert seed_calls["n"] == 1, seed_calls


# ── #8 index conflict check catches a same-mtime, different-size write ─

def test_index_conflict_on_same_mtime_size_change(tmp_path):
    from ficary.library.index import IndexConflictError, LibraryIndex, _empty

    path = tmp_path / "library-index.json"
    seed = LibraryIndex(path, _empty())
    seed.save()

    idx = LibraryIndex.load(path)          # captures (mtime, size)
    loaded_mtime = path.stat().st_mtime

    # Another writer appends bytes, then restores the original mtime so a
    # mtime-only check would miss it — the size delta must still trip.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(" " * 4096)
    os.utime(path, (loaded_mtime, loaded_mtime))
    assert path.stat().st_mtime == loaded_mtime

    with pytest.raises(IndexConflictError):
        idx.save()


# ── #9 structurally-malformed-but-valid JSON must not crash load() ────

def test_load_tolerates_libraries_as_list(tmp_path):
    from ficary.library.index import LibraryIndex, SCHEMA_VERSION

    path = tmp_path / "library-index.json"
    path.write_text(json.dumps({"version": SCHEMA_VERSION, "libraries": []}))
    idx = LibraryIndex.load(path)  # must not raise AttributeError
    assert list(idx.library_roots()) == []


# ── #6 explicit Rescan prunes files deleted off disk ──────────────────

def test_scan_clear_existing_prunes_orphans(tmp_path):
    pytest.importorskip("ebooklib")
    from ficary.exporters import export_epub
    from ficary.library.index import LibraryIndex
    from ficary.library.scanner import scan
    from ficary.models import Chapter, Story

    lib = tmp_path / "lib"
    lib.mkdir()
    index_path = tmp_path / "idx.json"
    story = Story(
        id=1, title="Orphan", author="A", summary="",
        url="https://www.fanfiction.net/s/999/1/",
        chapters=[Chapter(number=1, title="C1", html="<p>x</p>")],
    )
    epub = export_epub(story, str(lib))
    scan(lib, index_path=index_path, clear_existing=True)
    assert len(list(LibraryIndex.load(index_path).stories_in(lib))) == 1

    os.remove(epub)
    scan(lib, index_path=index_path, clear_existing=True)
    assert list(LibraryIndex.load(index_path).stories_in(lib)) == []
