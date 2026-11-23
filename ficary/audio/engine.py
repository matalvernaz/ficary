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

Gain model: each channel has a ``target`` (the designed volume — what a
restore returns to) and a current effective ``gain`` (which ducks and fades
move). Each handle keeps a per-sound base gain; the backend always receives
``base * channel_gain``, so channel-wide fades preserve the soundscape mix.

Pause model: pause/resume are channel-level state. The finished-clip poller
skips paused channels — a paused source reports "not playing" to the backend,
which must not be mistaken for completion. New sounds added to a paused
channel register paused and start on resume.

NOTE: the OpenAL backend's positional/reverb playback needs on-device
verification (no audio device in CI). The abstraction is deliberate so the
backend can be fixed or swapped (e.g. to synthizer3d) without touching the
engine logic or the reader/soundscape that depend on it.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from .events import Event

logger = logging.getLogger(__name__)

CHANNEL_VOICE = "voice"      # live-TTS clips, full gain
CHANNEL_AMBIENT = "ambient"  # soundscape loops, ducked while TTS speaks

_FADE_STEPS = 20  # ramp granularity for a fade/duck

# Why the engine could not start. Each needs a different thing from the
# user, so the reason is kept and not just the fact.
UNAVAILABLE_NO_PACKAGE = "package"  # PyOpenAL is not installed
UNAVAILABLE_NO_LIBRARY = "library"  # PyOpenAL is; the OpenAL library it loads is not
UNAVAILABLE_NO_DEVICE = "device"    # the library loaded, no sound output opened
UNAVAILABLE_OTHER = "other"

# The file PyOpenAL's 64-bit Windows wheel carries OpenAL Soft in, named
# so a user told it is missing can check for it.
_WINDOWS_OPENAL_DLL = "soft_oal_64.dll"


def ramp_values(start: float, end: float, steps: int) -> list[float]:
    """Linear gain ramp of ``steps`` values ending exactly at ``end``.
    Pure function so fade math is testable without audio."""
    steps = max(1, int(steps))
    return [start + (end - start) * (i / steps) for i in range(1, steps + 1)]


class _Channel:
    def __init__(self) -> None:
        self.gain = 1.0          # current effective gain (ducks/fades move this)
        self.target = 1.0        # designed gain to restore to after a duck
        self.paused = False
        self.handles: list[int] = []
        self.base_gains: dict[int, float] = {}  # handle -> per-sound gain
        self._fade_gen = 0       # bumps to cancel a superseded fade
        self._stop_gen = 0       # bumps on stop(); in-flight loads check it


