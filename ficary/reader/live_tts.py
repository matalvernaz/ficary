"""Live text-to-speech for the reader (app-voice mode).

Walks a chapter's chunks, synthesizing each with the existing
``tts_providers.synthesize`` dispatcher (edge/piper) to a temp file, playing
it through the shared audio engine's ``voice`` channel, and following along
with a highlight callback. One chunk is prefetched while the current one
plays. Emits reader lifecycle events so the soundscape ducks/restores.

The sequencing is driven by a worker loop and is testable with a fake engine
(whose ``play_file`` invokes ``on_done``) and a fake synth — no audio device
or network needed.
"""
from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from ..audio.engine import CHANNEL_VOICE, AudioEngine
from ..audio.events import Event, ReaderEvent
from .chunker import Chunk, chunk_text

logger = logging.getLogger(__name__)


class LiveTTSController:
    def __init__(self, engine: AudioEngine, *, voice: str, rate: str = "0",
                 on_highlight: Optional[Callable[[Chunk], None]] = None,
                 story_key: Optional[str] = None,
                 synth: Optional[Callable[[str, str, Path], None]] = None,
                 tmp_dir: Optional[Path] = None):
        self._engine = engine
        self._voice = voice
        self._rate = rate
        self._on_highlight = on_highlight
        self._story_key = story_key
        self._synth = synth or _default_synth
        self._tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="ficary-reader-tts-"))
        self._chunks: list[Chunk] = []
        self._files: dict[int, Optional[Path]] = {}
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._chapter_number: Optional[int] = None

    def is_active(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # ── transport ─────────────────────────────────────────────────
    def start(self, text: str, chapter_number: Optional[int] = None) -> None:
        self.stop()
        self._chunks = chunk_text(text)
        self._files.clear()
        self._chapter_number = chapter_number
        self._stop.clear()
        self._resume.set()
        if not self._chunks:
            return
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def pause(self) -> None:
        self._resume.clear()
        self._engine.pause(CHANNEL_VOICE)
        self._emit(ReaderEvent.TTS_PAUSED)

    def resume(self) -> None:
        self._engine.resume(CHANNEL_VOICE)
        self._resume.set()
        self._emit(ReaderEvent.TTS_RESUMED)

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()
        self._done.set()
        self._engine.stop(CHANNEL_VOICE)
        if self._worker and self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=2.0)
        self._worker = None

    # ── worker loop ───────────────────────────────────────────────
    def _run(self) -> None:
        self._emit(ReaderEvent.TTS_STARTED)
        try:
            for i, chunk in enumerate(self._chunks):
                if self._stop.is_set():
                    break
                self._resume.wait()
                path = self._ensure_synth(i)
                self._prefetch(i + 1)
                if path is None:
                    continue
                if self._on_highlight:
                    self._on_highlight(chunk)
                self._emit(ReaderEvent.TTS_CHUNK, chunk.index)
                self._done.clear()
                self._engine.play_file(path, CHANNEL_VOICE, on_done=self._done.set)
                self._done.wait()
        finally:
            if not self._stop.is_set():
                self._emit(ReaderEvent.TTS_STOPPED)

    def _ensure_synth(self, index: int) -> Optional[Path]:
        if index in self._files:
            return self._files[index]
        if not (0 <= index < len(self._chunks)):
            return None
        out = self._tmp_dir / f"chunk_{index:05d}.mp3"
        try:
            self._synth(self._voice, self._chunks[index].text, out)
            self._files[index] = out
        except Exception:
            logger.exception("TTS synth failed for chunk %d", index)
            self._files[index] = None
        return self._files[index]

    def _prefetch(self, index: int) -> None:
        if 0 <= index < len(self._chunks) and index not in self._files:
            threading.Thread(target=self._ensure_synth, args=(index,), daemon=True).start()

    def _emit(self, kind: ReaderEvent, chunk_index: Optional[int] = None) -> None:
        payload = {"chunk_index": chunk_index} if chunk_index is not None else {}
        self._engine.emit(Event(kind=kind, story_key=self._story_key,
                                chapter_number=self._chapter_number, payload=payload))


def _default_synth(voice: str, text: str, output_path: Path) -> None:
    from ..tts_providers import synthesize
    synthesize(voice, text, output_path)
