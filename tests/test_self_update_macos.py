"""macOS auto-update: data root, bundle-data migration, and .app swap.

The frozen macOS build stores data in Application Support (never
inside the .app — updates replace the bundle wholesale) and updates by
downloading the release zip itself (no quarantine attribute), staging
the new bundle on the same volume, and doing two atomic renames.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ficary import portable
from ficary import self_update as su


@pytest.fixture(autouse=True)
def reset_cache():
    portable._cached_root = None
    portable._env_set = False
    yield
    portable._cached_root = None
    portable._env_set = False


def _make_bundle(root, name="ficary.app", exe="ficary"):
    macos = root / name / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (root / name / "Contents" / "Info.plist").write_text("<plist/>")
    (macos / exe).write_bytes(b"old-binary")
    return root / name, macos / exe


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """Fake frozen macOS install: a valid .app bundle in a temp dir."""
    bundle, exe = _make_bundle(tmp_path)
    monkeypatch.setattr(su.sys, "platform", "darwin")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su.sys, "executable", str(exe))
    monkeypatch.setattr(su.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return bundle, exe


class TestMacosDataRoot:
    def test_frozen_darwin_uses_application_support(self, monkeypatch, tmp_path):
        monkeypatch.setattr(portable, "is_frozen", lambda: True)
        monkeypatch.setattr(portable.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        root = portable.portable_root()
        assert root == tmp_path / "Library" / "Application Support" / "ficary"
        assert root.exists()

    def test_bundle_data_migrates_once(self, monkeypatch, tmp_path):
        bundle, exe = _make_bundle(tmp_path)
        old_root = exe.parent
        (old_root / "settings.ini").write_text("[ui]\n")
        (old_root / "cache").mkdir()
        (old_root / "cache" / "story.html").write_text("cached")
        new_root = tmp_path / "appsupport"
        new_root.mkdir()

        monkeypatch.setattr(portable, "is_frozen", lambda: True)
        monkeypatch.setattr(portable.sys, "platform", "darwin")
        monkeypatch.setattr(portable.sys, "executable", str(exe))
        portable._migrate_macos_bundle_data(new_root)

        assert (new_root / "settings.ini").read_text() == "[ui]\n"
        assert (new_root / "cache" / "story.html").read_text() == "cached"
        assert not (old_root / "settings.ini").exists()
        # PyInstaller payload stays put.
        assert exe.exists()

    def test_migration_never_clobbers_existing_target(self, monkeypatch, tmp_path):
        bundle, exe = _make_bundle(tmp_path)
        (exe.parent / "settings.ini").write_text("stale-in-bundle")
        new_root = tmp_path / "appsupport"
        new_root.mkdir()
        (new_root / "settings.ini").write_text("current")

        monkeypatch.setattr(portable, "is_frozen", lambda: True)
        monkeypatch.setattr(portable.sys, "platform", "darwin")
        monkeypatch.setattr(portable.sys, "executable", str(exe))
        portable._migrate_macos_bundle_data(new_root)

        assert (new_root / "settings.ini").read_text() == "current"


class TestBundleDiscovery:
    def test_valid_bundle_found(self, mac):
        bundle, _exe = mac
        assert su._macos_bundle_path() == bundle

    def test_missing_info_plist_disqualifies(self, mac):
        bundle, _exe = mac
        (bundle / "Contents" / "Info.plist").unlink()
        assert su._macos_bundle_path() is None

    def test_translocated_bundle_refuses_self_replace(self, monkeypatch, tmp_path):
        transloc = tmp_path / "AppTranslocation" / "XYZ" / "d"
        bundle, exe = _make_bundle(transloc)
        monkeypatch.setattr(su.sys, "platform", "darwin")
        monkeypatch.setattr(su, "is_frozen", lambda: True)
        monkeypatch.setattr(su.sys, "executable", str(exe))
        assert su._macos_can_self_replace() is False

    def test_writable_bundle_can_self_replace(self, mac):
        assert su._macos_can_self_replace() is True
        assert su.can_self_replace() is True


class TestAssetSelection:
    def _fake_latest(self, monkeypatch, assets):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"tag_name": "v9.9.9", "html_url": "u", "assets": assets}

        monkeypatch.setattr(su.curl_requests, "get", lambda *a, **k: _Resp())

    def test_darwin_picks_macos_zip(self, mac, monkeypatch):
        self._fake_latest(monkeypatch, [
            {"name": "ficary-portable.zip", "browser_download_url": "w", "size": 1},
            {"name": "ficary-macos-arm64.zip", "browser_download_url": "m", "size": 2},
        ])
        info = su.check_for_update()
        assert info["download_url"] == "m"

    def test_darwin_without_mac_asset_returns_none(self, mac, monkeypatch):
        self._fake_latest(monkeypatch, [
            {"name": "ficary-portable.zip", "browser_download_url": "w", "size": 1},
        ])
        assert su.check_for_update() is None


class TestBundleSwap:
    def _run_swap(self, mac, monkeypatch, tmp_path, fail_second_rename=False):
        bundle, exe = mac

        new_payload = tmp_path / "payload"
        new_bundle, new_exe = _make_bundle(new_payload)
        new_exe.write_bytes(b"new-binary")
        zip_src = tmp_path / "release.zip"
        with zipfile.ZipFile(zip_src, "w") as zf:
            for p in new_payload.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(new_payload))

        def fake_download(url, dest, progress_cb=None, expected_size=0):
            dest.write_bytes(zip_src.read_bytes())

        def fake_ditto(argv, capture_output=True, text=True):
            with zipfile.ZipFile(Path(argv[3])) as zf:
                zf.extractall(argv[4])

            class _P:
                returncode = 0
                stderr = ""

            return _P()

        monkeypatch.setattr(su, "_download", fake_download)
        monkeypatch.setattr(su, "_verify_digest", lambda *a: None)
        monkeypatch.setattr(su.subprocess, "run", fake_ditto)

        if fail_second_rename:
            real_rename = su.os.rename
            calls = {"n": 0}

            def flaky_rename(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated rename failure")
                real_rename(src, dst)

            monkeypatch.setattr(su.os, "rename", flaky_rename)

        info = {"tag": "v9.9.9", "download_url": "m", "size": 0, "digest": None,
                "is_zip": True}
        return bundle, su._macos_download_and_swap, info

    def test_swap_replaces_bundle_and_keeps_old_aside(
        self, mac, monkeypatch, tmp_path,
    ):
        bundle, swap, info = self._run_swap(mac, monkeypatch, tmp_path)
        result = swap(info)
        assert result == bundle
        new_exe = bundle / "Contents" / "MacOS" / "ficary"
        assert new_exe.read_bytes() == b"new-binary"
        old = list(bundle.parent.glob("ficary.app.old-*"))
        assert len(old) == 1
        assert (old[0] / "Contents" / "MacOS" / "ficary").read_bytes() == b"old-binary"

    def test_failed_second_rename_restores_original(
        self, mac, monkeypatch, tmp_path,
    ):
        bundle, swap, info = self._run_swap(
            mac, monkeypatch, tmp_path, fail_second_rename=True,
        )
        with pytest.raises(OSError):
            swap(info)
        exe = bundle / "Contents" / "MacOS" / "ficary"
        assert exe.read_bytes() == b"old-binary"
        assert not list(bundle.parent.glob("ficary.app.old-*"))


class TestOldBundleSweep:
    def test_dead_pid_swept_live_pid_kept(self, mac, monkeypatch):
        bundle, _exe = mac
        import os
        import subprocess
        # A PID that is guaranteed dead: a real child, already reaped.
        child = subprocess.Popen(["true"])
        child.wait()
        dead = bundle.parent / f"ficary.app.old-{child.pid}"
        dead.mkdir()
        live = bundle.parent / f"ficary.app.old-{os.getpid()}"
        live.mkdir()
        staged = bundle.parent / ".ficary-update-staged-42.app"
        staged.mkdir()
        su.cleanup_old_exe()
        assert not dead.exists()
        assert live.exists()
        assert not staged.exists()
