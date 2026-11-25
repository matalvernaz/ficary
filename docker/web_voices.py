"""The voice choices on the web page: which voices there are, what they are called, what a sample
says, and which voices characters may be given. Kept apart from app.py, as web_jobs.py is, so it
can be tested without the web server's own packages.
"""
from __future__ import annotations

import re

# Where characters' voices come from: the narrator's engine only, every engine on the server
# (dealt out engine by engine), or nowhere, the narrator reading the whole book in one voice.
CHARACTER_VOICES_FROM_ENGINE = "engine"
CHARACTER_VOICES_FROM_ALL = "any"
CHARACTER_VOICES_NARRATOR = "narrator"
CHARACTER_VOICE_CHOICES = (CHARACTER_VOICES_FROM_ENGINE, CHARACTER_VOICES_FROM_ALL, CHARACTER_VOICES_NARRATOR)

# What a voice says when someone presses Listen, in the voice's own language where there is a
# sentence for it. "{name}" is the voice's name.
SAMPLE_SENTENCES = {
    "en": "Hello, I'm {name}. This is how a chapter sounds when I read it.",
    "de": "Hallo, ich bin {name}. So klingt ein Kapitel, wenn ich es vorlese.",
    "es": "Hola, soy {name}. Así suena un capítulo cuando lo leo.",
    "fr": "Bonjour, je suis {name}. Voici comment sonne un chapitre quand je le lis.",
    "it": "Ciao, sono {name}. Ecco come suona un capitolo quando lo leggo io.",
    "pt": "Olá, eu sou {name}. É assim que um capítulo soa quando eu o leio.",
    "fi": "Hei, olen {name}. Tältä luku kuulostaa, kun minä luen sen.",
    "pl": "Cześć, jestem {name}. Tak brzmi rozdział, kiedy go czytam.",
    "nl": "Hallo, ik ben {name}. Zo klinkt een hoofdstuk als ik het voorlees.",
    "ja": "こんにちは、{name}です。私が読むと、章はこのように聞こえます。",
}


def voice_name(voice) -> str:
    """A voice's own name, short enough for a list: "Aria" rather than Edge's "Microsoft Aria
    Online (Natural) - English (United States)", "Reed" rather than "Reed (Eloquence)"."""
    from ficary import tts_providers

    if voice.provider == "edge":
        # "en-US-AndrewMultilingualNeural" -> "Andrew Multilingual"
        last = voice.short_name.split("-", 2)[-1]
        last = re.sub(r"Neural$", "", last)
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", last) or voice.short_name
    suffix = f" ({tts_providers.group_of(voice)})"
    if voice.display.endswith(suffix):
        return voice.display[: -len(suffix)]
    return voice.display or voice.short_name


def available_voices() -> list:
    """Every voice the providers installed here can use. A provider that cannot list its voices
    (a voice server that is down) is left out with a log line rather than failing the page."""
    from ficary import tts_providers

    return tts_providers.all_voices()


def voices_for_page(voices: list) -> dict:
    """The voice list as the page's script reads it, and the voice it starts on."""
    from ficary import tts, tts_providers

    return {
        "voices": [
            {
                "id": voice.id,
                "name": voice_name(voice),
                "locale": voice.locale,
                "gender": voice.gender,
                "group": tts_providers.group_of(voice),
            }
            for voice in voices
        ],
        "default": tts._namespace_legacy(tts.NARRATOR_VOICE),
    }


def sample_rate(percent: int) -> str | None:
    """The form's speech rate as the rate a sample is spoken at, clamped as an audiobook clamps it,
    so the sample is as fast as the book's narration will be: 20 -> "+20%", 0 -> None."""
    from ficary import tts

    return tts._rate_str(tts._clamp_rate(percent))


def sample_text(voice) -> str:
    """What the voice says when Listen is pressed, in its own language where there is a sentence."""
    sentence = SAMPLE_SENTENCES.get(voice.language.lower(), SAMPLE_SENTENCES["en"])
    return sentence.format(name=voice_name(voice))


def character_cast(
    narrator: str, choice: str, voices: list,
) -> tuple[list[str] | None, bool, list[str] | None]:
    """For the form's choice: the voices characters may have, whether to deal them out engine by
    engine, and the only providers the render may use, fallbacks included. ``narrator`` is the
    chosen voice id, or "" for ficary's default.

    One engine or one voice keeps the whole render on the narrator's provider. Otherwise a voice
    server that stopped answering part-way through had every later line fall back to Edge's
    default voice: the book changed voice without a word, and its text went to Microsoft. Kept
    to the narrator's provider, the render stops and says the server could not be reached, and a
    line one voice cannot say falls back to the narrator, of the same engine. A cast from every
    engine already includes them all, so it keeps every provider."""
    from ficary import tts, tts_providers

    if choice == CHARACTER_VOICES_FROM_ALL:
        return None, True, None
    narrator_id = tts._namespace_legacy(narrator or tts.NARRATOR_VOICE)
    only_providers = [tts_providers.parse_voice_id(narrator_id)[0]]
    if choice == CHARACTER_VOICES_NARRATOR:
        # A cast of one: every character is given the narrator's voice.
        return [narrator_id], False, only_providers
    narrator_info = next((v for v in voices if v.id == narrator_id), None)
    if narrator_info is None:
        # The narrator's family cannot be told; leave the cast open rather than empty.
        return None, False, only_providers
    family = tts_providers.group_of(narrator_info)
    return [v.id for v in voices if tts_providers.group_of(v) == family], False, only_providers
