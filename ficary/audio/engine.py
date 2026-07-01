"""Shared audio engine: one mixer for live-TTS playback and the soundscape.

The reader emits lifecycle events on this engine's bus; the soundscape
subscribes and reacts (duck / fade). Both route audio through here, so there
is exactly one output device and ducking is trivial.

Design: the event bus, per-channel gain, fades, and finished-clip callbacks
live in :class:`AudioEngine` and are backend-agnostic (unit-tested with a fake
backend). The OpenAL Soft specifics are isolated in :class:`_OpenALBackend`;
when ``openal`` (PyOpenAL) or an audio device is missing, :class:`_NullBackend`
takes over and every method is a safe no-op — the reader still works in
screen-reader mode and offline audiobook export is unaffected.

NOTE: the OpenAL backend's positional/reverb playback needs on-device
verification (no audio device in CI). The abstraction is deliberate so the
backend can be fixed or swapped (e.g. to synthizer3d) without touching the
engine logic or the reader/soundscape that depend on it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from .events import Event

logger = logging.getLogger(__name__)

CHANNEL_VOICE = "voice"      # live-TTS clips, full gain
CHANNEL_AMBIENT = "ambient"  # soundscape loops, ducked while TTS speaks

_FADE_STEPS = 20  # ramp granularity for a fade/duck


def ramp_values(start: float, end: float, steps: int) -> list[float]:
    """Linear gain ramp of ``steps`` values ending exactly at ``end``.
    Pure function so fade math is testable without audio."""
    steps = max(1, int(steps))
    return [start + (end - start) * (i / steps) for i in range(1, steps + 1)]


class _Channel:
    def __init__(self) -> None:
        self.gain = 1.0          # current gain
        self.target = 1.0        # gain to restore to after a duck
        self.handles: list[int] = []
        self._fade_gen = 0       # bumps to cancel a superseded fade


class AudioEngine:
    def __init__(self, backend: Optional["_Backend"] = None):
        self._backend = backend if backend is not None else _make_backend()
        self._subscribers: list[Callable[[Event], None]] = []
        self._channels = {CHANNEL_VOICE: _Channel(), CHANNEL_AMBIENT: _Channel()}
        self._done_cbs: dict[int, Callable[[], None]] = {}
        self._lock = threading.RLock()
        self._poller: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

    @property
    def available(self) -> bool:
        return self._backend.available

    # ── event bus ─────────────────────────────────────────────────
    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, event: Event) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # one bad subscriber must not break the others
                logger.exception("audio subscriber failed on %s", event.kind)

    # ── playback ──────────────────────────────────────────────────
    def play_file(self, path, channel: str = CHANNEL_VOICE,
                  on_done: Optional[Callable[[], None]] = None) -> Optional[int]:
        chan = self._channels.setdefault(channel, _Channel())
        handle = self._backend.load(Path(path), looping=False)
        if handle is None:
            if on_done:
                on_done()
            return None
        with self._lock:
            self._backend.set_gain(handle, chan.gain)
            self._backend.play(handle)
            chan.handles.append(handle)
            if on_done:
                self._done_cbs[handle] = on_done
        self._ensure_poller()
        return handle

    def add_looping_source(self, path, *, gain: float = 1.0, positional: bool = False,
                           azimuth: float = 0.0, elevation: float = 0.0,
                           distance: float = 1.0, channel: str = CHANNEL_AMBIENT) -> Optional[int]:
        chan = self._channels.setdefault(channel, _Channel())
        handle = self._backend.load(Path(path), looping=True)
        if handle is None:
            return None
        with self._lock:
            self._backend.set_gain(handle, gain * chan.gain)
            if positional:
                self._backend.set_position(handle, azimuth, elevation, distance)
            self._backend.play(handle)
            chan.handles.append(handle)
        return handle

    def stop(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if not chan:
            return
        with self._lock:
            for h in chan.handles:
                self._backend.stop(h)
                self._done_cbs.pop(h, None)
            chan.handles.clear()

    def pause(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if chan:
            for h in chan.handles:
                self._backend.pause(h)

    def resume(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if chan:
            for h in chan.handles:
                self._backend.play(h)

    # ── gain / fades / ducking ────────────────────────────────────
    def set_gain(self, channel: str, gain: float) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        chan.gain = gain
        for h in chan.handles:
            self._backend.set_gain(h, gain)

    def duck(self, channel: str, to: float, ms: int = 250) -> None:
        """Fade a channel toward ``to``, remembering the pre-duck target."""
        self._fade(channel, to, ms, remember=False)

    def restore(self, channel: str, ms: int = 400) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        self._fade(channel, chan.target, ms, remember=False)

    def fade_in(self, channel: str, ms: int = 1500, to: float = 1.0) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        chan.target = to
        self.set_gain(channel, 0.0)
        self._fade(channel, to, ms, remember=False)

    def fade_out(self, channel: str, ms: int = 1500, then_stop: bool = True) -> None:
        self._fade(channel, 0.0, ms, remember=False,
                   done=(lambda: self.stop(channel)) if then_stop else None)

    def _fade(self, channel: str, target: float, ms: int, *, remember: bool,
              done: Optional[Callable[[], None]] = None) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        if remember:
            chan.target = target
        if ms <= 0 or not self.available:
            self.set_gain(channel, target)
            if done:
                done()
            return
        chan._fade_gen += 1
        gen = chan._fade_gen
        values = ramp_values(chan.gain, target, _FADE_STEPS)
        interval = (ms / 1000.0) / _FADE_STEPS

        def run():
            for v in values:
                if chan._fade_gen != gen:
                    return  # superseded by a newer fade
                self.set_gain(channel, v)
                if self._poll_stop.wait(interval):
                    return
            if done:
                done()

        threading.Thread(target=run, daemon=True).start()

    # ── finished-clip polling (voice chunks) ──────────────────────
    def _ensure_poller(self) -> None:
        if self._poller and self._poller.is_alive():
            return
        self._poll_stop.clear()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(0.1):
            with self._lock:
                pending = list(self._done_cbs.items())
            if not pending:
                return
            for handle, cb in pending:
                if not self._backend.is_playing(handle):
                    with self._lock:
                        self._done_cbs.pop(handle, None)
                        for chan in self._channels.values():
                            if handle in chan.handles:
                                chan.handles.remove(handle)
                    try:
                        cb()
                    except Exception:
                        logger.exception("play_file on_done callback failed")

    def set_reverb_room_size(self, size: float) -> None:
        self._backend.set_reverb(size)

    def shutdown(self) -> None:
        self._poll_stop.set()
        for name in list(self._channels):
            self.stop(name)
        self._backend.shutdown()


# ── backends ──────────────────────────────────────────────────────
class _Backend:
    available = False

    def load(self, path: Path, *, looping: bool) -> Optional[int]: return None
    def play(self, handle: int) -> None: pass
    def pause(self, handle: int) -> None: pass
    def stop(self, handle: int) -> None: pass
    def set_gain(self, handle: int, gain: float) -> None: pass
    def set_position(self, handle: int, az: float, el: float, dist: float) -> None: pass
    def is_playing(self, handle: int) -> bool: return False
    def set_reverb(self, size: float) -> None: pass
    def shutdown(self) -> None: pass


class _NullBackend(_Backend):
    """Used when PyOpenAL/an audio device isn't available. Everything no-ops."""
    available = False


