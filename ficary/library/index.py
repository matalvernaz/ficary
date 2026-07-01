"""LibraryIndex — persistent JSON state for the library manager.

One JSON file lives with the ficary program install (next to
settings.ini in portable mode, under ~/.ficary/ in dev). The file
holds entries for every library root the user has scanned, so a
single ficary install can manage multiple library directories
without collision.

Schema is versioned. v1:
    {
      "version": 1,
      "libraries": {
        "<absolute library root>": {
          "last_scan": "<ISO-8601 UTC>",
          "stories": {
            "<source URL>": {
              "relpath": "<path relative to library root>",
              "title": "...",
              "author": "...",
              "fandoms": [...],
              "adapter": "ffn|ao3|...",
              "format": "epub|html|txt",
              "confidence": "high|medium|low",
              "chapter_count": N,
              "file_mtime": <float>,   # stat().st_mtime, for cache-invalidate
              "file_size": <int>,      # stat().st_size,  for cache-invalidate
              "last_checked": "<ISO-8601 UTC>",
              "last_probed": "<ISO-8601 UTC>",      # optional
              "remote_chapter_count": N,            # optional — latest probe's upstream count
              "chapter_hashes": ["<sha256>", ...]   # optional — per-chapter
                                                    # content hashes for silent-
                                                    # edit detection; populated
                                                    # by --populate-hashes or on
                                                    # successful download
            }
          },
          "untrackable": [
            {
              "relpath": "...",
              "format": "...",
              "reason": "..."
            }
          ]
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..atomic import atomic_write_text
from ..sites import canonical_url
from .candidate import StoryCandidate

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1


def default_index_path() -> Path:
    """Resolve the index location when prefs don't override it."""
    from .. import portable
    return portable.portable_root() / "library-index.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_root(root: Path) -> str:
    """Absolute, resolved, string form — used as the dict key."""
    return str(Path(root).expanduser().resolve())


class IndexConflictError(RuntimeError):
    """Raised by :meth:`LibraryIndex.save` when the on-disk file has
    changed since this in-memory copy was loaded — another process or
    thread wrote between our load and save, and continuing would
    silently obliterate their changes. Callers should reload and merge.

    Optimistic concurrency check (mtime-based) rather than a file lock:
    no cross-platform stdlib lock primitive works on every filesystem
    Matt's libraries live on (NTFS, ext4, NFS shares); a mismatch is
    rare enough that the simple check covers the realistic threat —
    a CLI ``--update-library`` running alongside the GUI's Check for
    Updates."""


_EMPTY_LIBRARY = {"last_scan": None, "stories": {}, "untrackable": []}


