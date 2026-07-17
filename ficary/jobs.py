"""Typed parameter object for the download pipeline.

``cli._download_one`` and ``cli._build_scraper`` historically took the
argparse ``Namespace`` and read ~two dozen attributes off it. Every
non-argparse caller (the GUI's Check-for-Updates flow, the watchlist
auto-downloader) had to fabricate a fake Namespace and guess the full
attribute set — a missing field surfaced as an opaque AttributeError at
download time. :class:`DownloadJob` is that attribute set as a real
schema: one place declaring every field with the same defaults argparse
uses, plus constructors for the two non-argparse entry points.

Deliberately a *mutable* dataclass: the update-queue path deep-copies
the job per story and tweaks ``format``/``output`` in place, matching
the existing Namespace idiom, and ``cli``'s internals stay duck-typed —
they accept an argparse Namespace or a DownloadJob interchangeably.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _coerce_speech_rate(value) -> int:
    """Prefs store the TTS speech-rate percentage as a string (``"0"``,
    ``"-15"``); the CLI's argparse yields an ``int`` and the audio
    pipeline does integer arithmetic on it (``tts._combine_rate``).
    Coerce here — tolerating a stray ``%`` or a blank — so a
    prefs-seeded job never feeds a ``str`` into that arithmetic."""
    try:
        return int(str(value).strip().rstrip("%") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class DownloadJob:
    """Everything ``cli._download_one`` / ``cli._build_scraper`` read
    off ``args``. Field defaults mirror the CLI's argparse defaults."""

    # ── scraper construction (read by _build_scraper) ─────────────
    max_retries: int = 5
    no_cache: bool = False
    delay_min: Optional[float] = None
    delay_max: Optional[float] = None
    chunk_size: Optional[int] = None
    use_wayback: bool = False
    cf_solve: bool = False
    fichub: bool = False
    ao3_cookie: Optional[str] = None
    ao3_user_agent: Optional[str] = None
    webnovel_cookie: Optional[str] = None
    chyoa_max_depth: Optional[int] = None

    # ── bulk-update run options (read by _run_update_queue) ───────
    dry_run: bool = False
    skip_complete: bool = True
    probe_workers: int = 5
    recheck_interval: int = 0
    force_recheck: bool = False
    refetch_all: bool = False
    skip_stale_complete: int = 0

    # ── export/output knobs (read by _download_one) ───────────────
    format: Optional[str] = None
    output: Optional[str] = None
    chapters: Optional[str] = None
    name: Optional[str] = None  # filename template
    hr_as_stars: bool = False
    strip_notes: bool = False
    html_style: str = "modern"  # exporters.HTML_STYLE_MODERN; HTML export only
    chapter_notes: str = "keep"  # exporters.CHAPTER_NOTES_KEEP
    llm_strip_notes: bool = False
    speech_rate: int = 0
    attribution: str = "builtin"
    attribution_model_size: str = ""
    send_to_kindle: Optional[str] = None
    clean_cache: bool = False

    @classmethod
    def from_prefs(cls, **overrides) -> "DownloadJob":
        """Job seeded from the user's saved preferences — the GUI's
        Check-for-Updates flow and the watchlist auto-downloader use
        this so their downloads honour the same template/strip-notes
        settings the CLI reads from prefs. ``overrides`` set run
        options (dry_run, refetch_all, ...) on top."""
        # Imported locally so this stays importable where wxPython
        # isn't installed (Prefs no-ops gracefully without wx).
        from .exporters import DEFAULT_TEMPLATE
        from .prefs import (
            KEY_AO3_COOKIE,
            KEY_AO3_USER_AGENT,
            KEY_ATTRIBUTION_BACKEND,
            KEY_ATTRIBUTION_MODEL_SIZE,
            KEY_CHAPTER_NOTES,
            KEY_FICHUB,
            KEY_HR_AS_STARS,
            KEY_HTML_STYLE,
            KEY_LLM_STRIP_NOTES,
            KEY_NAME_TEMPLATE,
            KEY_SPEECH_RATE,
            KEY_STRIP_NOTES,
            KEY_WEBNOVEL_COOKIE,
            Prefs,
        )

        prefs = Prefs()
        job = cls(
            name=prefs.get(KEY_NAME_TEMPLATE) or DEFAULT_TEMPLATE,
            hr_as_stars=prefs.get_bool(KEY_HR_AS_STARS),
            strip_notes=prefs.get_bool(KEY_STRIP_NOTES),
            html_style=(prefs.get(KEY_HTML_STYLE) or "modern"),
            chapter_notes=(prefs.get(KEY_CHAPTER_NOTES) or "keep"),
            llm_strip_notes=prefs.get_bool(KEY_LLM_STRIP_NOTES),
            # The manual GUI download path already honours these (gui.py);
            # the update / watchlist-auto-download paths funnel through
            # here and used to drop them. Dropping the cookie meant a
            # restricted AO3/Webnovel work silently re-fetched the
            # login-gate page on every unattended update; dropping
            # speech_rate/attribution meant audio auto-renders ignored the
            # user's voice settings. Empty cookie pref -> None so
            # _build_scraper's FICARY_*_COOKIE env fallback still applies.
            ao3_cookie=(prefs.get(KEY_AO3_COOKIE) or None),
            ao3_user_agent=(prefs.get(KEY_AO3_USER_AGENT) or None),
            webnovel_cookie=(prefs.get(KEY_WEBNOVEL_COOKIE) or None),
            fichub=prefs.get_bool(KEY_FICHUB),
            attribution=(prefs.get(KEY_ATTRIBUTION_BACKEND) or "builtin"),
            attribution_model_size=(prefs.get(KEY_ATTRIBUTION_MODEL_SIZE) or ""),
            speech_rate=_coerce_speech_rate(prefs.get(KEY_SPEECH_RATE)),
        )
        for key, value in overrides.items():
            if not hasattr(job, key):
                raise TypeError(f"DownloadJob has no field {key!r}")
            setattr(job, key, value)
        return job

    @classmethod
    def from_args(cls, args) -> "DownloadJob":
        """Harvest a job from an argparse Namespace (or anything
        attribute-shaped). Missing attributes keep their schema
        defaults, so partial namespaces from tests work too."""
        job = cls()
        for name in job.__dataclass_fields__:
            if hasattr(args, name):
                setattr(job, name, getattr(args, name))
        return job
