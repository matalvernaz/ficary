"""Voices from a voice server: any speech server on the network that speaks OpenAI's speech API.

A voice server is how a machine shares the voices it can make. The web version's server may have
engines installed that a story site never could (old formant synthesisers, screen reader voices,
a local neural model), and this provider lets Ficary use every one of them.

Set ``FICARY_VOICE_SERVER`` to the server's address, for example ``http://voices:8000``. Ficary
then asks it:

- ``GET /v1/audio/voices`` for the voice list. Each entry may be a plain name or an object with
  ``id``, ``name``, ``locale`` (or ``language``), ``gender`` and ``group`` (the engine family,
  shown beside the name and used to give characters voices from one engine). A voice without a
  gender is only given to characters whose gender is unknown.
- ``POST /v1/audio/speech`` with ``{"model", "input", "voice", "response_format": "wav",
  "speed"}`` for each piece of speech, exactly as OpenAI's clients send it, so servers built for
  those clients (Kokoro-FastAPI, LocalAI, openedai-speech and the like) work too.

The audio comes back as WAV and is turned into MP3 with ffmpeg, as Piper's is, because the
audiobook pipeline joins MP3 pieces. Pitch and volume changes are not part of the API and are
left out, as they are for Piper.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import VoiceInfo, voice_id

logger = logging.getLogger(__name__)

# The environment variable that holds the server's address.
ADDRESS_VARIABLE = "FICARY_VOICE_SERVER"

# The provider's name, and the prefix of every voice id it hands out: "server:eloquence-en-us-reed".
PROVIDER_NAME = "server"

# What the provider is called where people read it.
DISPLAY_NAME = "Voice server"

# How long the voice list is kept before it is asked for again, so a server that gains an engine
# shows it without a restart, and how long a failed attempt is remembered so a render does not
# wait out the timeout once per voice.
VOICE_LIST_KEEP_SECONDS = 300.0
FAILED_LIST_KEEP_SECONDS = 15.0

# How long the server may take to answer the voice list, and to make one piece of speech. A piece
# is at most about 2,000 characters; a slow old engine on a small server can take a while.
LIST_TIMEOUT_SECONDS = 10.0
SPEECH_TIMEOUT_SECONDS = 180.0

# The speed range OpenAI's API accepts, as a multiple of normal. A server that can go faster says
# so with "max_speed" in its voice list, and that is used instead.
SLOWEST_SPEED = 0.25
FASTEST_SPEED = 4.0

# The model name sent with each request. OpenAI's API requires one; voice servers ignore it or
# take any name.
REQUEST_MODEL = "tts-1"

# How long ffmpeg may take to turn one piece of speech into MP3.
CONVERT_TIMEOUT_SECONDS = 120

_GENDERS = {"male": "Male", "m": "Male", "female": "Female", "f": "Female"}


class VoiceServerUnreachable(ConnectionError):
    """The voice server could not be reached at all. A ConnectionError, so the audiobook render
    treats it as the voice being down rather than as one piece of text it could not say."""


def address() -> str:
    """The voice server's address with no trailing slash, or "" when none is set."""
    value = os.environ.get(ADDRESS_VARIABLE, "").strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        logger.warning("%s must start with http:// or https://; ignoring %r", ADDRESS_VARIABLE, value)
        return ""
    return value


def speed_from_rate(rate: str | None, fastest: float = FASTEST_SPEED) -> float:
    """An edge-style rate change ("+20%", "-10%") as a speed multiple (1.2, 0.9), no faster than
    ``fastest``."""
    if not rate:
        return 1.0
    try:
        percent = int(rate.strip().rstrip("%"))
    except ValueError:
        return 1.0
    return max(SLOWEST_SPEED, min(fastest, 1.0 + percent / 100.0))


