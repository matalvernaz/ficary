"""Scan orchestrator: walk → read → identify → index. No file moves."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..updater import extract_metadata
from .candidate import Confidence
from .identifier import identify
from .index import LibraryIndex


_EXTS = (".epub", ".html", ".txt")


def _walk_files(root: Path, recursive: bool) -> Iterator[Path]:
    """Yield every file under ``root`` without following symlinks.

    Using ``os.walk(followlinks=False)`` instead of ``Path.rglob``
    protects against two failure modes: self-referential symlinks
    that would loop forever, and unintended double-indexing when a
    user's library contains convenience symlinks to files that
    already live elsewhere in the tree.
    """
    if not recursive:
        for entry in root.iterdir():
            if entry.is_file() and not entry.is_symlink():
                yield entry
        return
    for dirpath, _dirnames, filenames in os.walk(str(root), followlinks=False):
        for fname in filenames:
            candidate = Path(dirpath) / fname
            # Still skip symlink files even when walking without
            # following — they could point outside the library or
            # duplicate indexed content.
            if candidate.is_symlink():
                continue
            yield candidate


@dataclass
class ScanResult:
    root: Path
    total_files: int = 0
    identified_via_url: int = 0
    ambiguous: int = 0
    errors: int = 0
    duplicates: int = 0
    newly_abandoned: int = 0
    error_files: list[tuple[Path, str]] = field(default_factory=list)


def _resolve_abandoned_threshold(override: int | None) -> int:
    """Return the abandonment threshold in days, or 0 when disabled.

    Precedence: explicit ``override`` arg > ``KEY_LIBRARY_ABANDONED_
    AFTER_DAYS`` pref > 0. The pref lookup is lazy so the scanner
    stays importable in test/headless contexts where Prefs' wx-
    settings backend can't initialise.
    """
    if override is not None:
        return max(0, int(override))
    try:
        from ..prefs import KEY_LIBRARY_ABANDONED_AFTER_DAYS, Prefs
        raw = Prefs().get(KEY_LIBRARY_ABANDONED_AFTER_DAYS) or ""
    except Exception:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _resolve_bucket_folders() -> tuple[str, str]:
    """Return ``(adult_folder, original_folder)`` from user prefs, or
    the built-in defaults when prefs can't be read (test/headless).

    Threaded into :func:`~ffn_dl.library.identifier.identify` so the
    scanner can override embedded fandoms / parent-folder backfills
    for adult and original-fiction adapters — those buckets are
    determined by source site, not by wherever the file sits today.
    """
    from .template import DEFAULT_ADULT_FOLDER, DEFAULT_ORIGINAL_FOLDER
    try:
        from ..prefs import (
            KEY_LIBRARY_ADULT_FOLDER, KEY_LIBRARY_ORIGINAL_FOLDER, Prefs,
        )
        prefs = Prefs()
        adult = prefs.get(KEY_LIBRARY_ADULT_FOLDER) or DEFAULT_ADULT_FOLDER
        original = (
            prefs.get(KEY_LIBRARY_ORIGINAL_FOLDER) or DEFAULT_ORIGINAL_FOLDER
        )
        return adult, original
    except Exception:
        return DEFAULT_ADULT_FOLDER, DEFAULT_ORIGINAL_FOLDER


def scan(
    root: Path,
    *,
    index_path: Path | None = None,
    recursive: bool = True,
    clear_existing: bool = False,
    abandoned_after_days: int | None = None,
) -> ScanResult:
    """Scan ``root`` and populate/update the library index.

    ``clear_existing`` replaces this library's entries with the scan
    results instead of merging — use it when the user wants orphans
    (files deleted off disk) dropped from the index.

    ``abandoned_after_days`` controls the inline abandoned-WIP sweep
    that runs at the end of every scan. ``None`` (the default) reads
    the threshold from ``KEY_LIBRARY_ABANDONED_AFTER_DAYS`` in user
    prefs; ``0`` disables the sweep for this run regardless of the
    pref; any positive integer forces the threshold to that value.
    Rolling the sweep into ``scan`` matches user expectation —
    "just a thing that happens when I scan" — and folds the
    newly-marked count into :class:`ScanResult` so the CLI summary
    can surface it.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    index = LibraryIndex.load(index_path)
    # ``clear_existing`` rebuilds the index from disk state, so a
    # per-file extraction error mid-scan used to delete that file's
    # prior entry without warning — a poisoned FicHub HTML on rescan
    # day silently demoted a tracked story to "missing" until the user
    # noticed it disappeared from --library-stats. Cache the pre-clear
    # state by relpath so we can re-inject any file the per-file
    # extractor fails on, mirroring the merge semantics of a normal
    # (non-clear) rescan. Keyed by relpath since URL might not match
    # if the failing file was tracked under a non-canonical URL form.
    preserved_by_relpath: dict[str, tuple[str, dict]] = {}
    if clear_existing:
        for url, entry in list(index.stories_in(root)):
            rel = entry.get("relpath")
            if rel:
                preserved_by_relpath[rel] = (url, dict(entry))
        index.clear_library(root)

    result = ScanResult(root=root)

    adult_folder, original_folder = _resolve_bucket_folders()

    for path in _walk_files(root, recursive):
        if path.suffix.lower() not in _EXTS:
            continue
        result.total_files += 1
        try:
            md = extract_metadata(path)
            # Pass ``root`` so identify() can backfill the fandom from
            # the parent folder when the file's HTML metadata didn't
            # include one (most common on FicLab dumps whose tags row
            # mixes genres/characters/status/fandom into one blob).
            #
            # Pass the configured adult/original folder names so adult
            # and original-fiction adapters route to their dedicated
            # buckets regardless of where the file currently sits —
            # this is what migrates legacy erotica out of fandom folders
            # on the next scan + reorganise cycle.
            candidate = identify(
                path, md, root=root,
                adult_folder=adult_folder,
                original_folder=original_folder,
            )
            # record() returns False when this candidate was a
            # duplicate of a story already indexed under the same
            # canonical URL — the second (third, …) copy is recorded
            # in the primary entry's duplicate_relpaths list. Surface
            # the count so --scan-library can tell the user.
            is_new = index.record(root, candidate)
            if not is_new:
                result.duplicates += 1
            if candidate.confidence == Confidence.HIGH:
                result.identified_via_url += 1
            else:
                result.ambiguous += 1
        except Exception as exc:
            result.errors += 1
            result.error_files.append((path, str(exc)))
            # If this file had a prior entry and we cleared the
            # library before the loop, re-inject the cached copy so a
            # transient parse error doesn't silently drop a tracked
            # story from the index. The relpath relative-to-root form
            # matches what ``index.record`` writes.
            if preserved_by_relpath:
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    rel = ""
                cached = preserved_by_relpath.get(rel)
                if cached is not None:
                    url, prior = cached
                    index.library_state(root)["stories"][url] = prior

    days = _resolve_abandoned_threshold(abandoned_after_days)
    if days > 0:
        # Late import: abandoned.py imports extract_status out of
        # updater.py, and scanner.py is itself imported from cli.py
        # at startup. Keeping the import inside the conditional
        # avoids doing the work when the feature is off.
        from .abandoned import mark_abandoned

        report = mark_abandoned(index, root, days)
        result.newly_abandoned = report.newly_marked_count

    index.mark_scan_complete(root)
    index.save()
    return result
