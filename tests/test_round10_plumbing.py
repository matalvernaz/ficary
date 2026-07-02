"""Round-10 regression tests: webnovel/wattpad cache keying, locked-stub
detection, CLI FFN fandom mapping, fandom-browse filter validation, the
FFN auto-detect ValueError walk, and legacy dir-migration staging."""
from pathlib import Path

import pytest

from ficary import legacy, webnovel
from ficary.cli import _build_parser, _build_search_spec
from ficary.models import Chapter
from ficary.scraper import FFNScraper


class TestLockedStub:
    def test_stub_detected(self):
        assert webnovel.is_locked_stub(webnovel._LOCKED_NOTICE)

    def test_whitespace_variant_detected(self):
        squashed = " ".join(webnovel._LOCKED_NOTICE.split())
        assert webnovel.is_locked_stub("  " + squashed + "\n")

    def test_chapter_quoting_the_notice_is_not_a_stub(self):
        html = "<p>She read the sign aloud:</p>" + webnovel._LOCKED_NOTICE
        assert not webnovel.is_locked_stub(html)

    def test_real_chapter_is_not_a_stub(self):
        assert not webnovel.is_locked_stub("<p>Chapter body.</p>")


class TestIdKeyedChapterCache:
    """Stable-id cache keys: an ordinal-keyed entry must never satisfy an
    id-keyed lookup (the Wattpad/Webnovel mid-list-mutation bug)."""

    def _scraper(self, tmp_path):
        return FFNScraper(cache_dir=tmp_path, use_cache=True)

    def test_save_load_roundtrip_with_key(self, tmp_path):
        s = self._scraper(tmp_path)
        ch = Chapter(number=3, title="T", html="<p>x</p>")
        s._save_chapter_cache(1, ch, cache_key="part_999")
        got = s._load_chapter_cache(1, 3, cache_key="part_999")
        assert got is not None and got.html == "<p>x</p>"

    def test_legacy_ordinal_entry_misses_id_lookup(self, tmp_path):
        s = self._scraper(tmp_path)
        ch = Chapter(number=3, title="T", html="<p>old ordinal body</p>")
        s._save_chapter_cache(1, ch)  # ordinal-keyed (ch_0003.json)
        assert s._load_chapter_cache(1, 3, cache_key="part_999") is None

    def test_key_isolation(self, tmp_path):
        s = self._scraper(tmp_path)
        s._save_chapter_cache(1, Chapter(3, "A", "<p>a</p>"), cache_key="part_1")
        s._save_chapter_cache(1, Chapter(3, "B", "<p>b</p>"), cache_key="part_2")
        assert s._load_chapter_cache(1, 3, cache_key="part_1").html == "<p>a</p>"
        assert s._load_chapter_cache(1, 3, cache_key="part_2").html == "<p>b</p>"


class TestFfnCliFandomMapping:
    def test_fandom_reaches_ffn_filters(self):
        args = _build_parser().parse_args(
            ["--search", "x", "--site", "ffn", "--fandom", "Harry-Potter",
             "--ffn-characters", "Hermione G."]
        )
        _label, _fn, filters = _build_search_spec(args)
        assert filters.get("fandom") == "Harry-Potter"
        assert filters.get("characters") == "Hermione G."

    def test_ffn_category_flag_maps(self):
        args = _build_parser().parse_args(
            ["--search", "x", "--site", "ffn", "--fandom", "Naruto",
             "--ffn-category", "anime"]
        )
        _label, _fn, filters = _build_search_spec(args)
        assert filters.get("category") == "anime"


class TestFandomBrowseValidation:
    def test_bad_sort_raises(self):
        from ficary.search import _build_ffn_fandom_url
        with pytest.raises(ValueError, match="sort"):
            _build_ffn_fandom_url("book", "Harry-Potter", {"sort": "bogus"}, 1)

    def test_bad_min_words_raises(self):
        from ficary.search import _build_ffn_fandom_url
        with pytest.raises(ValueError, match="min_words"):
            _build_ffn_fandom_url("book", "Harry-Potter", {"min_words": "loads"}, 1)

    def test_bad_status_raises(self):
        from ficary.search import _build_ffn_fandom_url
        with pytest.raises(ValueError, match="status"):
            _build_ffn_fandom_url("book", "Harry-Potter", {"status": "done"}, 1)


class TestAutoDetectValueErrorWalk:
    def test_wrong_category_value_error_keeps_walking(self, monkeypatch):
        """A character name unknown to the FIRST category's fandom must not
        abort the walk before the right category is tried."""
        from ficary import search as search_mod

        monkeypatch.setattr(
            search_mod, "_resolve_fandom", lambda f, c: ("", "The-Fandom"))
        calls = []

        def fake_fandom_search(query, category, slug, filters, page):
            calls.append(category)
            if len(calls) == 1:
                raise ValueError("Unknown character 'X' for this fandom")
            return [{"title": "Hit", "url": "u"}]

        monkeypatch.setattr(search_mod, "_search_ffn_fandom", fake_fandom_search)
        results = search_mod.search_ffn("", fandom="The-Fandom")
        assert [r["title"] for r in results] == ["Hit"]
        assert len(calls) == 2

    def test_error_surfaces_when_no_category_works(self, monkeypatch):
        from ficary import search as search_mod

        monkeypatch.setattr(
            search_mod, "_resolve_fandom", lambda f, c: ("", "The-Fandom"))

        def always_bad(query, category, slug, filters, page):
            raise ValueError("Unknown character 'X' for this fandom")

        monkeypatch.setattr(search_mod, "_search_ffn_fandom", always_bad)
        with pytest.raises(ValueError, match="Unknown character"):
            search_mod.search_ffn("", fandom="The-Fandom")


class TestMigrateDirStaging:
    def test_normal_migration_moves(self, tmp_path):
        old = tmp_path / "old-dir"
        old.mkdir()
        (old / "data.txt").write_text("payload", encoding="utf-8")
        new = tmp_path / "new-dir"
        legacy.migrate_dir(old, new)
        assert not old.exists()
        assert (new / "data.txt").read_text(encoding="utf-8") == "payload"

    def test_stale_staging_leftover_is_replaced(self, tmp_path):
        old = tmp_path / "old-dir"
        old.mkdir()
        (old / "data.txt").write_text("payload", encoding="utf-8")
        new = tmp_path / "new-dir"
        staging = tmp_path / "new-dir.migrating"
        staging.mkdir()  # crashed earlier attempt
        (staging / "partial.txt").write_text("torn", encoding="utf-8")
        legacy.migrate_dir(old, new)
        assert (new / "data.txt").exists()
        assert not (new / "partial.txt").exists()
        assert not staging.exists()

    def test_existing_new_never_clobbered(self, tmp_path):
        old = tmp_path / "old-dir"
        old.mkdir()
        (old / "data.txt").write_text("old", encoding="utf-8")
        new = tmp_path / "new-dir"
        new.mkdir()
        (new / "keep.txt").write_text("keep", encoding="utf-8")
        legacy.migrate_dir(old, new)
        assert (new / "keep.txt").exists()
        assert old.exists()  # untouched