def _stat_mtime(path: Path) -> float | None:
    """Return the file's modification time, or ``None`` on any stat
    failure (file missing, permissions, network blip). Used by the
    save() conflict check; missing mtime is treated as "first write"
    which is safe."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class LibraryIndex:
    """In-memory view of the on-disk library index, with save()."""

    def __init__(self, path: Path, data: dict):
        self._path = Path(path)
        self._data = data
        # ``_save_blocker`` carries a reason string when ``load()`` could
        # not snapshot an unreadable original index. ``save()`` refuses
        # to overwrite the disk file in that state — the in-memory copy
        # is empty, so a save would atomically destroy the original.
        # Cleared by :meth:`discard_save_blocker` (wired to the CLI's
        # ``--discard-bad-index`` and a future GUI recovery dialog).
        self._save_blocker: str | None = None
        # mtime captured at load time. ``save()`` re-stats before writing;
        # a mismatch means another writer touched the file and a blind
        # overwrite would lose those changes. None means "never loaded
        # from disk" (first write, no prior state to conflict with).
        self._loaded_mtime: float | None = None

    # ── Construction ────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | None = None) -> "LibraryIndex":
        """Load the index from disk, or return an empty one if missing
        or malformed. Never raises on a bad file — stale data just gets
        replaced by an empty index on the next save().

        Schema-version mismatch (e.g. a user downgrading ficary onto an
        index written by a newer build) is treated as "structurally
        unreadable" and returns empty — but only after first snapshotting
        the original file via :func:`library.backup.backup`. Without that
        snapshot, the next :meth:`save` would atomically replace the
        original with ``{}`` and silently wipe the user's library, with
        no signal that anything went wrong.

        When the snapshot itself fails (disk full, permissions), the
        returned index carries a ``_save_blocker`` reason — subsequent
        :meth:`save` calls raise ``RuntimeError`` until the user
        acknowledges the data-loss risk via
        :meth:`discard_save_blocker`. The app still loads (so the user
        can navigate / diagnose), it just can't overwrite the original
        index until the situation is resolved."""
        p = Path(path) if path else default_index_path()
        if not p.exists():
            return cls(p, _empty())
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            inst = cls(p, _empty())
            # File exists but is unparseable. Set a blocker to prevent
            # blind overwrite — a JSON-corrupt index might still be
            # recoverable by hand. Mtime stays captured so a concurrent
            # writer that fixed the file in the meantime still triggers
            # the conflict path on save.
            inst._loaded_mtime = _stat_mtime(p)
            inst._save_blocker = (
                f"unreadable JSON at {p} (parse error or I/O); "
                "saves blocked until acknowledged via discard_save_blocker()"
            )
            return inst
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            snapshot_path, blocker = _snapshot_unreadable_index(p, raw)
            inst = cls(p, _empty())
            inst._loaded_mtime = _stat_mtime(p)
            if blocker is not None:
                inst._save_blocker = blocker
            return inst
        raw.setdefault("libraries", {})
        _migrate_non_canonical_keys(raw)
        inst = cls(p, raw)
        inst._loaded_mtime = _stat_mtime(p)
        return inst

    def save(self) -> None:
        """Atomic write with fsync, gated by two safety checks:

        1. ``_save_blocker`` — refuse to save when ``load()`` detected an
           unreadable original it couldn't snapshot. Writing would
           atomically destroy the original.
        2. Mtime check — refuse to save when another process has
           written the file since we loaded it. Caller must reload and
           merge before retrying.
        """
        if self._save_blocker is not None:
            raise RuntimeError(
                f"library index in unsafe state, refusing to save: "
                f"{self._save_blocker}"
            )
        if self._loaded_mtime is not None:
            current = _stat_mtime(self._path)
            if current is not None and current != self._loaded_mtime:
                raise IndexConflictError(
                    f"library index at {self._path} was modified by "
                    "another process since load(); reload before saving "
                    "to avoid silently overwriting concurrent changes"
                )
        atomic_write_text(
            self._path,
            json.dumps(self._data, indent=2, sort_keys=True),
            fsync_dir=True,
        )
        # Re-stat after our own write so subsequent saves don't
        # spuriously detect our last write as another writer's.
        self._loaded_mtime = _stat_mtime(self._path)

    @property
    def save_blocker(self) -> str | None:
        """Reason save is blocked, or ``None`` when save is safe.

        Callers should display this to the user before offering to
        proceed (the GUI Library panel renders it as a status banner;
        the CLI prints it before ``--discard-bad-index`` is honoured)."""
        return self._save_blocker

    def discard_save_blocker(self) -> None:
        """Clear the save-blocker flag.

        Call this only when the user has explicitly accepted that any
        unparseable original index file may be overwritten — wired to
        ``ficary --discard-bad-index`` and to a future GUI recovery
        dialog. Existing rolling backups (see ``library/backup.py``)
        remain in place if the snapshot itself succeeded; only the
        unsnapshotted case loses ground truth on next save, which is
        what the gate exists to make the user aware of."""
        self._save_blocker = None

    # ── Library-scoped accessors ────────────────────────────────

    def _library(self, root: Path, *, create: bool = True) -> dict:
        """Return the in-memory dict for ``root``.

        ``create=True`` (the default for mutating callers like
        ``record`` and ``library_state``) inserts an empty library
        entry if missing — keeps the historic mutate-on-touch behaviour
        for callers that genuinely want to start tracking a new root.

        ``create=False`` is for read-only paths (``stories_in``,
        ``untrackable_in``, ``lookup_by_url``). Without this split,
        every read of an unindexed root silently persisted a phantom
        library entry on the next save — observed as drift between
        what ``library_roots()`` returned and what the user had
        actually scanned."""
        key = _normalize_root(root)
        if create:
            return self._data["libraries"].setdefault(
                key, {"last_scan": None, "stories": {}, "untrackable": []}
            )
        return self._data["libraries"].get(key) or dict(_EMPTY_LIBRARY)

    def library_state(self, root: Path) -> dict:
        """Mutable in-place dict for a library root: ``stories``,
        ``untrackable``, and ``last_scan``. Callers that need to
        promote/demote entries reach in through this; single-read
        consumers stick to the stories_in / untrackable_in helpers."""
        return self._library(root, create=True)

    def record(self, root: Path, candidate: StoryCandidate) -> bool:
        """Add or update an entry for this candidate under ``root``.

        Trackable candidates (HIGH/MEDIUM) are keyed by *canonical*
        source URL (see :func:`ficary.sites.canonical_url`) so the same
        story embedded at two paths with slightly different URL forms
        (``/s/N`` vs ``/s/N/1/``) collapses to a single entry.

        When a second file maps to an already-recorded URL, the new
        relpath is appended to ``duplicate_relpaths`` on the existing
        entry rather than overwriting the primary ``relpath``. This
        preserves the original without silently losing information
        about the other copy. The scanner uses the return value to
        count duplicates for its scan summary: True if a new entry was
        created, False if this was a duplicate of an existing one.
        LOW-confidence candidates always append to ``untrackable`` and
        return True.
        """
        from ..sites import canonical_url

        lib = self._library(root)
        rel = str(candidate.path.relative_to(Path(root).expanduser().resolve()))
        md = candidate.metadata

        if candidate.is_trackable and md.source_url:
            key = canonical_url(md.source_url) or md.source_url
            existing = lib["stories"].get(key)
            if existing is not None and existing.get("relpath") != rel:
                # Duplicate copy of a story we've already indexed. Keep
                # the primary entry's relpath stable (so reorganise /
                # update-library keep pointing at the same file) and
                # record the second path as a sibling. Deduplicate in
                # case the same path turns up twice in a re-scan.
                dupes = existing.setdefault("duplicate_relpaths", [])
                if rel not in dupes and rel != existing.get("relpath"):
                    dupes.append(rel)
                return False

            # Preserve fields the scanner doesn't rewrite (last_probed,
            # duplicate_relpaths, remote_chapter_count) so a re-scan
            # never forgets that the update path already hit the remote
            # for this URL. Without this merge, rescan_library() after
            # --update-library would wipe last_probed and defeat the TTL
            # skip on the next run — and wipe remote_chapter_count,
            # losing the pending-update marker that lets an interrupted
            # batch resume without re-probing every story.
            existing_preserved = {}
            if existing is not None:
                # ``chapter_hashes`` is preserved across rescans for the
                # same reason as ``last_probed`` / ``remote_chapter_count``:
                # a plain ``--scan-library`` re-walk shouldn't wipe the
                # ground-truth hash list that the bootstrap or a prior
                # download populated. Silent-edit detection reads this
                # field on every run; losing it would silently force a
                # full refetch cycle just to repopulate.
                for k in (
                    "last_probed",
                    "remote_chapter_count",
                    "duplicate_relpaths",
                    "chapter_hashes",
                ):
                    if k in existing:
                        existing_preserved[k] = existing[k]

            entry_record = {
                "relpath": rel,
                "title": md.title,
                "author": md.author,
                "fandoms": list(md.fandoms),
                "rating": md.rating,
                "status": md.status,
                "adapter": candidate.adapter_name,
                "format": md.format,
                "confidence": candidate.confidence.value,
                "chapter_count": md.chapter_count,
                "last_checked": _now_iso(),
            }
            # Stamp mtime/size so build_refresh_queue can skip the
            # ebooklib re-parse on unchanged files. Stat can race
            # (file removed between walk and record) — fall back to
            # leaving the fields absent, which forces a fresh read on
            # the next probe. Better a slow probe than a wrong cache.
            try:
                st = candidate.path.stat()
                entry_record["file_mtime"] = st.st_mtime
                entry_record["file_size"] = st.st_size
            except OSError:
                pass
            entry_record.update(existing_preserved)
            lib["stories"][key] = entry_record
            return True

        # Re-running a scan over the same library shouldn't pile a
        # duplicate untrackable entry on every pass for the same
        # unparseable file. Replace any prior entry with the same
        # relpath rather than appending so the list reflects the
        # current state of the library, not the cumulative scan
        # history.
        new_entry = {
            "relpath": rel,
            "format": md.format,
            "title": md.title,
            "author": md.author,
            "reason": (
                "; ".join(candidate.notes)
                if candidate.notes
                else "no identification"
            ),
        }
        for i, existing in enumerate(lib["untrackable"]):
            if existing.get("relpath") == rel:
                lib["untrackable"][i] = new_entry
                return True
        lib["untrackable"].append(new_entry)
        return True

    def mark_scan_complete(self, root: Path) -> None:
        self._library(root)["last_scan"] = _now_iso()

    def mark_probed(
        self,
        root: Path,
        probed: "list[str] | dict[str, int | None]",
        *,
        timestamp: str | None = None,
    ) -> int:
        """Stamp ``last_probed`` (and optionally ``remote_chapter_count``).

        ``probed`` is either:

        * A ``list[str]`` of URLs — stamps ``last_probed`` only (the
          original behaviour, preserved for the belt-and-braces pass
          where the caller already flushed counts and is just making
          sure every queued URL gets timestamped).
        * A ``dict[str, int | None]`` mapping URL → remote chapter
          count — stamps both ``last_probed`` and ``remote_chapter_count``
          per entry. ``None`` means "probe answered but had no count"
          (e.g. StoryNotFoundError); stamps ``last_probed`` and clears
          any stale count so the TTL absorbs the dead story.

        The count-aware shape is what enables resume-without-reprobe:
        if the process dies mid-batch, a later run sees
        ``remote_chapter_count > local`` on the unfinished entries and
        can queue them for download directly.

        Returns how many entries were actually updated — URLs absent
        from the index (e.g. a story that got removed between probe
        and stamp) are silently skipped. Saves once at the end so a
        library-update pass does a single disk write rather than N.
        """
        stamp = timestamp or _now_iso()
        stories = self._library(root)["stories"]
        touched = 0
        missed: list[str] = []

        if isinstance(probed, dict):
            items: "list[tuple[str, int | None]]" = list(probed.items())
        else:
            items = [(url, None) for url in probed]

        with_count_updates = isinstance(probed, dict)

        for url, remote_count in items:
            entry = stories.get(url)
            if entry is None:
                missed.append(url)
                continue
            entry["last_probed"] = stamp
            if with_count_updates:
                if remote_count is None:
                    # Probe answered with "no count available" —
                    # StoryNotFoundError, deletion, etc. Clear any stale
                    # pending-count so the next refresh doesn't treat a
                    # ghost as needing a download.
                    entry.pop("remote_chapter_count", None)
                else:
                    entry["remote_chapter_count"] = int(remote_count)
            touched += 1
        if touched:
            self.save()
        # Observability hook. If touched < len(urls), the caller sent
        # URLs that don't match any stored key — most often a path-
        # normalisation mismatch between the probe's root and the
        # stored library root, which silently drains stamps.
        logger.info(
            "mark_probed: stamped %d/%d under %r",
            touched, len(items), _normalize_root(root),
        )
        if missed:
            logger.warning(
                "mark_probed: %d URL(s) had no matching index entry: %r",
                len(missed), missed[:5],
            )
        return touched

    def clear_library(self, root: Path) -> None:
        """Drop all entries for a library root. Used when the user
        re-scans from scratch and wants the index to reflect the
        current disk state only (e.g., after deleting files)."""
        lib = self._library(root)
        lib["stories"] = {}
        lib["untrackable"] = []

    def lookup_by_url(self, root: Path, url: str) -> dict | None:
        # Stored keys are canonicalised (see ``record`` and
        # ``_migrate_non_canonical_keys``); callers routinely pass the
        # raw source_url from a file's metadata, which can be a fuller
        # form (chapter id in the path, query strings, etc.). Match the
        # storage convention before lookup.
        key = canonical_url(url) or url
        return self._library(root, create=False)["stories"].get(key)

    def stories_in(self, root: Path) -> Iterator[tuple[str, dict]]:
        for url, entry in self._library(root, create=False)["stories"].items():
            yield url, entry

    def untrackable_in(self, root: Path) -> list[dict]:
        return list(self._library(root, create=False)["untrackable"])

    def library_roots(self) -> list[str]:
        return list(self._data["libraries"].keys())

    @property
    def path(self) -> Path:
        return self._path


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "libraries": {}}


def _snapshot_unreadable_index(
    path: Path, raw: object,
) -> tuple[Path | None, str | None]:
    """Back up an index whose schema we can't read before the next save
    overwrites it.

    Returns ``(snapshot_path, save_blocker_reason)``:

    * ``(<Path>, None)`` — snapshot succeeded; save is safe.
    * ``(None, None)`` — original file didn't exist (nothing to snapshot,
      nothing to lose).
    * ``(None, "<reason>")`` — snapshot failed; the caller should attach
      the reason to the LibraryIndex's ``_save_blocker`` so subsequent
      saves refuse to atomically destroy the original.

    ``load()`` still returns an empty index in every case — the loaded
    instance must be functional enough for the UI to render. But when
    the snapshot fails, the in-memory empty index carries a blocker so
    ``save()`` raises rather than silently overwriting the corrupt-but-
    recoverable original. This was a verified data-loss path before:
    swallowing every exception here meant a disk-full or permissions
    failure on backup proceeded to ``return empty``, and the next save
    atomically obliterated the original."""
    try:
        from .backup import backup
        snapshot = backup(path)
    except Exception as exc:
        logger.warning(
            "library index at %s has an unrecognised schema "
            "(got version=%r, expected %d) and snapshot failed: %s; "
            "saves blocked until --discard-bad-index acknowledges the risk",
            path,
            raw.get("version") if isinstance(raw, dict) else None,
            SCHEMA_VERSION,
            exc,
        )
        return None, (
            f"unreadable index schema at {path}; snapshot failed ({exc!r}); "
            "saving would overwrite the original irrecoverably"
        )
    if snapshot is None:
        return None, None
    logger.warning(
        "library index at %s has an unrecognised schema "
        "(got version=%r, expected %d); snapshotted to %s before "
        "treating as empty",
        path,
        raw.get("version") if isinstance(raw, dict) else None,
        SCHEMA_VERSION,
        snapshot,
    )
    return snapshot, None


def _migrate_non_canonical_keys(raw: dict) -> None:
    """Re-key each library's ``stories`` dict by canonical URL.

    Indexes written by 1.20.x and earlier keyed entries by whatever
    source URL the parser pulled out of the file, including the
    ``/s/N/`` and ``/s/N/1/`` variants FFN uses. When two files for
    the same story happened to carry different URL shapes, both landed
    as separate entries — silently doubling up. ``canonical_url`` now
    collapses them, so on load we re-key the stored entries in place
    and merge any collisions via :func:`_merge_secondary_into_primary`.

    Collision merge preserves tracking state (``last_probed``,
    ``remote_chapter_count``, ``chapter_hashes``, ``duplicate_relpaths``)
    that an earlier version of this migration silently dropped from the
    loser — see :func:`_merge_secondary_into_primary` for the field-by-
    field policy.
    """
    # Imported locally because sites.py imports scraper modules that
    # pull in heavy dependencies; keeping the import out of module
    # scope avoids paying that cost on every library-tool invocation.
    from ..sites import canonical_url

    for lib in raw.get("libraries", {}).values():
        stories = lib.get("stories", {})
        if not isinstance(stories, dict):
            continue
        rekeyed: dict[str, dict] = {}
        for old_key, entry in stories.items():
            new_key = canonical_url(old_key) or old_key
            existing = rekeyed.get(new_key)
            if existing is None:
                rekeyed[new_key] = entry
                continue
            primary, secondary = _pick_primary_entry(existing, entry)
            _merge_secondary_into_primary(primary, secondary)
            rekeyed[new_key] = primary
        lib["stories"] = rekeyed


def _merge_secondary_into_primary(primary: dict, secondary: dict) -> None:
    """Merge tracking fields from ``secondary`` into ``primary`` after a
    migration-time collision.

    The primary was chosen by :func:`_pick_primary_entry` as the
    higher-completeness entry; its identity fields (``relpath``,
    ``title``, ``author``, ``fandoms``, ``adapter``, ``format``,
    ``chapter_count``) are retained. Tracking fields are merged
    conservatively so we don't lose ground truth from either side:

    * ``duplicate_relpaths`` — union of primary's, secondary's, and
      secondary.relpath itself, minus the primary's own relpath.
    * ``last_probed`` / ``remote_chapter_count`` — taken from whichever
      entry has the newer ``last_probed`` (treating absent as oldest).
      They travel together because they're written in lockstep by
      :meth:`mark_probed`.
    * ``chapter_hashes`` — if primary has none and secondary does, adopt
      secondary's. If both have non-empty differing lists, keep the
      primary's and log WARNING so the migration is observable. Never
      silently drop the loser's hashes without surfacing the conflict.
    """
    primary_rel = primary.get("relpath")

    dupes = primary.setdefault("duplicate_relpaths", [])
    candidate_rel = secondary.get("relpath")
    if (
        candidate_rel
        and candidate_rel not in dupes
        and candidate_rel != primary_rel
    ):
        dupes.append(candidate_rel)
    for rel in secondary.get("duplicate_relpaths") or []:
        if rel and rel not in dupes and rel != primary_rel:
            dupes.append(rel)
    if not dupes:
        primary.pop("duplicate_relpaths", None)

    primary_lp = primary.get("last_probed") or ""
    secondary_lp = secondary.get("last_probed") or ""
    if secondary_lp and secondary_lp > primary_lp:
        primary["last_probed"] = secondary_lp
        if "remote_chapter_count" in secondary:
            primary["remote_chapter_count"] = secondary["remote_chapter_count"]
        else:
            primary.pop("remote_chapter_count", None)

    primary_hashes = primary.get("chapter_hashes")
    secondary_hashes = secondary.get("chapter_hashes")
    if not primary_hashes and secondary_hashes:
        primary["chapter_hashes"] = list(secondary_hashes)
    elif (
        primary_hashes
        and secondary_hashes
        and list(primary_hashes) != list(secondary_hashes)
    ):
        logger.warning(
            "migration: conflicting chapter_hashes for collapsed entry "
            "%r; keeping primary's (%d chapters), discarding secondary's "
            "(%d chapters); silent-edit detection may need re-bootstrap "
            "for this story",
            primary_rel or "?",
            len(primary_hashes),
            len(secondary_hashes),
        )


def _entry_completeness_score(entry: dict) -> int:
    """Score a story entry by how much metadata it has.

    Used to decide which of two colliding entries keeps the primary
    ``relpath`` when merging — prefer the one with more fields
    populated rather than defaulting to whichever was scanned first.
    """
    fields = ("title", "author", "chapter_count", "rating", "status")
    return sum(1 for f in fields if entry.get(f))


def _pick_primary_entry(a: dict, b: dict) -> tuple[dict, dict]:
    """Return ``(primary, secondary)`` for two entries that collided.

    Higher-completeness wins; ties go to ``a`` (which was inserted
    first in the walk) so the merge is deterministic.
    """
    if _entry_completeness_score(b) > _entry_completeness_score(a):
        return b, a
    return a, b
