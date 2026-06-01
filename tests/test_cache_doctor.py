"""Scraper-cache doctor — reporting and pruning."""

from __future__ import annotations

from pathlib import Path

from ffn_dl.cache_doctor import check_cache, prune
from ffn_dl.library.index import LibraryIndex


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(
        tmp_path / "library-index.json",
        {"version": 1, "libraries": {}},
    )


def _seed_cache_entry(cache_root: Path, site: str, story_id: str, bytes_ish: int = 1024) -> Path:
    """Create a fake cache directory with a populated meta.json and
    a chapter file whose byte count is ``bytes_ish``-ish."""
    d = cache_root / f"{site}_{story_id}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"title": "T"}', encoding="utf-8")
    (d / "ch_0001.html").write_bytes(b"x" * bytes_ish)
    return d


def _seed_library_entry(
    index: LibraryIndex, root: Path, url: str, adapter: str,
) -> None:
    index.library_state(root)["stories"][url] = {
        "relpath": "foo.epub",
        "title": "T", "author": "A",
        "fandoms": [], "adapter": adapter,
        "format": "epub", "confidence": "high",
        "chapter_count": 1, "last_checked": "2026-04-01T00:00:00Z",
    }


class TestReportBasics:
    def test_empty_cache_dir_reports_zero(self, tmp_path):
        report = check_cache(tmp_path)
        assert report.total_entries == 0
        assert report.total_bytes == 0
        assert "0" in report.summary()

    def test_missing_cache_dir_is_ok(self, tmp_path):
        # Non-existent root — the doctor reports empty rather than crashing.
        report = check_cache(tmp_path / "not-there")
        assert report.total_entries == 0

    def test_counts_cache_entries_per_site(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1", bytes_ish=2000)
        _seed_cache_entry(tmp_path, "ffn", "2", bytes_ish=1500)
        _seed_cache_entry(tmp_path, "ao3", "100", bytes_ish=500)
        report = check_cache(tmp_path)
        assert report.total_entries == 3
        assert report.by_site["ffn"] == 2
        assert report.by_site["ao3"] == 1

    def test_total_bytes_sums_recursively(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1", bytes_ish=1000)
        _seed_cache_entry(tmp_path, "ffn", "2", bytes_ish=2000)
        report = check_cache(tmp_path)
        # Each entry also has a small meta.json (~14 bytes) — allow slack.
        assert report.total_bytes >= 3000
        assert report.total_bytes < 3200

    def test_largest_list_caps_at_10(self, tmp_path):
        for i in range(15):
            _seed_cache_entry(tmp_path, "ffn", str(i), bytes_ish=1000 + i)
        report = check_cache(tmp_path)
        assert len(report.largest) == 10
        # Largest first.
        sizes = [s for _p, s in report.largest]
        assert sizes == sorted(sizes, reverse=True)

    def test_ignores_non_site_directories(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        (tmp_path / "covers").mkdir()  # non-site directory
        (tmp_path / "covers" / "junk.jpg").write_bytes(b"x" * 100)
        report = check_cache(tmp_path)
        assert report.total_entries == 1


class TestOrphanDetection:
    def test_no_index_means_no_orphan_flagging(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        report = check_cache(tmp_path)  # no index
        assert report.orphan_entries == []

    def test_cache_entry_not_in_index_is_orphan(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        _seed_cache_entry(tmp_path, "ffn", "2")
        lib_root = tmp_path / "lib"
        lib_root.mkdir()

        index = _fresh_index(tmp_path)
        _seed_library_entry(
            index, lib_root, "https://www.fanfiction.net/s/1", "ffn",
        )

        report = check_cache(tmp_path, index=index)
        # Story 1 is tracked; story 2 is not.
        orphan_names = {p.name for p in report.orphan_entries}
        assert orphan_names == {"ffn_2"}


class TestPrune:
    def test_prune_removes_orphans(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        _seed_cache_entry(tmp_path, "ffn", "2")
        lib_root = tmp_path / "lib"
        lib_root.mkdir()

        index = _fresh_index(tmp_path)
        _seed_library_entry(
            index, lib_root, "https://www.fanfiction.net/s/1", "ffn",
        )

        report = check_cache(tmp_path, index=index)
        result = prune(report)
        assert result.pruned == 1
        # ffn_2 is gone, ffn_1 stays.
        assert (tmp_path / "ffn_1").exists()
        assert not (tmp_path / "ffn_2").exists()

    def test_prune_with_empty_orphan_list_does_nothing(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        report = check_cache(tmp_path)  # no index → no orphans
        result = prune(report)
        assert result.pruned == 0
        assert (tmp_path / "ffn_1").exists()

    def test_prune_counts_freed_bytes(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1", bytes_ish=5000)  # orphan
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        # A tracked story keeps the index non-empty so ffn_1 is a
        # genuine orphan. An empty index deliberately flags nothing —
        # see test_empty_index_flags_no_orphans.
        _seed_cache_entry(tmp_path, "ffn", "2")
        _seed_library_entry(
            index, lib_root, "https://www.fanfiction.net/s/2", "ffn",
        )
        report = check_cache(tmp_path, index=index)
        assert len(report.orphan_entries) == 1
        result = prune(report)
        assert result.bytes_freed >= 5000

    def test_empty_index_flags_no_orphans(self, tmp_path):
        # Regression: an index with zero tracked stories (moved,
        # quarantined, or fresh) must NOT flag every cache entry as an
        # orphan. That path let `--doctor --heal` wipe the entire cache,
        # forcing a full re-scrape at FFN's 2s/chapter rate-limit floor.
        _seed_cache_entry(tmp_path, "ffn", "1")
        _seed_cache_entry(tmp_path, "ao3", "2")
        index = _fresh_index(tmp_path)  # zero tracked stories
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []


class TestSummary:
    def test_summary_mentions_cache_root(self, tmp_path):
        report = check_cache(tmp_path)
        assert str(tmp_path) in report.summary()

    def test_summary_breaks_down_by_site_when_populated(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "1")
        _seed_cache_entry(tmp_path, "ao3", "1")
        summary = check_cache(tmp_path).summary()
        assert "ffn" in summary
        assert "ao3" in summary

    def test_summary_omits_largest_when_empty(self, tmp_path):
        summary = check_cache(tmp_path).summary()
        assert "Largest" not in summary

    def test_summary_surfaces_orphan_count(self, tmp_path):
        _seed_cache_entry(tmp_path, "ffn", "orphan")
        _seed_cache_entry(tmp_path, "ffn", "1")  # tracked, keeps index non-empty
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(
            index, lib_root, "https://www.fanfiction.net/s/1", "ffn",
        )
        report = check_cache(tmp_path, index=index)
        summary = report.summary()
        assert "Orphan" in summary


class TestNonIntegerCacheKeys:
    """Regression coverage for the 5 sites where ``parse_story_id`` returns
    a tuple/slug — :meth:`cache_key_for_url` is what the cache directory
    is actually named with, and the orphan match has to use it. Without
    these tests the cache_doctor silently mis-orphans every entry on
    these sites and ``--prune`` deletes them all.
    """

    def test_chyoa_cache_entry_indexed_is_not_orphan(self, tmp_path):
        from ffn_dl.erotica import ChyoaScraper

        url = "https://chyoa.com/chapter/Foo.42"
        sid = ChyoaScraper.cache_key_for_url(url)
        _seed_cache_entry(tmp_path, "chyoa", str(sid))
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(index, lib_root, url, "chyoa")
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []

    def test_chyoa_node_entries_never_flagged_as_orphan(self, tmp_path):
        # Per-node caches (chyoa_node_<id>) outlive any one download
        # and aren't tied to a library URL — they must never be
        # treated as orphans regardless of index contents.
        node_dir = tmp_path / "chyoa_node_abc123"
        node_dir.mkdir()
        (node_dir / "node.json").write_text("{}", encoding="utf-8")
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)  # empty index — would orphan everything
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []
        assert report.total_entries == 0  # node dirs aren't story entries

    def test_literotica_cache_entry_indexed_is_not_orphan(self, tmp_path):
        from ffn_dl.erotica import LiteroticaScraper

        url = "https://www.literotica.com/s/some-fic"
        sid = LiteroticaScraper.cache_key_for_url(url)
        _seed_cache_entry(tmp_path, "literotica", str(sid))
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(index, lib_root, url, "literotica")
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []

    def test_lushstories_cache_entry_indexed_is_not_orphan(self, tmp_path):
        from ffn_dl.erotica import LushStoriesScraper

        url = "https://www.lushstories.com/stories/feet/foot-worship"
        sid = LushStoriesScraper.cache_key_for_url(url)
        _seed_cache_entry(tmp_path, "lushstories", str(sid))
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(index, lib_root, url, "lushstories")
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []

    def test_mcstories_cache_entry_indexed_is_not_orphan(self, tmp_path):
        from ffn_dl.erotica import MCStoriesScraper

        url = "https://mcstories.com/SomeStory/"
        sid = MCStoriesScraper.cache_key_for_url(url)
        _seed_cache_entry(tmp_path, "mcstories", str(sid))
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(index, lib_root, url, "mcstories")
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []

    def test_nifty_cache_entry_indexed_is_not_orphan(self, tmp_path):
        from ffn_dl.erotica import NiftyScraper

        url = "https://www.nifty.org/nifty/gay/college/the-brotherhood/"
        sid = NiftyScraper.cache_key_for_url(url)
        _seed_cache_entry(tmp_path, "nifty", str(sid))
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        _seed_library_entry(index, lib_root, url, "nifty")
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []

    def test_default_cache_key_falls_through_to_parse_story_id(self):
        # Sites whose parse_story_id already returns the cache-friendly id
        # (FFN, AO3, RoyalRoad, FicWad, MediaMiner, Wattpad, AFF, SOL,
        # Sexstories, Fictionmania, TGStorytime, DarkWanderer, GreatFeet)
        # inherit the default and must not regress.
        from ffn_dl.scraper import FFNScraper

        assert FFNScraper.cache_key_for_url(
            "https://www.fanfiction.net/s/12345",
        ) == 12345
        assert FFNScraper.cache_key_for_url(12345) == 12345

    def test_non_story_top_level_dirs_are_skipped(self, tmp_path):
        # ``llm_an``, ``cf-cookies``, ``covers``, ``huggingface`` live
        # next to the per-story caches but aren't story-keyed; they
        # must never appear in the orphan list, regardless of index.
        for name in ("llm_an", "covers", "huggingface", "cf-cookies"):
            d = tmp_path / name
            d.mkdir()
            (d / "junk.json").write_bytes(b"{}")
        lib_root = tmp_path / "lib"
        lib_root.mkdir()
        index = _fresh_index(tmp_path)
        report = check_cache(tmp_path, index=index)
        assert report.orphan_entries == []
        assert report.total_entries == 0
