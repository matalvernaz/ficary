"""Update journal: post-update verification and roll-forward repair.

download_and_replace writes a journal (target tag + SHA-256 manifest of
the flat zip) into the install dir before spawning ZipExtractor. The
next launch calls pending_update_status():

* manifest verifies clean  -> journal + workdir cleaned up, None
* running version < target -> "stale" (extractor never swapped files)
* files mismatch manifest  -> "torn"  (extraction was interrupted)

retry_pending_update() re-spawns the extractor from the retained flat
zip; when that zip is gone the GUI falls back to check_for_update
(allow_equal=True) so the same version can be re-downloaded.
"""
from __future__ import annotations

import json
import time
import zipfile

import pytest

from ficary import self_update as su


def _make_flat_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _install(dirpath, files):
    for name, data in files.items():
        target = dirpath / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


PAYLOAD = {
    "ficary.exe": b"exe-bytes-v2",
    "_internal/base_library.zip": b"stdlib-ish",
    "_internal/sub/data.bin": b"\x00\x01\x02" * 100,
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake frozen install + journal-bearing workdir in a temp tree."""
    install = tmp_path / "install"
    install.mkdir()
    workdir = tmp_path / "ficary-update-test"
    workdir.mkdir()
    flat_zip = workdir / "ficary-flat.zip"
    _make_flat_zip(flat_zip, PAYLOAD)
    _install(install, PAYLOAD)

    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su.sys, "executable", str(install / "ficary.exe"))
    monkeypatch.setattr(su, "__version__", "2.0.0")
    monkeypatch.setattr(su.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(su, "_TORN_REVERIFY_DELAY_S", 0)
    return install, workdir, flat_zip


def _write_journal(flat_zip, tag="v2.0.0", started=None):
    manifest = su._flat_zip_manifest(flat_zip)
    su._write_update_journal(tag, flat_zip, manifest)
    if started is not None:
        path = su._journal_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started"] = started
        path.write_text(json.dumps(data), encoding="utf-8")
    return manifest


class TestManifest:
    def test_covers_every_file_with_real_hashes(self, env):
        _install, _workdir, flat_zip = env
        manifest = su._flat_zip_manifest(flat_zip)
        assert set(manifest) == set(PAYLOAD)
        for name, data in PAYLOAD.items():
            assert manifest[name]["size"] == len(data)
            assert manifest[name]["sha256"] == su.hashlib.sha256(data).hexdigest()


class TestPendingUpdateStatus:
    def test_no_journal_is_none(self, env):
        assert su.pending_update_status() is None

    def test_clean_verify_deletes_journal_and_workdir(self, env):
        _install, workdir, flat_zip = env
        _write_journal(flat_zip)
        assert su.pending_update_status() is None
        assert not su._journal_path().exists()
        assert not workdir.exists()

    def test_torn_file_reported_and_journal_kept(self, env):
        install, _workdir, flat_zip = env
        _write_journal(flat_zip)
        (install / "_internal" / "sub" / "data.bin").write_bytes(b"short")
        status = su.pending_update_status()
        assert status["state"] == "torn"
        assert status["target_tag"] == "v2.0.0"
        assert status["flat_zip"] == flat_zip
        assert su._journal_path().exists()

    def test_missing_file_counts_as_torn(self, env):
        install, _workdir, flat_zip = env
        _write_journal(flat_zip)
        (install / "_internal" / "base_library.zip").unlink()
        assert su.pending_update_status()["state"] == "torn"

    def test_stale_when_running_old_version(self, env):
        _install, _workdir, flat_zip = env
        _write_journal(flat_zip, tag="v2.1.0", started=time.time() - 3600)
        status = su.pending_update_status()
        assert status["state"] == "stale"
        assert status["target_tag"] == "v2.1.0"

    def test_grace_window_suppresses_fresh_stale(self, env):
        _install, _workdir, flat_zip = env
        # Journal written seconds ago: ZipExtractor is plausibly still
        # waiting on the old process — stay quiet.
        _write_journal(flat_zip, tag="v2.1.0")
        assert su.pending_update_status() is None
        assert su._journal_path().exists()

    def test_newer_running_version_discards_journal(self, env):
        _install, _workdir, flat_zip = env
        _write_journal(flat_zip, tag="v1.9.0")
        assert su.pending_update_status() is None
        assert not su._journal_path().exists()

    def test_corrupt_journal_discarded(self, env):
        su._journal_path().write_text("{not json", encoding="utf-8")
        assert su.pending_update_status() is None
        assert not su._journal_path().exists()

    def test_missing_zip_still_reports_torn_without_flat_zip(self, env):
        install, workdir, flat_zip = env
        _write_journal(flat_zip)
        (install / "ficary.exe").write_bytes(b"different")
        flat_zip.unlink()
        status = su.pending_update_status()
        assert status["state"] == "torn"
        assert status["flat_zip"] is None


class TestRetryPendingUpdate:
    def test_missing_zip_raises(self, env):
        with pytest.raises(RuntimeError, match="fresh download"):
            su.retry_pending_update({"target_tag": "v2.0.0", "flat_zip": None})

    def test_corrupt_zip_raises(self, env):
        _install, workdir, flat_zip = env
        flat_zip.write_bytes(b"PK\x03\x04 garbage")
        with pytest.raises(RuntimeError, match="unreadable|corrupt"):
            su.retry_pending_update(
                {"target_tag": "v2.0.0", "flat_zip": flat_zip}
            )

    def test_spawns_extractor_and_refreshes_journal(self, env, monkeypatch):
        _install, _workdir, flat_zip = env
        _write_journal(flat_zip, started=time.time() - 3600)
        monkeypatch.setattr(su, "can_self_replace", lambda: True)
        calls = {}
        monkeypatch.setattr(
            su, "_stage_and_spawn_extractor",
            lambda zip_, inst, exe, updated=None: calls.update(
                zip=zip_, install=inst, exe=exe, updated=updated,
            ),
        )
        before = time.time()
        su.retry_pending_update({"target_tag": "v2.0.0", "flat_zip": flat_zip})
        assert calls["zip"] == flat_zip
        journal = json.loads(su._journal_path().read_text(encoding="utf-8"))
        # Timestamp refreshed so the post-repair relaunch lands inside
        # the grace window instead of instantly re-prompting.
        assert journal["started"] >= before


class TestSweepKeepsJournalWorkdir:
    def test_referenced_workdir_survives_other_stale_removed(
        self, env, monkeypatch,
    ):
        _install, workdir, flat_zip = env
        _write_journal(flat_zip)
        other = su.Path(su.tempfile.gettempdir()) / "ficary-update-old"
        other.mkdir()
        old = time.time() - 48 * 3600
        import os
        os.utime(workdir, (old, old))
        os.utime(other, (old, old))
        su.cleanup_old_exe()
        assert workdir.exists()
        assert flat_zip.exists()
        assert not other.exists()


class TestAllowEqual:
    def _fake_latest(self, monkeypatch, tag):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "tag_name": tag,
                    "html_url": "https://example.invalid/rel",
                    "assets": [{
                        "name": "ficary-portable.zip",
                        "browser_download_url": "https://example.invalid/z",
                        "size": 1,
                    }],
                }

        monkeypatch.setattr(
            su.curl_requests, "get", lambda *a, **k: _Resp(),
        )

    def test_equal_version_rejected_by_default(self, env, monkeypatch):
        self._fake_latest(monkeypatch, "v2.0.0")
        assert su.check_for_update() is None

    def test_equal_version_accepted_for_repair(self, env, monkeypatch):
        self._fake_latest(monkeypatch, "v2.0.0")
        info = su.check_for_update(allow_equal=True)
        assert info is not None and info["tag"] == "v2.0.0"

    def test_older_release_never_offered(self, env, monkeypatch):
        self._fake_latest(monkeypatch, "v1.5.0")
        assert su.check_for_update(allow_equal=True) is None