class AudioEngine:
    def __init__(self, backend: Optional["_Backend"] = None):
        self._backend = backend if backend is not None else _make_backend()
        self._subscribers: list[Callable[[Event], None]] = []
        self._channels = {CHANNEL_VOICE: _Channel(), CHANNEL_AMBIENT: _Channel()}
        self._done_cbs: dict[int, Callable[[], None]] = {}
        self._handle_channel: dict[int, _Channel] = {}
        self._lock = threading.RLock()
        self._poller: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

    @property
    def available(self) -> bool:
        return self._backend.available

    @property
    def unavailable_reason(self) -> Optional[str]:
        """One of the ``UNAVAILABLE_*`` codes, or None when audio works."""
        if self._backend.available:
            return None
        return getattr(self._backend, "reason", None) or UNAVAILABLE_OTHER

    @property
    def unavailable_detail(self) -> str:
        """The underlying error text, for the log and for the one reason
        that has no better explanation."""
        if self._backend.available:
            return ""
        return getattr(self._backend, "detail", "") or ""

    def retry_backend(self) -> bool:
        """Try once more to start audio, and say whether it is available.

        The engine is made once per run of the app, on the first reader
        window. If headphones were not connected yet at that moment,
        audio stayed off until the app was restarted. Trying again when
        the user presses Play costs a moment and fixes that.
        """
        with self._lock:
            if self._backend.available:
                return True
            # Safe to swap: a backend that never started holds no sounds.
            self._backend = _make_backend()
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
        """Play a one-shot clip. Returns the handle, or None if the load
        failed (``on_done`` fires immediately) or a ``stop(channel)`` raced
        the load (``on_done`` does NOT fire — the stop's own signalling is
        authoritative; callers wait with stop-aware timeout loops)."""
        chan = self._channels.setdefault(channel, _Channel())
        with self._lock:
            stop_gen = chan._stop_gen
        handle = self._backend.load(Path(path), looping=False)
        if handle is None:
            if on_done:
                on_done()
            return None
        with self._lock:
            if chan._stop_gen != stop_gen:
                # stop(channel) ran while the backend was loading — this
                # clip must not start or register.
                stale = True
            else:
                stale = False
                chan.base_gains[handle] = 1.0
                self._backend.set_gain(handle, chan.gain)
                self._backend.play(handle)
                if chan.paused:
                    self._backend.pause(handle)
                chan.handles.append(handle)
                self._handle_channel[handle] = chan
                if on_done:
                    self._done_cbs[handle] = on_done
        if stale:
            self._backend.stop(handle)
            return None
        self._ensure_poller()
        return handle

    def add_looping_source(self, path, *, gain: float = 1.0, positional: bool = False,
                           azimuth: float = 0.0, elevation: float = 0.0,
                           distance: float = 1.0, channel: str = CHANNEL_AMBIENT) -> Optional[int]:
        chan = self._channels.setdefault(channel, _Channel())
        with self._lock:
            stop_gen = chan._stop_gen
        handle = self._backend.load(Path(path), looping=True)
        if handle is None:
            return None
        with self._lock:
            if chan._stop_gen != stop_gen:
                stale = True
            else:
                stale = False
                chan.base_gains[handle] = gain
                self._backend.set_gain(handle, gain * chan.gain)
                if positional:
                    self._backend.set_position(handle, azimuth, elevation, distance)
                self._backend.play(handle)
                if chan.paused:
                    self._backend.pause(handle)
                chan.handles.append(handle)
                self._handle_channel[handle] = chan
        if stale:
            self._backend.stop(handle)
            return None
        return handle

    def stop(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if not chan:
            return
        with self._lock:
            chan._stop_gen += 1
            chan.paused = False
            handles = list(chan.handles)
            chan.handles.clear()
            chan.base_gains.clear()
            for h in handles:
                self._done_cbs.pop(h, None)
                self._handle_channel.pop(h, None)
        for h in handles:
            self._backend.stop(h)

    def stop_handles(self, channel: str, handles) -> None:
        """Stop only ``handles`` on ``channel``, leaving the rest alone.

        A channel-wide :meth:`stop` is the wrong tool for an obsolete
        worker cleaning up after itself: by the time it notices it has
        been superseded, the replacement's sources are already on the
        same channel and a blanket stop silences them too.
        """
        wanted = [h for h in handles if h is not None]
        if not wanted:
            return
        chan = self._channels.get(channel)
        if not chan:
            return
        with self._lock:
            owned = [h for h in wanted if h in chan.handles]
            for h in owned:
                chan.handles.remove(h)
                chan.base_gains.pop(h, None)
                self._done_cbs.pop(h, None)
                self._handle_channel.pop(h, None)
        for h in owned:
            self._backend.stop(h)

    def pause(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if not chan:
            return
        with self._lock:
            chan.paused = True
            handles = list(chan.handles)
        for h in handles:
            self._backend.pause(h)

    def resume(self, channel: str) -> None:
        chan = self._channels.get(channel)
        if not chan:
            return
        with self._lock:
            chan.paused = False
            handles = list(chan.handles)
        for h in handles:
            self._backend.play(h)

    # ── gain / fades / ducking ────────────────────────────────────
    def set_gain(self, channel: str, gain: float) -> None:
        """Set the channel's designed volume. Also becomes the level a
        later ``restore()`` returns to."""
        chan = self._channels.setdefault(channel, _Channel())
        with self._lock:
            chan.target = gain
        self._apply_gain(channel, gain)

    def _apply_gain(self, channel: str, gain: float) -> None:
        """Move the channel's effective gain without touching the restore
        target — the fade/duck primitive."""
        chan = self._channels.setdefault(channel, _Channel())
        with self._lock:
            chan.gain = gain
            pairs = [(h, chan.base_gains.get(h, 1.0)) for h in chan.handles]
        for h, base in pairs:
            self._backend.set_gain(h, base * gain)

    def duck(self, channel: str, to: float, ms: int = 250) -> None:
        """Fade a channel's effective gain toward ``to``. The designed
        volume (``target``) is untouched, so ``restore`` undoes the duck."""
        self._fade(channel, to, ms)

    def restore(self, channel: str, ms: int = 400) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        self._fade(channel, chan.target, ms)

    def fade_in(self, channel: str, ms: int = 1500, to: float = 1.0) -> None:
        """Ramp the channel from its current effective gain to ``to``.
        Callers bringing up a channel from silence should ``set_gain(ch, 0)``
        (or build their sources at zero) first — fade_in no longer hard-cuts
        an audible channel to zero before ramping."""
        chan = self._channels.setdefault(channel, _Channel())
        with self._lock:
            chan.target = to
        self._fade(channel, to, ms)

    def fade_out(self, channel: str, ms: int = 1500, then_stop: bool = True) -> None:
        self._fade(channel, 0.0, ms,
                   done=(lambda: self.stop(channel)) if then_stop else None)

    def _fade(self, channel: str, target: float, ms: int, *,
              done: Optional[Callable[[], None]] = None) -> None:
        chan = self._channels.setdefault(channel, _Channel())
        if ms <= 0 or not self.available:
            self._apply_gain(channel, target)
            if done:
                done()
            return
        with self._lock:
            chan._fade_gen += 1
            gen = chan._fade_gen
            start = chan.gain
        values = ramp_values(start, target, _FADE_STEPS)
        interval = (ms / 1000.0) / _FADE_STEPS

        def run():
            for v in values:
                if chan._fade_gen != gen:
                    return  # superseded by a newer fade
                self._apply_gain(channel, v)
                if self._poll_stop.wait(interval):
                    return
            if done and chan._fade_gen == gen:
                done()

        threading.Thread(target=run, daemon=True).start()

    # ── finished-clip polling (voice chunks) ──────────────────────
    def _ensure_poller(self) -> None:
        with self._lock:
            if self._poller and self._poller.is_alive():
                return
            self._poll_stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, daemon=True)
            self._poller.start()

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(0.1):
            with self._lock:
                if not self._done_cbs:
                    self._poller = None
                    return
                pending = list(self._done_cbs.keys())
            for handle in pending:
                with self._lock:
                    chan = self._handle_channel.get(handle)
                    if handle not in self._done_cbs or chan is None or chan.paused:
                        continue
                playing = self._backend.is_playing(handle)
                if playing:
                    continue
                with self._lock:
                    chan = self._handle_channel.get(handle)
                    if chan is None or chan.paused:
                        continue  # paused (or stopped) between probe and now
                    cb = self._done_cbs.pop(handle, None)
                    self._handle_channel.pop(handle, None)
                    if handle in chan.handles:
                        chan.handles.remove(handle)
                    chan.base_gains.pop(handle, None)
                # Release the backend source — finished one-shots used to
                # accumulate in the backend for the life of the process.
                self._backend.stop(handle)
                if cb is None:
                    continue
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
    """Used when PyOpenAL/an audio device isn't available. Everything
    no-ops. Remembers why, as an ``UNAVAILABLE_*`` code plus the error's
    own text."""
    available = False

    def __init__(self, reason: Optional[str] = None, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail


class _OpenALBackend(_Backend):
    """OpenAL Soft backend via PyOpenAL. NEEDS ON-DEVICE VERIFICATION —
    there is no audio device in CI, so this path can't be exercised by tests.
    Decodes to WAV via the bundled ffmpeg, then plays through PyOpenAL."""

    available = True

    def __init__(self) -> None:
        import openal  # noqa: F401  (import proves availability)
        self._openal = openal
        self._sources: dict[int, object] = {}
        self._decoded: dict[int, Path] = {}  # handle -> temp WAV to unlink
        self._next = 1
        self._lock = threading.Lock()
        openal.oalInit()

    def _decode_to_wav(self, path: Path) -> tuple[Optional[Path], bool]:
        """Returns ``(wav_path, is_temp)``; temp WAVs are unlinked when the
        source is released."""
        if path.suffix.lower() == ".wav":
            return path, False
        from ..tts import FFMPEG
        import os
        import subprocess
        import tempfile
        fd, name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        out = Path(name)
        try:
            subprocess.run([FFMPEG, "-y", "-i", str(path), str(out)],
                           check=True, capture_output=True)
            return out, True
        except Exception:
            logger.exception("ffmpeg decode failed for %s", path)
            out.unlink(missing_ok=True)
            return None, False

    def load(self, path: Path, *, looping: bool) -> Optional[int]:
        try:
            wav, is_temp = self._decode_to_wav(path)
            if wav is None:
                return None
            src = self._openal.oalOpen(str(wav))
            if looping:
                src.set_looping(True)
            with self._lock:
                handle = self._next
                self._next += 1
                self._sources[handle] = src
                if is_temp:
                    self._decoded[handle] = wav
            return handle
        except Exception:
            logger.exception("OpenAL load failed for %s", path)
            return None

    def play(self, handle): self._call(handle, "play")
    def pause(self, handle): self._call(handle, "pause")

    def stop(self, handle):
        self._call(handle, "stop")
        with self._lock:
            self._sources.pop(handle, None)
            wav = self._decoded.pop(handle, None)
        if wav is not None:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass

    def set_gain(self, handle, gain):
        with self._lock:
            src = self._sources.get(handle)
        if src is not None:
            try:
                src.set_gain(max(0.0, gain))
            except Exception:
                pass

    def set_position(self, handle, az, el, dist):
        import math
        with self._lock:
            src = self._sources.get(handle)
        if src is None:
            return
        rad = math.radians(az)
        try:
            src.set_position((math.sin(rad) * dist, el, -math.cos(rad) * dist))
        except Exception:
            pass

    def is_playing(self, handle) -> bool:
        with self._lock:
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
        with self._lock:
            sources = list(self._sources.values())
            decoded = list(self._decoded.values())
            self._sources.clear()
            self._decoded.clear()
        for src in sources:
            try:
                src.stop()
            except Exception:
                pass
        for wav in decoded:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self._openal.oalQuit()
        except Exception:
            pass

    def _call(self, handle, method):
        with self._lock:
            src = self._sources.get(handle)
        if src is not None:
            try:
                getattr(src, method)()
            except Exception:
                pass


def _classify_backend_failure(exc: Exception) -> str:
    """Which ``UNAVAILABLE_*`` code an error from starting OpenAL means.

    PyOpenAL's own error classes are matched by name. They cannot be
    imported to compare against: when the library is missing, importing
    the package they live in is the very thing that fails.
    """
    if isinstance(exc, ImportError):
        return UNAVAILABLE_NO_PACKAGE
    error_name = type(exc).__name__
    if error_name == "ExternalLibraryError":
        return UNAVAILABLE_NO_LIBRARY
    # Starting up only opens a device and makes a context on it, so an
    # error from either is about the sound output. PyOpenAL raises
    # ``OalError`` when the open returns nothing, and ``ALCError`` when
    # OpenAL reports an error code first (seen as ALC_INVALID_VALUE
    # with a driver that could not start).
    if error_name in _OPENAL_DEVICE_ERROR_NAMES:
        return UNAVAILABLE_NO_DEVICE
    return UNAVAILABLE_OTHER


_OPENAL_DEVICE_ERROR_NAMES = frozenset({"OalError", "ALCError"})


def _make_backend(announce: bool = True) -> _Backend:
    """Start OpenAL, or hand back a stand-in that remembers why not.

    ``announce`` False keeps the reason out of the log, for a caller
    that is about to report it in its own words.
    """
    # The catch is broad because this must never stop the reader from
    # opening: whatever goes wrong, the reader falls back to
    # screen-reader mode and the reason is kept for the user.
    try:
        return _OpenALBackend()
    except Exception as exc:  # noqa: BLE001 — see comment above
        reason = _classify_backend_failure(exc)
        if announce:
            # A package nobody installed is an ordinary state. The
            # others mean something is wrong with this install or this
            # machine.
            level = (
                logging.INFO if reason == UNAVAILABLE_NO_PACKAGE
                else logging.WARNING
            )
            logger.log(
                level,
                "Audio engine unavailable (%s); soundscape/live-TTS disabled",
                exc,
            )
        return _NullBackend(reason, str(exc))


def describe_unavailable(reason: Optional[str], detail: str = "") -> str:
    """Why the app voice and soundscapes cannot play, and what to do
    about it, in words for the user.

    The old message named two possible causes at once and told
    everyone to "install the playback feature". On the Windows
    download that feature was already installed and the fault was a
    missing file, so the advice led nowhere.
    """
    if reason == UNAVAILABLE_NO_PACKAGE:
        # Named the way the Optional Features window names it, so the
        # user can find the row by the words in this message.
        from ..optional_features import FEATURES
        feature_name = FEATURES["playback"]["display"]
        return (
            "The reader's audio feature is not installed. Install "
            f'"{feature_name}" from Edit > Optional Features, then '
            "restart Ficary."
        )
    if reason == UNAVAILABLE_NO_LIBRARY:
        if sys.platform == "win32":
            return (
                f"This copy of Ficary is missing its sound library "
                f"({_WINDOWS_OPENAL_DLL}). Updating Ficary, or unzipping "
                "a fresh copy of it, puts the file back."
            )
        if sys.platform == "darwin":
            return (
                "The OpenAL sound library could not be found on this Mac, "
                "so Ficary has nothing to play sound through."
            )
        return (
            "The OpenAL sound library is not installed on this computer. "
            "Install it with your package manager (on Debian or Ubuntu: "
            "sudo apt install libopenal1), then restart Ficary."
        )
    if reason == UNAVAILABLE_NO_DEVICE:
        return (
            "No sound output could be opened. Check that speakers or "
            "headphones are connected and switched on, then press Play "
            "again."
        )
    if detail:
        return f"The sound system could not start ({detail})."
    return "The sound system could not start."


class AudioProbe:
    """The result of :func:`probe_audio`: whether audio can play here,
    and when it cannot, why."""

    def __init__(self, available: bool, reason: Optional[str], detail: str) -> None:
        self.available = available
        self.reason = reason
        self.detail = detail

    @property
    def package_found(self) -> bool:
        return self.available or self.reason != UNAVAILABLE_NO_PACKAGE

    @property
    def library_found(self) -> bool:
        return self.available or self.reason not in (
            UNAVAILABLE_NO_PACKAGE, UNAVAILABLE_NO_LIBRARY,
        )

    @property
    def device_opened(self) -> bool:
        return self.available


def probe_audio() -> AudioProbe:
    """Start the audio backend, note what happened, and shut it down.

    For the command-line self-check. It does not touch the app's shared
    engine, so it is safe to call whether or not a reader is open.
    """
    backend = _make_backend(announce=False)
    probe = AudioProbe(
        backend.available,
        None if backend.available else (getattr(backend, "reason", None) or UNAVAILABLE_OTHER),
        "" if backend.available else getattr(backend, "detail", ""),
    )
    backend.shutdown()
    return probe


_engine: Optional[AudioEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> AudioEngine:
    """Process-wide singleton."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = AudioEngine()
        return _engine


def shutdown_engine() -> None:
    """Shut down the singleton if it was ever created. Safe to call at app
    exit whether or not audio was used."""
    global _engine
    with _engine_lock:
        engine, _engine = _engine, None
    if engine is not None:
        engine.shutdown()
