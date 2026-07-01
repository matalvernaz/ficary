"""Tests for the ffn-dl → Ficary rename compatibility shims."""
import json

from ficary import legacy


class TestMigrateSidecar:
    def test_renames_legacy_when_new_absent(self, tmp_path):
        """A pre-rename .ffn-voices-* file is moved onto the .ficary-* name."""
        legacy_file = tmp_path / ".ffn-voices-42.json"
        legacy_file.write_text(json.dumps({"Harry": "en-GB"}), encoding="utf-8")
        new_path = tmp_path / ".ficary-voices-42.json"

        result = legacy.migrate_sidecar(new_path)

        assert result == new_path
        assert new_path.exists()
        assert not legacy_file.exists()
        assert json.loads(new_path.read_text())["Harry"] == "en-GB"

    def test_noop_when_new_exists(self, tmp_path):
        """An existing .ficary-* file wins; the legacy file is left alone."""
        legacy_file = tmp_path / ".ffn-voices-42.json"
        legacy_file.write_text("legacy", encoding="utf-8")
        new_path = tmp_path / ".ficary-voices-42.json"
        new_path.write_text("current", encoding="utf-8")

        result = legacy.migrate_sidecar(new_path)

        assert result == new_path
        assert new_path.read_text() == "current"
        assert legacy_file.exists()

    def test_returns_new_when_no_legacy(self, tmp_path):
        """No legacy sibling: return the new path unchanged for writing."""
        new_path = tmp_path / ".ficary-accents-7.json"
        assert legacy.migrate_sidecar(new_path) == new_path
        assert not new_path.exists()


class TestGetenvCompat:
    def test_prefers_new_name(self, monkeypatch):
        monkeypatch.setenv("FICARY_AO3_COOKIE", "new")
        monkeypatch.setenv("FFN_DL_AO3_COOKIE", "old")
        assert legacy.getenv_compat("FICARY_AO3_COOKIE") == "new"

    def test_falls_back_to_legacy_name(self, monkeypatch):
        monkeypatch.delenv("FICARY_AO3_COOKIE", raising=False)
        monkeypatch.setenv("FFN_DL_AO3_COOKIE", "old")
        assert legacy.getenv_compat("FICARY_AO3_COOKIE") == "old"

    def test_default_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("FICARY_AO3_COOKIE", raising=False)
        monkeypatch.delenv("FFN_DL_AO3_COOKIE", raising=False)
        assert legacy.getenv_compat("FICARY_AO3_COOKIE", "fallback") == "fallback"


class TestMigrateDir:
    def test_moves_when_target_absent(self, tmp_path):
        old = tmp_path / ".ffn-dl"
        old.mkdir()
        (old / "settings.ini").write_text("x=1", encoding="utf-8")
        new = tmp_path / ".ficary"

        legacy.migrate_dir(old, new)

        assert not old.exists()
        assert (new / "settings.ini").read_text() == "x=1"

    def test_skips_when_target_exists(self, tmp_path):
        """Never clobber an existing new dir — leave both untouched."""
        old = tmp_path / ".ffn-dl"
        old.mkdir()
        (old / "old.txt").write_text("old", encoding="utf-8")
        new = tmp_path / ".ficary"
        new.mkdir()
        (new / "new.txt").write_text("new", encoding="utf-8")

        legacy.migrate_dir(old, new)

        assert old.exists()
        assert (new / "new.txt").exists()
        assert not (new / "old.txt").exists()

    def test_noop_when_old_absent(self, tmp_path):
        old = tmp_path / ".ffn-dl"
        new = tmp_path / ".ficary"
        legacy.migrate_dir(old, new)
        assert not new.exists()
