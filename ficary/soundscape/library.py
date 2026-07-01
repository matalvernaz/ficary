"""On-disk soundscape library: JSON definitions in ``soundscapes_dir()``,
and resolution of a sound reference to a playable file."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from .. import portable
from .model import Soundscape

logger = logging.getLogger(__name__)

_AUDIO_EXTS = (".ogg", ".wav", ".mp3", ".flac", ".m4a")


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return s or "soundscape"


def _dir() -> Path:
    d = portable.soundscapes_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_slugs() -> list[str]:
    return sorted(p.stem for p in _dir().glob("*.json"))


def load(slug: str) -> Optional[Soundscape]:
    path = _dir() / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return Soundscape.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.warning("Soundscape %s unreadable (%s); ignoring", slug, exc)
        return None


def save(sc: Soundscape) -> str:
    slug = slugify(sc.name)
    path = _dir() / f"{slug}.json"
    path.write_text(json.dumps(sc.to_dict(), indent=2) + "\n", encoding="utf-8")
    return slug


def delete(slug: str) -> None:
    try:
        (_dir() / f"{slug}.json").unlink(missing_ok=True)
    except OSError:
        pass


def resolve_source(source: str) -> Optional[Path]:
    """Resolve a :class:`Sound` source to a playable file path.

    ``bundled:<name>`` resolves to a shipped loop under this package's
    ``bundled/`` dir; anything else is a filename under ``sounds_dir()``.
    Returns None if the file isn't present.
    """
    if source.startswith("bundled:"):
        name = source[len("bundled:"):]
        base = Path(__file__).parent / "bundled"
        for ext in _AUDIO_EXTS:
            p = base / f"{name}{ext}"
            if p.exists():
                return p
        return None
    p = portable.sounds_dir() / source
    return p if p.exists() else None