class _OpenALBackend(_Backend):
    """OpenAL Soft backend via PyOpenAL. NEEDS ON-DEVICE VERIFICATION —
    there is no audio device in CI, so this path can't be exercised by tests.
    Decodes to WAV via the bundled ffmpeg, then plays through PyOpenAL."""

    available = True

    def __init__(self) -> None:
        import openal  # noqa: F401  (import proves availability)
        self._openal = openal
        self._sources: dict[int, object] = {}
        self._next = 1
        openal.oalInit()

    def _decode_to_wav(self, path: Path) -> Optional[Path]:
        if path.suffix.lower() == ".wav":
            return path
        from ..tts import FFMPEG
        import subprocess
        import tempfile
        out = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            subprocess.run([FFMPEG, "-y", "-i", str(path), str(out)],
                           check=True, capture_output=True)
            return out
        except Exception:
            logger.exception("ffmpeg decode failed for %s", path)
            return None

    def load(self, path: Path, *, looping: bool) -> Optional[int]:
        try:
            wav = self._decode_to_wav(path)
            if wav is None:
                return None
            src = self._openal.oalOpen(str(wav))
            if looping:
                src.set_looping(True)
            handle = self._next
            self._next += 1
            self._sources[handle] = src
            return handle
        except Exception:
            logger.exception("OpenAL load failed for %s", path)
            return None

    def play(self, handle): self._call(handle, "play")
    def pause(self, handle): self._call(handle, "pause")
    def stop(self, handle):
        self._call(handle, "stop")
        self._sources.pop(handle, None)

    def set_gain(self, handle, gain):
        src = self._sources.get(handle)
        if src is not None:
            try:
                src.set_gain(max(0.0, gain))
            except Exception:
                pass

    def set_position(self, handle, az, el, dist):
        import math
        src = self._sources.get(handle)
        if src is None:
            return
        rad = math.radians(az)
        try:
            src.set_position((math.sin(rad) * dist, el, -math.cos(rad) * dist))
        except Exception:
            pass

    def is_playing(self, handle) -> bool:
        src = self._sources.get(handle)
        if src is None:
            return False
        try:
            return src.get_state() == self._openal.AL_PLAYING
        except Exception:
            return False

    def set_reverb(self, size: float) -> None:
        # EFX reverb wiring is device-specific; left as a no-op until wired
        # and verified on hardware. Positional audio + ducking work without it.
        pass

    def shutdown(self) -> None:
        for src in list(self._sources.values()):
            try:
                src.stop()
            except Exception:
                pass
        self._sources.clear()
        try:
            self._openal.oalQuit()
        except Exception:
            pass

    def _call(self, handle, method):
        src = self._sources.get(handle)
        if src is not None:
            try:
                getattr(src, method)()
            except Exception:
                pass


def _make_backend() -> _Backend:
    try:
        return _OpenALBackend()
    except Exception as exc:
        logger.info("Audio engine unavailable (%s); soundscape/live-TTS disabled", exc)
        return _NullBackend()


_engine: Optional[AudioEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> AudioEngine:
    """Process-wide singleton."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = AudioEngine()
        return _engine
