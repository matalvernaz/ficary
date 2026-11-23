"""Pluggable text-to-speech providers for audiobook synthesis.

Two providers ship in-tree:

- ``edge`` — Microsoft Edge Neural Voices via ``edge-tts``. Cloud TTS
  (the same backend powering Edge's "Read aloud"), no API key, broad
  English-locale coverage. This is the historical default and the one
  every existing voice map JSON file points at.
- ``piper`` — Rhasspy's Piper TTS, local ONNX inference. Ships nothing
  by default; voices are downloaded on first use into the portable
  ``piper_models/`` folder. Works fully offline once a model is in
  place.

A provider exposes:

    name:     str
    is_installed() -> bool
    list_voices() -> list[VoiceInfo]
    synthesize(text, voice_short_name, *, rate, volume, pitch, output_path)

The dispatcher namespaces every voice id as ``"<provider>:<short_name>"``
so a voice-map JSON entry survives a provider swap. For backwards
compatibility, bare names (no colon) are treated as ``edge:`` — every
voice map written before 2.2.0 still resolves correctly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceInfo:
    """A single TTS voice surfaced from a provider's catalog.

    ``id`` is the provider-namespaced form (``"edge:en-US-AvaNeural"``)
    used everywhere outside the provider module itself; ``short_name``
    is the bare per-provider id passed back into synthesize().
    """

    id: str
    provider: str
    short_name: str
    locale: str       # BCP-47-ish: "en-US", "en-GB", "fr-FR", ...
    gender: str       # "Male" / "Female" / "Neutral"
    display: str      # human-friendly label for UI
    description: str = ""

    @property
    def language(self) -> str:
        """Two-letter language tag derived from ``locale`` ('en' from 'en-US')."""
        return self.locale.split("-", 1)[0] if self.locale else ""


def voice_id(provider: str, short_name: str) -> str:
    """Build the canonical ``provider:short_name`` form."""
    return f"{provider}:{short_name}"


def parse_voice_id(value: str) -> tuple[str, str]:
    """Split a voice id into ``(provider, short_name)``.

    Bare values without a ``:`` separator are treated as edge voices —
    that's how every pre-2.2.0 voice map looks, and the legacy form
    must keep resolving so users don't lose their per-story mappings.
    """
    if not value:
        return ("edge", "")
    if ":" not in value:
        return ("edge", value)
    provider, short = value.split(":", 1)
    return (provider, short)


# ── Registry ───────────────────────────────────────────────────────


_REGISTRY: dict[str, "object"] = {}
_REGISTRY_BUILT = False


def _build_registry() -> None:
    """Lazy-import providers so the bare ``ficary`` import doesn't pull
    edge-tts or onnxruntime. Either provider is allowed to fail its
    import — the registry just records what's available, and callers
    fall back to whatever's left."""
    global _REGISTRY_BUILT
    if _REGISTRY_BUILT:
        return
    try:
        from . import edge as _edge_mod
        _REGISTRY["edge"] = _edge_mod.EdgeProvider()
    except Exception as exc:  # noqa: BLE001 — we want any import failure
        logger.warning("Edge TTS provider unavailable: %s", exc)
    try:
        from . import piper as _piper_mod
        _REGISTRY["piper"] = _piper_mod.PiperProvider()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Piper TTS provider unavailable: %s", exc)
    _REGISTRY_BUILT = True


def all_provider_names() -> list[str]:
    _build_registry()
    return list(_REGISTRY.keys())


def get_provider(name: str):
    _build_registry()
    return _REGISTRY.get(name)


def installed_provider_names() -> list[str]:
    _build_registry()
    return [n for n, p in _REGISTRY.items() if p.is_installed()]


def all_voices(*, providers: Iterable[str] | None = None) -> list[VoiceInfo]:
    """Aggregate the voice catalog across the requested providers
    (default: all installed providers). Voices with the same id across
    providers shouldn't happen because of the namespacing, but this
    function deduplicates by id anyway as a safety net."""
    _build_registry()
    names = list(providers) if providers is not None else installed_provider_names()
    seen: set[str] = set()
    out: list[VoiceInfo] = []
    for n in names:
        provider = _REGISTRY.get(n)
        if provider is None or not provider.is_installed():
            continue
        try:
            for v in provider.list_voices():
                if v.id in seen:
                    continue
                seen.add(v.id)
                out.append(v)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Provider %r list_voices failed: %s", n, exc)
    return out


