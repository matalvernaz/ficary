"""Timestamped record of what a destructive doctor heal changed.

Rounds 8–9 of the audit both found doctor data-loss bugs; the systemic
fix is that every destructive heal (index drops, watchlist drops, cache
prune) writes a manifest naming the pre-heal snapshots and the cache
quarantine directory, and ``--doctor-restore-last`` rolls the most
recent one back in a single command. Manifests live under
``portable_root()/heal-manifests/`` with the same stamp+salt naming and
depth cap as :mod:`ficary.library.backup`.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import portable
from .atomic import atomic_write_bytes, atomic_write_text

logger = logging.getLogger(__name__)

_MANIFEST_DIRNAME = "heal-manifests"
_SNAPSHOT_DIRNAME = "snapshots"
_MAX_MANIFESTS = 10
# The fractional second is absent on manifests written before 2.20.0.
_NAME_RE = re.compile(
    r"^heal-(?P<ts>\d{8}-\d{6}(?:\.\d{6})?)-[0-9a-f]{8}\.json$"
)


def _stamp() -> str:
    """Local-time name stamp, to the microsecond.

    Whole seconds were not enough. Two heals inside one second produced
    names that differed only in their uuid salt, so ``list_manifests``
    ordered them at random: ``--doctor-restore-last`` could restore the
    earlier heal's snapshot, and ``_prune_old`` could delete the newer
    manifest and keep the older one. Local time, matching the stamps
    already on disk, so old and new names still sort together.
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S.%f")


def _newest_first(paths) -> list[Path]:
    """Sort stamped manifests newest first.

    The embedded stamp is the primary key so a filesystem with a coarse
    or backwards-jumping clock still gives a stable order; mtime only
    separates the second-resolution names written before 2.20.0, which
    the stamp alone cannot tell apart.
    """
    entries = []
    for path in paths:
        m = _NAME_RE.match(path.name)
        if m is None:
            continue
        try:
            written = path.stat().st_mtime_ns
        except OSError:
            written = 0
        entries.append((m.group("ts"), written, path))
    entries.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [path for _ts, _written, path in entries]


@dataclass
class HealManifest:
    created_at: str = ""
    label: str = ""
    index_snapshot: Optional[str] = None
    watchlist_snapshot: Optional[str] = None
    cache_quarantine_dir: Optional[str] = None
    dropped_index_entries: int = 0
    dropped_watches: int = 0
    pruned_cache_entries: int = 0
    restored_at: str = ""
    path: str = field(default="", compare=False)

    def has_anything_to_restore(self) -> bool:
        return bool(
            self.index_snapshot
            or self.watchlist_snapshot
            or self.cache_quarantine_dir
        )


def manifest_dir() -> Path:
    return Path(portable.portable_root()) / _MANIFEST_DIRNAME


def _persist(manifest: HealManifest) -> None:
    """Write the manifest's current fields to its own ``path`` atomically."""
    payload = {k: v for k, v in asdict(manifest).items() if k != "path"}
    atomic_write_text(Path(manifest.path), json.dumps(payload, indent=2) + "\n")


def write_manifest(manifest: HealManifest) -> Path:
    """Persist ``manifest`` (assigning a fresh stamped name on first write)
    and prune old ones past the depth cap. Returns the manifest path.

    Meant to be written BEFORE the destructive heal it records, so a crash
    mid-heal still leaves a recovery record for ``--doctor-restore-last``;
    call :func:`update_manifest` afterward to fill in the post-heal
    counters and the cache-quarantine location."""
    directory = manifest_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if not manifest.path:
        stamp = _stamp()
        salt = uuid.uuid4().hex[:8]
        manifest.path = str(directory / f"heal-{stamp}-{salt}.json")
    manifest.created_at = manifest.created_at or time.strftime(
        "%Y-%m-%dT%H:%M:%S")
    _persist(manifest)
    _prune_old(directory)
    return Path(manifest.path)


def update_manifest(manifest: HealManifest) -> None:
    """Re-persist an already-written manifest in place — e.g. to record
    what the heal actually changed after it ran. No-op if the manifest was
    never written; logs (doesn't raise) on a write failure so a bookkeeping
    hiccup can't crash a heal that already succeeded."""
    if not manifest.path:
        return
    try:
        _persist(manifest)
    except OSError:
        logger.warning("Couldn't update heal manifest %s", manifest.path)


def snapshot_dir() -> Path:
    return manifest_dir() / _SNAPSHOT_DIRNAME


def capture_snapshot(src, kind: str) -> Optional[Path]:
    """Copy ``src`` into manifest-owned storage and return the copy's path.

    A snapshot a manifest references must NOT live in the rolling
    library-index backup pool: that pool prunes to a fixed depth on every
    backup, so a routine ``--heal`` or ``--restore-index`` can garbage-
    collect the snapshot a still-current manifest points at, leaving
    ``--doctor-restore-last`` with nothing to restore. Keeping the copy
    under ``heal-manifests/snapshots/`` ties its lifecycle to the manifest
    (see :func:`_unlink_owned_snapshots`). Returns ``None`` when ``src`` is
    absent or the copy fails — the caller decides whether that aborts the
    heal (it should, when the heal would otherwise drop unrecoverable data).
    """
    src = Path(src)
    if not src.exists():
        return None
    try:
        directory = snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _stamp()
        salt = uuid.uuid4().hex[:8]
        dest = directory / f"{stamp}-{salt}-{kind}{src.suffix or '.json'}"
        atomic_write_bytes(dest, src.read_bytes())
        return dest
    except OSError:
        logger.warning("Couldn't capture %s heal snapshot of %s", kind, src)
        return None


def load_manifest(path: Path) -> Optional[HealManifest]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable heal manifest %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    manifest = HealManifest(path=str(path))
    for key, value in data.items():
        if hasattr(manifest, key) and key != "path":
            setattr(manifest, key, value)
    return manifest


def list_manifests() -> list[Path]:
    directory = manifest_dir()
    if not directory.is_dir():
        return []
    return _newest_first(
        p for p in directory.iterdir() if _NAME_RE.match(p.name)
    )


def latest_manifest() -> Optional[HealManifest]:
    for path in list_manifests():
        manifest = load_manifest(path)
        if manifest is not None:
            return manifest
    return None


def mark_restored(manifest: HealManifest) -> None:
    """Stamp ``restored_at`` into the on-disk manifest so a second
    ``--doctor-restore-last`` is visibly a re-run, not fresh."""
    manifest.restored_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    update_manifest(manifest)


def _unlink_owned_snapshots(manifest: HealManifest) -> None:
    """Remove snapshot files this manifest owns (those under
    :func:`snapshot_dir`). External paths — e.g. a legacy manifest still
    pointing at a rolling backup-pool file — are left untouched."""
    try:
        snap_root = snapshot_dir().resolve()
    except OSError:
        return
    for path_str in (manifest.index_snapshot, manifest.watchlist_snapshot):
        if not path_str:
            continue
        p = Path(path_str)
        try:
            if p.parent.resolve() == snap_root and p.exists():
                p.unlink()
        except OSError:
            continue


def _prune_old(directory: Path) -> None:
    entries = _newest_first(
        p for p in directory.iterdir() if _NAME_RE.match(p.name)
    )
    for old in entries[_MAX_MANIFESTS:]:
        # Delete the manifest's owned snapshots before the manifest itself
        # so pruning never orphans snapshot files under snapshots/.
        stale = load_manifest(old)
        if stale is not None:
            _unlink_owned_snapshots(stale)
        try:
            old.unlink()
        except OSError:
            pass
