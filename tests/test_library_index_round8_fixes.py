"""Tests for the round-8 audit fixes in library/index.py:

* V1 — ``_save_blocker`` data-loss safety gate.
* V4-light — optimistic mtime conflict detection in ``save()``.
* V12 — migrate-collision field preservation (last_probed,
  remote_chapter_count, chapter_hashes, duplicate_relpaths).
* O2 — ``_library`` split so read accessors don't leave phantom roots.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ficary.library.index import (
    IndexConflictError,
    LibraryIndex,
    SCHEMA_VERSION,
    _merge_secondary_into_primary,
    _migrate_non_canonical_keys,
)


def _write_index(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ── V1: _save_blocker ────────────────────────────────────────────


def test_save_blocker_set_when_snapshot_fails(tmp_path: Path, monkeypatch):
    """A schema-mismatch index whose backup attempt fails must mark
    the in-memory copy unsafe-to-save so the next save() doesn't
    silently obliterate the original."""
    idx_path = tmp_path / "lib.json"
    _write_index(idx_path, {"version": 999, "libraries": {}})

    def boom(_path):
        raise OSError("disk full")

    # Make the snapshot helper raise from inside _snapshot_unreadable_index.
    monkeypatch.setattr(
        "ficary.library.backup.backup",
        boom,
    )

    idx = LibraryIndex.load(idx_path)
    assert idx.save_blocker is not None
    assert "snapshot failed" in idx.save_blocker

    with pytest.raises(RuntimeError, match="unsafe state"):
        idx.save()

    # Original file unharmed — the blocker prevented overwrite.
    raw = json.loads(idx_path.read_text())
    assert raw == {"version": 999, "libraries": {}}


def test_discard_save_blocker_unblocks_save(tmp_path: Path, monkeypatch):
    """After the user acknowledges the data-loss risk, save() proceeds."""
    idx_path = tmp_path / "lib.json"
    _write_index(idx_path, {"version": 999, "libraries": {}})

    monkeypatch.setattr(
        "ficary.library.backup.backup", lambda _p: (_ for _ in ()).throw(OSError("nope")),
    )

    idx = LibraryIndex.load(idx_path)
    assert idx.save_blocker is not None
    idx.discard_save_blocker()
    # After discard, the mtime check is the only remaining gate;
    # writing the file should now succeed.
    idx.save()
    raw = json.loads(idx_path.read_text())
    assert raw == {"version": SCHEMA_VERSION, "libraries": {}}


def test_save_blocker_on_unparseable_json(tmp_path: Path):
    """JSON-corrupt index also triggers the blocker (not just schema-
    version mismatch), so a hand-edit gone wrong doesn't silently get
    overwritten by the next save."""
    idx_path = tmp_path / "lib.json"
    idx_path.write_text("{this is not valid json")
    idx = LibraryIndex.load(idx_path)
    assert idx.save_blocker is not None
    assert "unreadable JSON" in idx.save_blocker


# ── V4-light: mtime conflict ─────────────────────────────────────


def test_save_raises_on_concurrent_write(tmp_path: Path):
    """Two LibraryIndex instances loaded from the same file: if one
    saves, the other's save() raises IndexConflictError rather than
    silently overwriting the first writer's changes."""
    idx_path = tmp_path / "lib.json"
    _write_index(
        idx_path,
        {"version": SCHEMA_VERSION, "libraries": {}},
    )

    a = LibraryIndex.load(idx_path)
    b = LibraryIndex.load(idx_path)

    a.library_state(tmp_path / "lib_a")["stories"]["url-a"] = {"relpath": "a"}
    # Bump the mtime by sleeping past the FS's mtime granularity.
    time.sleep(0.02)
    a.save()

    b.library_state(tmp_path / "lib_b")["stories"]["url-b"] = {"relpath": "b"}
    with pytest.raises(IndexConflictError):
        b.save()


def test_save_round_trip_updates_mtime(tmp_path: Path):
    """A single LibraryIndex's repeated saves don't spuriously detect
    their own previous write as a conflict."""
    idx_path = tmp_path / "lib.json"
    a = LibraryIndex.load(idx_path)
    a.library_state(tmp_path / "lib")["stories"]["url-1"] = {"relpath": "x"}
    a.save()
    time.sleep(0.02)
    a.library_state(tmp_path / "lib")["stories"]["url-2"] = {"relpath": "y"}
    a.save()  # must not raise


# ── O2: _library split ───────────────────────────────────────────


def test_read_accessors_do_not_persist_phantom_root(tmp_path: Path):
    """Reading an unindexed root via stories_in / untrackable_in /
    lookup_by_url must not insert a phantom library entry."""
    idx_path = tmp_path / "lib.json"
    idx = LibraryIndex.load(idx_path)

    list(idx.stories_in(tmp_path / "never_indexed"))
    idx.untrackable_in(tmp_path / "also_never_indexed")
    idx.lookup_by_url(tmp_path / "yet_another", "https://example/")

    # No phantom entries should be in _data.
    assert idx.library_roots() == []

    # ``library_state`` is the explicit "mutate-on-touch" surface —
    # that one IS allowed to create an entry.
    idx.library_state(tmp_path / "real")
    assert str((tmp_path / "real").resolve()) in idx.library_roots()


# ── V12: migrate-collision field preservation ────────────────────


def test_merge_preserves_last_probed_from_newer():
    primary = {
        "relpath": "p.epub",
        "title": "T",
        "author": "A",
        "last_probed": "2024-01-01T00:00:00Z",
    }
    secondary = {
        "relpath": "s.epub",
        "last_probed": "2024-06-01T00:00:00Z",
        "remote_chapter_count": 42,
    }
    _merge_secondary_into_primary(primary, secondary)
    assert primary["last_probed"] == "2024-06-01T00:00:00Z"
    assert primary["remote_chapter_count"] == 42


def test_merge_preserves_chapter_hashes_when_primary_empty():
    primary = {"relpath": "p.epub"}
    secondary = {"relpath": "s.epub", "chapter_hashes": ["aa", "bb"]}
    _merge_secondary_into_primary(primary, secondary)
    assert primary["chapter_hashes"] == ["aa", "bb"]


def test_merge_warns_but_keeps_primary_on_conflicting_hashes(caplog):
    primary = {"relpath": "p.epub", "chapter_hashes": ["aa"]}
    secondary = {"relpath": "s.epub", "chapter_hashes": ["bb", "cc"]}
    with caplog.at_level("WARNING"):
        _merge_secondary_into_primary(primary, secondary)
    assert primary["chapter_hashes"] == ["aa"]
    assert any(
        "conflicting chapter_hashes" in rec.message for rec in caplog.records
    )


def test_merge_unions_duplicate_relpaths():
    primary = {
        "relpath": "p.epub",
        "duplicate_relpaths": ["dup1.epub"],
    }
    secondary = {
        "relpath": "s.epub",
        "duplicate_relpaths": ["dup1.epub", "dup2.epub"],
    }
    _merge_secondary_into_primary(primary, secondary)
    dupes = primary["duplicate_relpaths"]
    # s.epub itself is added; duplicates are deduped; primary's own
    # relpath is not added even if it appeared in secondary's list.
    assert set(dupes) == {"s.epub", "dup1.epub", "dup2.epub"}
