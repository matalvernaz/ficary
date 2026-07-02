"""In-process de-duplication for downloads and exports.

Two hazards this closes, both in a single running process (the GUI, or
a one-shot CLI run — no cross-process locking; the watchlist poller
never writes story files, and ``--watchlist-run`` under cron only
polls):

* **Duplicate downloads.** The GUI download queue and the library
  update-queue both funnel through ``DownloadQueues``; the same story
  can land in the queue twice (a double-click, or a manual download of
  a story a bulk update already queued), burning rate-limited requests
  to produce the same file twice. :func:`claim` registers an
  in-flight marker keyed on the canonical story URL so the second
  caller joins the first instead of re-running.

* **Concurrent writes to one output path.** Two different-site jobs can
  render to the same templated filename. Writes are already atomic
  (:mod:`ficary.atomic`), so the file can't tear, but the second write
  clobbers the first and an interleaved read-merge-write can lose an
  update. :func:`path_lock` serialises writers to one normalised path.

Cross-process protection (a sidecar lock file) is deliberately out of
scope for v1 — documented here so the next person doesn't assume it.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# canonical story key -> the Future of the job currently downloading it
_inflight: dict[str, Future] = {}
_inflight_lock = threading.Lock()


def claim(key: str, future: Future) -> Optional[Future]:
    """Register ``future`` as the in-flight download for ``key``.

    Returns ``None`` if ``key`` was free (the caller owns it and must
    arrange for :func:`release` to run when the future settles — wire it
    with ``future.add_done_callback``). Returns the existing in-flight
    Future if one is already registered, so the caller can join it
    instead of starting a duplicate download.
    """
    with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None and not existing.done():
            return existing
        _inflight[key] = future
        return None


def release(key: str, future: Future) -> None:
    """Clear ``key``'s registration if it still points at ``future``.

    Guarded on identity so a late release from a superseded job can't
    evict a newer in-flight registration for the same key."""
    with _inflight_lock:
        if _inflight.get(key) is future:
            del _inflight[key]


def _normalise(path) -> str:
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


_path_locks: dict[str, threading.RLock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path) -> threading.RLock:
    key = _normalise(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            # RLock so a thread already holding the outer update-path
            # lock can re-enter via the exporter's own path_lock without
            # deadlocking. Never pruned — a lock is a few bytes and the
            # population is bounded by distinct output paths per session.
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


@contextmanager
def path_lock(path):
    """Serialise writers to one output path (normalised, case-folded).

    Second caller for the same path waits — skipping would silently
    drop a legitimate export. Re-entrant on the same thread."""
    lock = _lock_for(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
