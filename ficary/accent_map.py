"""Per-story accent + character profile JSON files.

Two on-disk files live next to each audiobook output (alongside the
existing voice / pronunciation maps):

- ``.ficary-accents-<story_id>.json``: ``{"Harry Potter": "en-GB", ...}``
  Maps speaker → BCP-47 locale code. Filters the candidate voice pool
  for each character; the audiobook generator refuses voices whose
  locale doesn't match. Special value ``"any"`` (or omission) means
  no filter.
- ``.ficary-profile-<story_id>.json``: per-character profile dict
  ``{"Harry Potter": {"gender": "male", "age": "young adult",
  "accent": "en-GB", "tone": "earnest"}}``. Computed by the LLM (when
  the LLM attribution backend is enabled) and used as a richer prior
  for VoiceMapper than the gender heuristic alone.

Both files are user-editable. Edits survive re-renders because the
generator only seeds them when they're empty.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _quarantine(path: Path, why: Exception) -> None:
    """Move a corrupt hand-editable sidecar aside instead of leaving it
    in place: the generator seeds these files when the load comes back
    empty, so a single trailing-comma typo plus one LLM-enabled render
    would otherwise REWRITE the user's whole map (the docstring promise
    is "edits survive re-renders"). Quarantined content is recoverable
    with a one-character fix."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(target)
        logger.warning(
            "%s is unreadable (%s); moved to %s — fix and rename back to "
            "keep your edits", path.name, why, target.name,
        )
    except OSError:
        logger.warning("%s is unreadable (%s); ignoring", path.name, why)


def load_accents(path: Path) -> dict[str, str]:
    """Read a ``.ficary-accents-*.json`` file. Returns ``{}`` on missing
    or wrong-shape files — accent filtering is a positive override, so a
    bad file falls through to "no filter" rather than blocking the
    render. Unparseable files are quarantined (see :func:`_quarantine`).
    ``ValueError`` covers both ``json.JSONDecodeError`` and
    ``UnicodeDecodeError`` — these files are advertised as hand-editable
    and Windows editors love saving them as UTF-16."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _quarantine(path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def save_accents(path: Path, accents: dict[str, str]) -> None:
    from .atomic import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    skeleton = {
        "_comment": (
            "Per-character accent overrides for the audiobook generator. "
            "Values are BCP-47 locale codes (en-GB, en-IE, en-AU, en-IN, "
            "fr-FR, ...) — VoiceMapper restricts each character's voice "
            "pool to that locale. Use 'any' (or omit the entry) to skip "
            "the filter for a given character."
        ),
    }
    skeleton.update(accents)
    atomic_write_text(path, json.dumps(skeleton, indent=2) + "\n")


def load_profiles(path: Path) -> dict[str, dict]:
    """Read a ``.ficary-profile-*.json`` file. Field values are coerced
    to strings on load — a hand-edited ``"gender": 1`` used to surface
    hours later as ``int.lower()`` crashing the voice-pool builder."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _quarantine(path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and isinstance(v, dict)):
            continue
        out[k] = {
            field: value.strip()
            for field, value in v.items()
            if isinstance(field, str) and isinstance(value, str) and value.strip()
        }
    return out


def save_profiles(path: Path, profiles: dict[str, dict]) -> None:
    from .atomic import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    skeleton = {
        "_comment": (
            "Per-character profiles seeded by the LLM attribution "
            "backend. Each entry: gender (male/female/neutral), age "
            "(child/teen/young adult/adult/elder), accent (BCP-47), "
            "tone (free-form descriptor). The audiobook generator "
            "uses these to pick voices that match the character "
            "rather than just gender. Hand-edit freely."
        ),
    }
    skeleton.update(profiles)
    atomic_write_text(path, json.dumps(skeleton, indent=2) + "\n")


def filter_user_entries(d: dict) -> dict:
    """Drop the ``_comment`` skeleton key when iterating — keeps the
    file self-documenting on disk without polluting the runtime view."""
    return {k: v for k, v in d.items() if not k.startswith("_")}