def voice_by_id(value: str) -> VoiceInfo | None:
    """Resolve a voice-map entry to a VoiceInfo, returning None if the
    provider isn't installed or doesn't know the voice. Treats bare
    names as edge voices for backwards compat."""
    provider_name, short = parse_voice_id(value)
    provider = get_provider(provider_name)
    if provider is None or not provider.is_installed():
        return None
    try:
        for v in provider.list_voices():
            if v.short_name == short or v.id == value:
                return v
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_by_id lookup failed: %s", exc)
    return None


# Error class names that mean the request never got an answer: no
# network, no route, a name that will not resolve, a timeout. They are
# matched by name because the network library they come from is an
# optional dependency and may not be importable here.
_UNREACHABLE_ERROR_NAMES = frozenset({
    "ClientConnectorError",
    "ClientConnectorDNSError",
    "ClientOSError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "ConnectionTimeoutError",
    "gaierror",
})


def is_refusal(error: BaseException | None) -> bool:
    """True when the Edge voice service turned the connection away with
    a 401 or 403, which is what it does to a client it has stopped
    accepting. Every request will get the same answer."""
    if type(error).__name__ != "WSServerHandshakeError":
        return False
    text = str(error)
    return "403" in text or "401" in text


def is_unreachable(error: BaseException | None) -> bool:
    """True when the voice service could not be reached at all."""
    if error is None:
        return False
    return type(error).__name__ in _UNREACHABLE_ERROR_NAMES or isinstance(
        error, (TimeoutError, ConnectionError),
    )


def voice_is_out(error: BaseException | None) -> bool:
    """True when a failure says the voice itself is not working: the
    service refused the connection, could not be reached, or the
    provider is not installed.

    False for everything else, and in particular for the service
    answering with no audio. That is what it does with text a voice
    cannot speak (a chapter in a language the voice does not know, say),
    and a chapter of that is a chapter with gaps, not a voice that is
    down."""
    if error is None:
        return False
    if is_refusal(error) or is_unreachable(error):
        return True
    text = str(error)
    return isinstance(error, RuntimeError) and (
        "is not installed" in text or "is not registered" in text
    )


def explain_synthesis_error(
    error: BaseException | None,
    *,
    retry_hint: str = "then press Play again",
) -> str:
    """Why a voice could not turn text into speech, in words for the
    user, with what to do about it where that is known.

    ``retry_hint`` finishes the advice for a service that could not be
    reached. The reader's "press Play again" is the default; an
    audiobook render passes its own.

    The Edge voices go through a Microsoft service that from time to
    time stops accepting older versions of the client. It answers them
    with a 403 while the connection is being set up. Seen on
    2026-11-23: edge-tts 7.2.8 was refused, 7.3.1 was served.
    """
    if error is None:
        return "The voice produced no audio."
    name = type(error).__name__
    text = str(error)
    if name == "WSServerHandshakeError" and "403" in text:
        return (
            "Microsoft's Edge voice service refused the request (403). "
            "That usually means this copy of Ficary's Edge voice support "
            "has gone out of date, and updating Ficary normally fixes it. "
            "Piper voices run on this computer and are not affected."
        )
    if is_unreachable(error):
        return (
            "The voice service could not be reached. Check the internet "
            f"connection, {retry_hint}."
        )
    if name == "NoAudioReceived":
        return "The voice service sent back no audio for this text."
    return f"The voice reported: {text or name}"


def synthesize(
    voice: str, text: str, output_path: Path, *,
    rate: str | None = None,
    volume: str | None = None,
    pitch: str | None = None,
) -> None:
    """Dispatch a synthesize call to the voice's provider.

    Raises ``RuntimeError`` if the provider is unavailable. ``rate`` /
    ``volume`` / ``pitch`` are the same edge-tts-style strings the
    audiobook pipeline already produces (e.g. ``"+10%"``, ``"-5Hz"``);
    each provider adapts them to its own knobs internally."""
    provider_name, short = parse_voice_id(voice)
    provider = get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"TTS provider {provider_name!r} is not registered")
    if not provider.is_installed():
        raise RuntimeError(
            f"TTS provider {provider_name!r} is not installed — "
            f"voice {voice!r} cannot be synthesized"
        )
    provider.synthesize(
        text=text, voice=short, output_path=output_path,
        rate=rate, volume=volume, pitch=pitch,
    )
