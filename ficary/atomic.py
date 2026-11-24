"""Atomic file writes.

A crash (OS kill, power loss, user Ctrl-C mid-export) is much more
likely to happen during a long-running ficary run than is obvious — a
library-wide update hits hundreds of file writes, and losing one to a
half-written file corrupts the entry silently. The symptoms then show
up later as "why is this EPUB unreadable?" or "why did the next scan
mark this story as untrackable?"

This module centralises the write-to-tmp, fsync, rename-over-target
idiom so every file the program persists lands whole-or-not-at-all.
Before this existed, only ``LibraryIndex.save`` and ``watchlist.save``
bothered; exporters and the scraper cache wrote directly to the
destination, and an interrupted export left a partial file sitting in
the library.

Design notes:

* The rename is atomic on POSIX and (since Python 3.3) on Windows when
  ``os.replace`` is used. We never use ``os.rename`` here because it
  fails if the destination exists on Windows.

* ``fsync`` on the temp file forces pending page-cache data to disk
  before the rename is committed, so a power loss between the rename
  and the actual data hitting the platter still leaves either the old
  file or the new one — never garbage.

* Permissions. A file that already exists keeps its permission bits.
  A new one is private (owner only) unless the caller passes
  ``shared=True``, in which case it gets whatever any other new file in
  that folder would get: the process umask and the folder's default
  ACL decide. Books and audiobooks are written shared, so a media
  server running under another account can read them. Settings,
  cookies, keys and caches stay private.

* We do *not* fsync the enclosing directory. On ext4 + most journalled
  filesystems the rename itself is journalled and durable; the extra
  sync would double the wall time of a library-wide export run for a
  guarantee users don't need (the index is our source of truth; a
  missing file just triggers a re-download). If a durability-critical
  caller ever needs it, pass ``fsync_dir=True``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Union

PathLike = Union[str, os.PathLike[str]]


def _new_temp(target_path: Path, suffix: str, shared: bool) -> tuple[int, str]:
    """Create the temporary file a write goes into, beside the target.

    Private by default: ``tempfile.mkstemp`` makes it owner-only, and a
    new target keeps that. With ``shared`` it is created the way any
    program creates a new file, asking for read and write for everyone
    and letting the umask and the folder's default ACL take away what
    they take away. That is "the system's usual setting" exactly, with
    no reading of the umask (reading it means changing it for a moment,
    which would race other threads creating files).
    """
    prefix = f".{target_path.name}."
    if not shared:
        return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(target_path.parent))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _attempt in range(100):
        name = target_path.parent / f"{prefix}{secrets.token_hex(4)}{suffix}"
        try:
            return os.open(str(name), flags, 0o666), str(name)
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not find a free temporary name beside {target_path}")


def _inherit_target_perms(tmp_path: Path, target_path: Path) -> None:
    """Carry the target file's permission bits onto the temp before
    the atomic rename.

    ``tempfile.mkstemp`` defaults to mode 0600. Without this step the
    rename installs that mode in place of whatever the user (or another
    tool) had granted — a shared library on a NAS becomes unreadable
    by the Plex / Calibre user, a chmod 644 config file silently
    becomes private. We only run it when the target already exists;
    a new file keeps the mode its temporary file was created with (see
    :func:`_new_temp`)."""
    if not target_path.exists():
        return
    try:
        shutil.copymode(target_path, tmp_path)
    except OSError:
        # copymode is best-effort: tmpfs, network mounts, Windows ACLs
        # where chmod is a no-op. The data write is more important than
        # the mode mirror, so we don't surface the failure.
        return


def atomic_write_text(
    path: PathLike,
    content: str,
    *,
    encoding: str = "utf-8",
    fsync_dir: bool = False,
    shared: bool = False,
) -> None:
    """Write ``content`` to ``path`` atomically.

    The target path either ends up with the full new content or is
    left untouched; there's no half-written window a concurrent
    reader (or a crash-restart re-scan) could observe. ``shared``: see
    the module notes on permissions.
    """
    _atomic_write(
        path,
        content.encode(encoding),
        fsync_dir=fsync_dir,
        shared=shared,
    )


def atomic_write_bytes(
    path: PathLike,
    content: bytes,
    *,
    fsync_dir: bool = False,
    shared: bool = False,
) -> None:
    """Binary equivalent of :func:`atomic_write_text`.

    Useful for anything that's already been serialised into bytes —
    cover images, the EPUB zip (when we've built it in memory first),
    and other binary artefacts.
    """
    _atomic_write(path, content, fsync_dir=fsync_dir, shared=shared)


@contextmanager
def atomic_path(
    target: PathLike,
    *,
    suffix: str = ".tmp",
    fsync_dir: bool = False,
    shared: bool = False,
) -> Generator[Path, None, None]:
    """Yield a temporary Path in the same directory as ``target``.

    On successful exit, ``target`` is atomically replaced by the temp
    file's contents. On any exception, the temp file is removed and
    the original ``target`` is left untouched.

    Use this when a third-party library insists on writing to a
    filesystem path itself (``ebooklib.write_epub``, for example):
    hand it the yielded temp path instead of the real target and let
    the context manager swap it in atomically on success.
    """
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # The file descriptor is closed at once: the third-party writer
    # opens the path itself. The leading dot keeps the temp file hidden
    # in directory listings.
    fd, tmp_name = _new_temp(target_path, suffix, shared)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        yield tmp_path
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    # Success path: fsync the temp, mirror the target's perms onto it
    # (so an existing 0644 file doesn't silently become 0600), then
    # atomically swap. If the swap itself fails — Windows file lock,
    # antivirus interception, target-directory permission flip — the
    # temp would otherwise be orphaned in the parent dir; the
    # try/finally cleans it up.
    try:
        # The third-party writer (e.g. ebooklib.write_epub) typically
        # doesn't fsync, so the temp file's bytes may still be in the
        # page cache when we rename. Without this fsync, a power loss
        # between rename and writeback can leave a renamed-but-empty
        # target — the failure mode this module exists to prevent.
        try:
            fd = os.open(str(tmp_path), os.O_RDONLY)
            try:
                os.fsync(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
        except OSError:
            pass
        _inherit_target_perms(tmp_path, target_path)
        os.replace(tmp_path, target_path)
        if fsync_dir:
            _fsync_dir(target_path.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write(
    path: PathLike,
    payload: bytes,
    *,
    fsync_dir: bool,
    shared: bool = False,
) -> None:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = _new_temp(target_path, ".tmp", shared)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            # fsync the file before rename so the rename commit can't
            # outrun the data to disk on a crash.
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't supported on every filesystem (some
                # network mounts, tmpfs in unusual configurations).
                # The rename is still atomic — we just lose the
                # durability guarantee on those mounts.
                pass
        _inherit_target_perms(Path(tmp_name), target_path)
        os.replace(tmp_name, target_path)
    except BaseException:
        # The fdopen block already closed the fd on success; on
        # failure it may or may not have, so we try unlink and ignore
        # a "file gone" error.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if fsync_dir:
        _fsync_dir(target_path.parent)


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory. Silent on platforms (Windows)
    that don't allow opening a directory for fsync."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
