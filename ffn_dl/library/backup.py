"""Library-index backup and restore.

The library index file is small but load-bearing: it's what the refresh
path, reorganiser, and doctor all consult to decide what's where. A
botched ``--heal`` or a bug in the reorganiser can wipe entries the
user wanted to keep, and recovering means re-scanning (which can miss
untrackable promotions the user did by hand) or restoring from a
generic filesystem backup (which most people don't have for a file
this small).

This module keeps a lightweight, per-file rolling backup of the index:

* ``backup(path)`` — copy ``path`` to
  ``<path>.backup-YYYYMMDD-HHMMSS.json`` and prune the oldest
  ``<path>.backup-*`` so only :data:`_MAX_BACKUPS` remain. Returns
  the new backup's path.
* ``list_backups(path)`` — return every existing backup for ``path``
  in newest-first order.
* ``restore(backup_path, index_path)`` — atomically overwrite
  ``index_path`` with ``backup_path``.

The CLI wires these up to an auto-backup-before-mutate policy for
destructive operations (``--heal``, ``--clear-library``,
``--reorganize --apply``) so users don't need to remember to run a
backup manually.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_BACKUPS = 10
"""How many rolling backups to keep per index file. Ten is enough to
recover from "I ran --heal yesterday, it was wrong, then ran it again
today, still wrong" without the backup dir turning into clutter."""

# Match both the legacy second-resolution stamp and the new
# stamp-plus-uuid-suffix. The uuid was added to defeat same-second
# collisions when a retry loop or a load-with-failure-then-load cycle
# fires two backups inside one second — the prior shape silently
# overwrote the first backup, taking the unreadable original with it.
_BACKUP_SUFFIX_RE = re.compile(
    r"\.backup-(?P<ts>\d{8}-\d{6})(?:-[0-9a-f]+)?\.json$",
    re.IGNORECASE,
)


def backup(index_path: Path) -> Path | None:
    """Copy the index file to a timestamped sibling and prune old
    backups. Returns the new backup's path, or ``None`` when the
    source file doesn't exist (e.g. first-run before any scan).

    Restore with :func:`restore`. Backups share the index's parent
    directory so a restore never crosses a filesystem boundary — the
    atomic-rename trick only works within one filesystem.

    Each backup carries an 8-hex-char UUID suffix in addition to the
    UTC timestamp so two backups taken inside the same second don't
    overwrite each other. The old same-second-overwrite shape lost
    the only useful copy of an unreadable original whenever the
    load-fail path retried.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return None
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    # 8 hex chars of uuid4 = 32 bits of collision-resistance, enough
    # for a workflow that produces at most a few backups per second.
    salt = uuid.uuid4().hex[:8]
    backup_path = index_path.with_name(
        f"{index_path.stem}.backup-{stamp}-{salt}.json"
    )
    # Use the same atomic write helper as ``save`` so a crash mid-backup
    # can't leave a truncated copy that a later restore would then
    # enshrine as the new "correct" index.
    from ..atomic import atomic_write_bytes
    atomic_write_bytes(backup_path, index_path.read_bytes())
    _prune(index_path)
    return backup_path


def snapshot_before(label: str, index_path: Path) -> Path | None:
    """Take a backup before a destructive operation, logging the label.

    Wired by destructive entry points (``--reorganize --apply``,
    ``--heal``, ``--clear-library``, future
    ``--prune-untrackable``) so the user can recover from a
    misdiagnosed run with ``--restore-index``. Returns the snapshot
    path or ``None`` when there's nothing to back up (first-run
    before any scan).

    The label is logged at INFO so the operator can map snapshots
    back to the user action that triggered them — diagnosing "which
    backup do I roll back to" otherwise means timestamp guesswork.
    """
    snapshot = backup(index_path)
    if snapshot is not None:
        logger.info(
            "library index snapshotted to %s before %s",
            snapshot, label,
        )
    return snapshot


def list_backups(index_path: Path) -> list[Path]:
    """Every existing backup for ``index_path``, newest first.

    Ordering is by the filename's embedded timestamp rather than file
    mtime, so a filesystem that rounds mtime to seconds (or whose
    clock jumped backwards) still returns a stable order."""
    index_path = Path(index_path)
    if not index_path.parent.exists():
        return []
    candidates: list[tuple[str, Path]] = []
    prefix = f"{index_path.stem}.backup-"
    for p in index_path.parent.iterdir():
        if not p.is_file():
            continue
        if not p.name.startswith(prefix):
            continue
        m = _BACKUP_SUFFIX_RE.search(p.name)
        if m is None:
            continue
        candidates.append((m.group("ts"), p))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return [p for _ts, p in candidates]


def restore(backup_path: Path, index_path: Path) -> Path | None:
    """Atomically replace ``index_path`` with ``backup_path``'s contents.

    Doesn't touch the backup — it's a snapshot the user can revert to
    multiple times if needed. Raises :class:`FileNotFoundError` if the
    backup isn't there.

    Before overwriting, snapshots the current ``index_path`` (when it
    exists) so a wrong restore can be undone by restoring the
    pre-restore snapshot. Returns the path of that safety snapshot, or
    ``None`` when ``index_path`` didn't exist (nothing to lose). Without
    this safety, a user who mis-picks an old backup wipes out the only
    current state and has no way back.
    """
    backup_path = Path(backup_path)
    index_path = Path(index_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")
    safety_snapshot: Path | None = None
    if index_path.exists():
        safety_snapshot = backup(index_path)
        if safety_snapshot is not None:
            logger.info(
                "pre-restore safety snapshot of %s saved to %s",
                index_path, safety_snapshot,
            )
    from ..atomic import atomic_write_bytes
    atomic_write_bytes(index_path, backup_path.read_bytes())
    return safety_snapshot


def _prune(index_path: Path) -> None:
    """Drop the oldest backups so only :data:`_MAX_BACKUPS` remain."""
    existing = list_backups(index_path)
    for old in existing[_MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            # Another process might have removed it already. Not fatal.
            continue
