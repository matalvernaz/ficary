"""Retirement of the ffn-dl.exe rename shim.

The release zip carried an ``ffn-dl.exe`` copy through the ffn-dl ->
ficary rename so pre-rename auto-updaters could cross over. The zip no
longer ships it, so two behaviours keep existing installs healthy:

* ``cleanup_old_exe`` deletes a leftover ``ffn-dl.exe`` on launch, but
  only when the running binary is ``ficary.exe`` (an install still
  running as ffn-dl.exe must not unlink itself).
* ``_spawn_extractor`` can pass ``--updated-exe`` so ZipExtractor waits
  on the live ffn-dl.exe (via ``--current-exe``) yet relaunches the
  freshly-written ficary.exe, migrating the crossed-over install.
"""
from __future__ import annotations

from ficary import self_update as su


class TestSpawnExtractorMigration:
    def _capture_params(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(su, "_is_writable", lambda p: True)
        monkeypatch.setattr(
            su, "_shell_execute",
            lambda verb, file, params, cwd: captured.update(params=params),
        )
        return captured

    def test_omits_updated_exe_by_default(self, monkeypatch, tmp_path):
        captured = self._capture_params(monkeypatch)
        su._spawn_extractor(
            tmp_path / "ZipExtractor.exe", tmp_path / "u.zip", tmp_path,
            tmp_path / "ficary.exe",
        )
        assert "--current-exe" in captured["params"]
        assert "--updated-exe" not in captured["params"]

    def test_adds_updated_exe_for_migration(self, monkeypatch, tmp_path):
        captured = self._capture_params(monkeypatch)
        su._spawn_extractor(
            tmp_path / "ZipExtractor.exe", tmp_path / "u.zip", tmp_path,
            tmp_path / "ffn-dl.exe", updated_exe="ficary.exe",
        )
        params = captured["params"]
        # Waits on the live ffn-dl.exe but relaunches ficary.exe.
        assert "--current-exe" in params and "ffn-dl.exe" in params
        assert "--updated-exe" in params and "ficary.exe" in params


class TestCleanupShim:
    def _isolate(self, monkeypatch, tmp_path, exe_name):
        monkeypatch.setattr(su, "is_frozen", lambda: True)
        monkeypatch.setattr(su.tempfile, "gettempdir", lambda: str(tmp_path))
        exe = tmp_path / exe_name
        exe.write_bytes(b"")
        monkeypatch.setattr(su.sys, "executable", str(exe))
        return exe

    def test_removes_shim_when_running_as_ficary(self, monkeypatch, tmp_path):
        exe = self._isolate(monkeypatch, tmp_path, "ficary.exe")
        shim = tmp_path / "ffn-dl.exe"
        shim.write_bytes(b"")
        su.cleanup_old_exe()
        assert not shim.exists()
        assert exe.exists()  # never touches the running binary

    def test_keeps_ffndl_when_running_as_ffndl(self, monkeypatch, tmp_path):
        exe = self._isolate(monkeypatch, tmp_path, "ffn-dl.exe")
        su.cleanup_old_exe()
        # A crossed-over install must not delete the binary it runs as.
        assert exe.exists()

    def test_noop_when_shim_absent(self, monkeypatch, tmp_path):
        exe = self._isolate(monkeypatch, tmp_path, "ficary.exe")
        su.cleanup_old_exe()  # no ffn-dl.exe sibling — must not raise
        assert exe.exists()