def _normal_locale(value) -> str:
    """A locale written the way the rest of Ficary writes them: "en_us" -> "en-US"."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parts = value.strip().replace("_", "-").split("-")
    parts[0] = parts[0].lower()
    if len(parts) >= 2 and len(parts[1]) == 2:
        parts[1] = parts[1].upper()
    return "-".join(parts)


def _voice_from_entry(entry) -> VoiceInfo | None:
    """One entry of the server's voice list as a VoiceInfo, or None when it names no voice."""
    if isinstance(entry, str):
        entry = {"id": entry}
    if not isinstance(entry, dict):
        return None
    short = entry.get("id") or entry.get("voice_id") or entry.get("name")
    if not isinstance(short, str) or not short.strip():
        return None
    short = short.strip()
    name = entry.get("name") if isinstance(entry.get("name"), str) else short
    group = entry.get("group") or entry.get("engine") or ""
    group = group if isinstance(group, str) else ""
    gender = _GENDERS.get(str(entry.get("gender", "")).strip().lower(), "Neutral")
    description = entry.get("description") if isinstance(entry.get("description"), str) else ""
    return VoiceInfo(
        id=voice_id(PROVIDER_NAME, short),
        provider=PROVIDER_NAME,
        short_name=short,
        locale=_normal_locale(entry.get("locale") or entry.get("language")),
        gender=gender,
        display=f"{name} ({group})" if group else name,
        description=description,
        group=group or DISPLAY_NAME,
    )


def _error_message(error: urllib.error.HTTPError) -> str:
    """What the server said was wrong, from an OpenAI-style error body when it sent one."""
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        message = body.get("error", {}).get("message") or body.get("detail")
        if isinstance(message, str) and message:
            return message
    except (ValueError, AttributeError, OSError):
        pass
    return f"HTTP {error.code} {error.reason}"


def _plain_reason(reason) -> str:
    """Why a connection failed, in words rather than an errno: "[Errno 111] Connection refused"
    and the like mean nothing to someone choosing a voice."""
    if isinstance(reason, ConnectionRefusedError):
        return "nothing answered at that address"
    if isinstance(reason, socket.gaierror):
        return "its name could not be looked up"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "it did not answer in time"
    if isinstance(reason, OSError) and reason.strerror:
        return reason.strerror[0].lower() + reason.strerror[1:]
    return str(reason)


