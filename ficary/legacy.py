"""Backward-compat shims for the ffn-dl → Ficary rename (2.5.0).

The project was called ffn-dl through 2.4.x. This module migrates or
falls back to the pre-rename on-disk names so an in-place upgrade keeps
a user's settings, library index, cached chapters, and per-story TTS
maps. Everything here is best-effort: a failed migration degrades to
"regenerate from defaults", never a crash.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_LEGACY_SIDECAR_PREFIX = ".ffn-"
_SIDECAR_PREFIX = ".ficary-"

# Pre-rename data-directory names, still read once on upgrade.
LEGACY_DIR_NAME = ".ffn-dl"
LEGACY_CACHE_NAME = "ffn-dl"
LEGACY_ENV_PREFIX = "FFN_DL_"
ENV_PREFIX = "FICARY_"


def migrate_sidecar(new_path: Path) -> Path:
    """Return ``new_path``, first renaming a pre-rename ``.ffn-*`` sibling
    onto it when the new file doesn't exist yet.

    Per-story TTS sidecars (voices/pronunciations/accents/profile) live
    next to each book; this carries a user's hand-edited maps across the
    rename. Idempotent. On a rename failure it returns the legacy path so
    the file is still read in place this run.
    """
    if new_path.exists():
        return new_path
    name = new_path.name
    if not name.startswith(_SIDECAR_PREFIX):
        return new_path
    legacy = new_path.with_name(_LEGACY_SIDECAR_PREFIX + name[len(_SIDECAR_PREFIX):])
    if legacy.exists():
        try:
            legacy.rename(new_path)
        except OSError as exc:
            logger.warning("Sidecar migration %s -> %s failed: %s", legacy, new_path, exc)
            return legacy
    return new_path


def getenv_compat(name: str, default: str = "") -> str:
    """``os.environ.get`` for a ``FICARY_*`` var, falling back to the
    pre-rename ``FFN_DL_*`` name so existing scripts keep working."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith(ENV_PREFIX):
        legacy = os.environ.get(LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):])
        if legacy is not None:
            return legacy
    return default


def migrate_dir(old: Path, new: Path) -> None:
    """Move a whole pre-rename data directory to its new location, once.

    Fires only when ``old`` exists and ``new`` doesn't; any other state is
    left untouched so real data is never clobbered. Best-effort — a failed
    move just means the app starts fresh at ``new``.
    """
    try:
        if not old.exists() or new.exists():
            return
        new.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        # Cross-filesystem, shutil.move is copytree-then-rmtree; a crash
        # mid-copy would leave a partial ``new`` that the exists() check
        # above then treats as done forever, stranding the real data in
        # ``old``. Stage the copy and rename into place — the rename is
        # same-directory, so it can't be torn.
        staging = new.parent / (new.name + ".migrating")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.move(str(old), str(staging))
        os.rename(staging, new)
        logger.info("Migrated pre-rename data %s -> %s", old, new)
    except OSError as exc:
        logger.warning("Data-dir migration %s -> %s failed: %s", old, new, exc)


def migrate_data_dirs() -> None:
    """Migrate the pre-rename dev/pip data + cache dirs to their Ficary
    names. Frozen builds keep their data beside the exe, so this only
    matters for pip/dev installs and the Windows LOCALAPPDATA fallback.
    Called once at startup, before the new dirs are created."""
    home = Path.home()
    migrate_dir(home / LEGACY_DIR_NAME, home / ".ficary")
    migrate_dir(home / ".cache" / LEGACY_CACHE_NAME, home / ".cache" / "ficary")
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        migrate_dir(Path(localappdata) / LEGACY_CACHE_NAME, Path(localappdata) / "ficary")
