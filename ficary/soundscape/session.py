"""Bridges reader lifecycle events to the shared engine's ambient channel.

Subscribes to the audio engine's event bus and reacts by calling engine mixer
methods only (fade in on open, duck under narration, restore, fade out on
close) — no wx, so it's safe on the bus's worker thread. The reader owns the
``voice`` channel; this owns ``ambient``.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..audio.engine import CHANNEL_AMBIENT, AudioEngine
from ..audio.events import Event, ReaderEvent
from . import library
from .model import Soundscape

logger = logging.getLogger(__name__)

_DUCK_LEVEL = 0.25
_FADE_IN_MS = 1500
_FADE_OUT_MS = 1500
_DUCK_MS = 250
_RESTORE_MS = 400


class SoundscapeSession:
    """One per open reader. Holds the story's assigned soundscape (or None)."""

    def __init__(self, engine: AudioEngine, soundscape: Optional[Soundscape] = None):
        self._engine = engine
        self._soundscape = soundscape
        self._started = False
        engine.subscribe(self._on_event)

    def set_soundscape(self, soundscape: Optional[Soundscape]) -> None:
        was_running = self._started
        if was_running:
            self._teardown()
        self._soundscape = soundscape
        if was_running:
            self._build_and_fade_in()

    def close(self) -> None:
        self._engine.unsubscribe(self._on_event)
        self._teardown()

    def _on_event(self, event: Event) -> None:
        kind = event.kind
        if kind is ReaderEvent.READER_OPENED:
            self._build_and_fade_in()
        elif kind is ReaderEvent.READER_CLOSED:
            self._fade_out()
        elif kind in (ReaderEvent.TTS_STARTED, ReaderEvent.TTS_RESUMED):
            if self._started:
                self._engine.duck(CHANNEL_AMBIENT, _DUCK_LEVEL, _DUCK_MS)
        elif kind in (ReaderEvent.TTS_PAUSED, ReaderEvent.TTS_STOPPED):
            if self._started:
                self._engine.restore(CHANNEL_AMBIENT, _RESTORE_MS)

    def _build_and_fade_in(self) -> None:
        if self._started or not self._soundscape:
            return
        sc = self._soundscape
        self._engine.set_reverb_room_size(sc.reverb_room_size)
        added = False
        for snd in sc.sounds:
            path = library.resolve_source(snd.source)
            if path is None:
                logger.info("Soundscape sound not found: %s", snd.source)
                continue
            self._engine.add_looping_source(
                path, gain=snd.volume, positional=snd.positional,
                azimuth=snd.azimuth, elevation=snd.elevation,
                distance=snd.distance, channel=CHANNEL_AMBIENT)
            added = True
        if added:
            self._started = True
            self._engine.fade_in(CHANNEL_AMBIENT, _FADE_IN_MS, to=sc.master_volume)

    def fade_out(self) -> None:
        """Public fade-out of the ambient bed (used by the sleep timer)."""
        self._fade_out()

    def _fade_out(self) -> None:
        if self._started:
            self._engine.fade_out(CHANNEL_AMBIENT, _FADE_OUT_MS, then_stop=True)
            self._started = False

    def _teardown(self) -> None:
        self._engine.stop(CHANNEL_AMBIENT)
        self._started = False