def _fetch(request: urllib.request.Request, timeout: float, where: str) -> bytes:
    """The body of the server's answer. A server that cannot be reached, or stops answering
    part-way through, is reported as VoiceServerUnreachable; an answer that is an HTTP error is
    raised as HTTPError for the caller to read."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — the address is the user's own setting
            return response.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as exc:
        raise VoiceServerUnreachable(
            f"The voice server at {where} could not be reached: {_plain_reason(exc.reason)}."
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise VoiceServerUnreachable(
            f"The voice server at {where} did not answer in time."
        ) from exc
    except OSError as exc:
        raise VoiceServerUnreachable(
            f"The voice server at {where} could not be reached: {_plain_reason(exc)}."
        ) from exc


class ServerProvider:
    """Voices from the voice server named by FICARY_VOICE_SERVER."""

    name = PROVIDER_NAME
    display_name = DISPLAY_NAME

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._voices: list[VoiceInfo] = []
        self._voices_from = ""
        self._voices_until = 0.0
        # The fastest speed the server said it makes; OpenAI's 4 until its voice list says more.
        self._max_speed = FASTEST_SPEED
        # What the last failed attempt said: its type and message, not the exception itself,
        # which would keep its traceback (and every request it touched) alive while remembered.
        self._failure: tuple[type, str] | None = None

    def max_speed(self) -> float:
        """The fastest speed the server makes, as a multiple of normal, from its last voice list.
        No request is made for it."""
        return self._max_speed

    def is_installed(self) -> bool:
        """True when an address is set. Whether the server answers is found out by list_voices,
        which this is called far too often to do."""
        return bool(address())

    def list_voices(self) -> list[VoiceInfo]:
        where = address()
        if not where:
            return []
        with self._lock:
            now = time.monotonic()
            if self._voices_from == where and now < self._voices_until:
                if self._failure is not None:
                    kind, message = self._failure
                    raise kind(message)
                return list(self._voices)
            try:
                voices = self._fetch_voices(where)
            except (VoiceServerUnreachable, RuntimeError) as exc:
                self._voices_from = where
                self._voices_until = time.monotonic() + FAILED_LIST_KEEP_SECONDS
                self._failure = (type(exc), str(exc))
                self._voices = []
                raise
            self._voices_from = where
            self._voices_until = now + VOICE_LIST_KEEP_SECONDS
            self._failure = None
            self._voices = voices
            return list(voices)

    def _fetch_voices(self, where: str) -> list[VoiceInfo]:
        request = urllib.request.Request(f"{where}/v1/audio/voices", headers={"Accept": "application/json"})
        try:
            body = json.loads(_fetch(request, LIST_TIMEOUT_SECONDS, where).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.warning("The voice server at %s has no voice list (GET /v1/audio/voices)", where)
                return []
            raise RuntimeError(f"The voice server's voice list failed: {_error_message(exc)}") from exc
        except ValueError as exc:
            raise RuntimeError("The voice server's voice list was not JSON.") from exc
        entries = body.get("voices") if isinstance(body, dict) else body
        if not isinstance(entries, list):
            raise RuntimeError("The voice server's voice list was not a list of voices.")
        advertised = body.get("max_speed") if isinstance(body, dict) else None
        if isinstance(advertised, (int, float)) and not isinstance(advertised, bool) and advertised > 1:
            self._max_speed = float(advertised)
        else:
            self._max_speed = FASTEST_SPEED
        voices: list[VoiceInfo] = []
        seen: set[str] = set()
        for entry in entries:
            voice = _voice_from_entry(entry)
            if voice is None or voice.id in seen:
                continue
            seen.add(voice.id)
            voices.append(voice)
        return voices

    def synthesize(
        self, *, text: str, voice: str, output_path: Path,
        rate: str | None = None,
        volume: str | None = None,  # not part of the API
        pitch: str | None = None,   # not part of the API
    ) -> None:
        where = address()
        if not where:
            raise RuntimeError(
                f"TTS provider {PROVIDER_NAME!r} is not installed: set {ADDRESS_VARIABLE} "
                "to the voice server's address"
            )
        payload = json.dumps({
            "model": REQUEST_MODEL,
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "speed": speed_from_rate(rate, self._max_speed),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{where}/v1/audio/speech",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            audio = _fetch(request, SPEECH_TIMEOUT_SECONDS, where)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"The voice server could not say this: {_error_message(exc)}") from exc
        if not audio or _wav_has_no_samples(audio):
            raise RuntimeError("The voice server sent back no audio for this text.")
        _write_mp3(audio, Path(output_path))


def _wav_has_no_samples(audio: bytes) -> bool:
    """True for a WAV whose data is empty. Anything that is not a WAV is left to ffmpeg."""
    if not (audio.startswith(b"RIFF") and audio[8:12] == b"WAVE"):
        return False
    at = 12
    while at + 8 <= len(audio):
        chunk, size = audio[at:at + 4], int.from_bytes(audio[at + 4:at + 8], "little")
        if chunk == b"data":
            return min(size, len(audio) - at - 8) <= 0
        at += 8 + size + (size % 2)
    return True


def _write_mp3(audio: bytes, output_path: Path) -> None:
    """The server's audio as an MP3 at ``output_path``. ffmpeg works out what it was sent, so a
    server that answers in another format than the WAV asked for still works."""
    from ..tts import _find_tool

    ffmpeg = _find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to use a voice server's voices.")
    source = output_path.with_suffix(".server-audio")
    try:
        source.write_bytes(audio)
        convert = subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(source),
                "-codec:a", "libmp3lame", "-qscale:a", "4",
                str(output_path),
            ],
            # ffmpeg otherwise inherits the terminal and can wait on it (see tts._run_silent).
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=CONVERT_TIMEOUT_SECONDS,
        )
        if convert.returncode != 0:
            raise RuntimeError(
                f"ffmpeg could not convert the voice server's audio (exit {convert.returncode}): "
                f"{(convert.stderr or '').strip()[-300:]}"
            )
    finally:
        try:
            source.unlink()
        except OSError:
            pass
