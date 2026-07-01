"""Reader→audio lifecycle events.

The reader emits these; the soundscape subscribes. Neither package imports the
other — they meet at the :class:`ficary.audio.engine.AudioEngine` event bus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReaderEvent(str, Enum):
    READER_OPENED = "reader.opened"
    READER_CLOSED = "reader.closed"
    TTS_STARTED = "tts.started"
    TTS_PAUSED = "tts.paused"
    TTS_RESUMED = "tts.resumed"
    TTS_STOPPED = "tts.stopped"
    TTS_CHUNK = "tts.chunk"  # a chunk started (for fine ducking / progress)


@dataclass(frozen=True)
class Event:
    kind: ReaderEvent
    story_key: Optional[str] = None
    chapter_number: Optional[int] = None
    payload: dict = field(default_factory=dict)
