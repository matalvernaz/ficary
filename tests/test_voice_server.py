"""Voices from a voice server, the character pool keeping to the story's language, and the web
page's voice choices.

The provider is tested against a real HTTP server on this computer (the network guard in
conftest allows loopback), speaking the OpenAI speech API the way a voice server does.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ficary import tts, tts_providers
from ficary.tts_providers import server as server_provider

# A short stretch of a tone, long enough for ffmpeg to make an MP3 of.
SAMPLE_RATE = 11025
TONE_SAMPLES = 2205


def make_wav(samples: int = TONE_SAMPLES) -> bytes:
    """A 16-bit mono WAV with ``samples`` samples of a square wave (or none)."""
    data = b"".join(struct.pack("<h", 8000 if (i // 20) % 2 else -8000) for i in range(samples))
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


class FakeVoiceServer:
    """A voice server on 127.0.0.1 whose answers a test sets, recording what it was asked."""

    def __init__(self) -> None:
        self.voices_reply = (200, {"voices": []})
        self.speech_reply = (200, make_wav())
        self.requests: list[tuple[str, str, bytes]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, b""))
                if self.path == "/v1/audio/voices":
                    status, body = owner.voices_reply
                    self._send(status, json.dumps(body).encode(), "application/json")
                else:
                    self._send(404, b"{}", "application/json")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append(("POST", self.path, body))
                status, reply = owner.speech_reply
                if isinstance(reply, dict):
                    self._send(status, json.dumps(reply).encode(), "application/json")
                else:
                    self._send(status, reply, "audio/wav")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


ELOQUENCE_VOICES = [
    {"id": "eloquence-en-us-reed", "name": "Reed", "locale": "en-US", "gender": "Male", "group": "Eloquence"},
    {"id": "eloquence-en-us-shelley", "name": "Shelley", "locale": "en-US", "gender": "Female", "group": "Eloquence"},
    {"id": "eloquence-en-us-bobby", "name": "Bobby", "locale": "en-US", "gender": "Neutral", "group": "Eloquence"},
    {"id": "eloquence-de-de-reed", "name": "Reed", "locale": "de-DE", "gender": "Male", "group": "Eloquence"},
]


@pytest.fixture
def voice_server(monkeypatch):
    server = FakeVoiceServer()
    server.voices_reply = (200, {"voices": ELOQUENCE_VOICES})
    monkeypatch.setenv(server_provider.ADDRESS_VARIABLE, server.address)
    yield server
    server.close()


@pytest.fixture
def provider():
    return server_provider.ServerProvider()


# ── The provider ──────────────────────────────────────────────────


def test_off_until_an_address_is_set(monkeypatch, provider):
    monkeypatch.delenv(server_provider.ADDRESS_VARIABLE, raising=False)
    assert not provider.is_installed()
    assert provider.list_voices() == []


def test_an_address_that_is_not_http_is_ignored(monkeypatch, provider):
    monkeypatch.setenv(server_provider.ADDRESS_VARIABLE, "voices:8000")
    assert not provider.is_installed()


def test_registered_with_the_other_providers():
    assert "server" in tts_providers.all_provider_names()


def test_lists_the_servers_voices_with_language_gender_and_family(voice_server, provider):
    voices = provider.list_voices()
    assert [v.id for v in voices] == [
        "server:eloquence-en-us-reed", "server:eloquence-en-us-shelley",
        "server:eloquence-en-us-bobby", "server:eloquence-de-de-reed",
    ]
    reed = voices[0]
    assert (reed.provider, reed.short_name, reed.locale, reed.gender) == ("server", "eloquence-en-us-reed", "en-US", "Male")
    assert reed.group == "Eloquence"
    assert reed.display == "Reed (Eloquence)"
    assert tts_providers.group_of(reed) == "Eloquence"
    assert voices[2].gender == "Neutral"


def test_plain_names_and_other_field_names_are_understood(voice_server, provider):
    """OpenAI-style servers often list bare names, and some say "language" or "voice_id"."""
    voice_server.voices_reply = (200, {"voices": [
        "af_bella",
        {"voice_id": "x1", "language": "en_gb", "gender": "f", "engine": "Kokoro"},
        {"name": ""},  # names nothing: left out
        42,            # not a voice: left out
        "af_bella",    # listed twice: once is enough
    ]})
    voices = provider.list_voices()
    assert [v.short_name for v in voices] == ["af_bella", "x1"]
    assert voices[0].gender == "Neutral" and voices[0].locale == ""
    assert voices[0].group == "Voice server"
    assert (voices[1].locale, voices[1].gender, voices[1].group) == ("en-GB", "Female", "Kokoro")


def test_the_voice_list_is_kept_for_a_while(voice_server, provider):
    provider.list_voices()
    provider.list_voices()
    asked = [r for r in voice_server.requests if r[1] == "/v1/audio/voices"]
    assert len(asked) == 1


def test_a_server_without_a_voice_list_has_no_voices(voice_server, provider):
    voice_server.voices_reply = (404, {})
    assert provider.list_voices() == []


def test_a_server_that_cannot_be_reached_says_so(monkeypatch, provider):
    server = FakeVoiceServer()
    address = server.address
    server.close()  # nothing listens there now
    monkeypatch.setenv(server_provider.ADDRESS_VARIABLE, address)
    with pytest.raises(server_provider.VoiceServerUnreachable) as caught:
        provider.list_voices()
    assert address in str(caught.value)
    # Remembered for a moment, so a render does not wait it out once per voice.
    asked = []
    real_fetch = provider._fetch_voices
    monkeypatch.setattr(provider, "_fetch_voices", lambda where: asked.append(where) or real_fetch(where))
    with pytest.raises(server_provider.VoiceServerUnreachable):
        provider.list_voices()
    assert asked == []


def test_unreachable_counts_as_the_voice_being_out_and_is_explained(monkeypatch):
    error = server_provider.VoiceServerUnreachable("The voice server at http://voices:8000 could not be reached (refused).")
    assert tts_providers.voice_is_out(error)
    explained = tts_providers.explain_synthesis_error(error)
    assert "http://voices:8000" in explained
    assert "internet" not in explained


def test_speech_is_asked_for_the_openai_way(voice_server, provider, monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(server_provider, "_write_mp3", lambda audio, path: written.update(audio=audio, path=path))
    provider.synthesize(text="Hello there.", voice="eloquence-en-us-reed", output_path=tmp_path / "a.mp3", rate="+20%", pitch="+5Hz")
    method, path, body = voice_server.requests[-1]
    assert (method, path) == ("POST", "/v1/audio/speech")
    sent = json.loads(body)
    assert sent == {
        "model": server_provider.REQUEST_MODEL,
        "input": "Hello there.",
        "voice": "eloquence-en-us-reed",
        "response_format": "wav",
        "speed": 1.2,
    }
    assert written["audio"].startswith(b"RIFF")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_speech_arrives_as_an_mp3_through_the_dispatcher(voice_server, tmp_path):
    out = tmp_path / "piece.mp3"
    tts_providers.synthesize("server:eloquence-en-us-reed", "Hello there.", out)
    data = out.read_bytes()
    assert data[:3] == b"ID3" or data[0] == 0xFF
    assert not list(tmp_path.glob("*.server-audio")), "the server's audio is not left behind"


def test_a_server_that_stops_answering_part_way_is_unreachable(voice_server, provider, monkeypatch, tmp_path):
    """A read that times out after the connection was made is the voice being down too."""
    class StalledResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise TimeoutError("timed out")

    monkeypatch.setattr(server_provider.urllib.request, "urlopen", lambda *a, **k: StalledResponse())
    with pytest.raises(server_provider.VoiceServerUnreachable, match="did not answer in time"):
        provider.synthesize(text="Hi.", voice="eloquence-en-us-reed", output_path=tmp_path / "a.mp3")
    with pytest.raises(server_provider.VoiceServerUnreachable):
        provider.list_voices()


def test_what_the_server_says_is_wrong_is_passed_on(voice_server, provider, tmp_path):
    voice_server.speech_reply = (400, {"error": {"message": "No voice called 'x'", "type": "invalid_request_error"}})
    with pytest.raises(RuntimeError, match="No voice called 'x'"):
        provider.synthesize(text="Hi.", voice="x", output_path=tmp_path / "a.mp3")


def test_no_audio_is_a_failure_not_a_silent_piece(voice_server, provider, tmp_path):
    voice_server.speech_reply = (200, make_wav(0))
    with pytest.raises(RuntimeError, match="no audio"):
        provider.synthesize(text="Hi.", voice="eloquence-en-us-reed", output_path=tmp_path / "a.mp3")


@pytest.mark.parametrize("rate, speed", [
    (None, 1.0), ("+20%", 1.2), ("-10%", 0.9), ("+500%", 4.0), ("-95%", 0.25), ("odd", 1.0),
])
def test_rate_becomes_a_speed(rate, speed):
    assert server_provider.speed_from_rate(rate) == pytest.approx(speed)


# ── The character pool ────────────────────────────────────────────


def _voice(provider, short, locale, gender, group=""):
    return tts_providers.VoiceInfo(
        id=f"{provider}:{short}", provider=provider, short_name=short, locale=locale,
        gender=gender, display=short, group=group,
    )


class _CatalogProvider:
    def __init__(self, name, voices):
        self.name = name
        self._voices = voices

    def is_installed(self):
        return True

    def list_voices(self):
        return list(self._voices)


def _install(monkeypatch, providers: dict):
    monkeypatch.setattr(tts_providers, "_REGISTRY_BUILT", True)
    monkeypatch.setattr(tts_providers, "_REGISTRY", {
        name: _CatalogProvider(name, voices) for name, voices in providers.items()
    })


# Edge lists its voices by locale, so Afrikaans, Albanian and Amharic come before English.
EDGE_CATALOG = [
    _voice("edge", "af-ZA-WillemNeural", "af-ZA", "Male"),
    _voice("edge", "af-ZA-AdriNeural", "af-ZA", "Female"),
    _voice("edge", "sq-AL-IlirNeural", "sq-AL", "Male"),
    _voice("edge", "en-AU-WilliamNeural", "en-AU", "Male"),
    _voice("edge", "en-GB-RyanNeural", "en-GB", "Male"),
    _voice("edge", "en-GB-SoniaNeural", "en-GB", "Female"),
    _voice("edge", "en-US-AriaNeural", "en-US", "Female"),
    _voice("edge", "en-US-DavisNeural", "en-US", "Male"),
    _voice("edge", "en-US-GuyNeural", "en-US", "Male"),
    _voice("edge", "en-US-JennyNeural", "en-US", "Female"),
]

SERVER_CATALOG = [
    _voice("server", "eloquence-en-us-reed", "en-US", "Male", "Eloquence"),
    _voice("server", "eloquence-en-us-shelley", "en-US", "Female", "Eloquence"),
    _voice("server", "eloquence-en-us-rocko", "en-US", "Male", "Eloquence"),
    _voice("server", "eloquence-de-de-reed", "de-DE", "Male", "Eloquence"),
    _voice("server", "sam-sam", "en-US", "Neutral", "SAM"),
]


def _pool(**overrides):
    arguments = dict(
        characters=["Harry", "Hermione"],
        genders={"Harry": "male", "Hermione": "female"},
        profiles={}, accents={}, enabled_providers=None,
        narrator_voice="en-US-AriaNeural",
    )
    arguments.update(overrides)
    return tts._build_voice_pool(**arguments)


def test_characters_with_no_accent_get_the_storys_language(monkeypatch):
    """They used to get the catalog's first voices: Afrikaans, then Albanian."""
    _install(monkeypatch, {"edge": EDGE_CATALOG})
    pool = _pool()
    for voices in pool.values():
        assert voices
        assert all(v.split(":", 1)[1].startswith("en-") for v in voices), voices
    # The curated voices lead, in their own order: Guy before Ryan.
    assert pool["Harry"][:2] == ["edge:en-US-GuyNeural", "edge:en-GB-RyanNeural"]
    assert pool["Hermione"][:2] == ["edge:en-US-JennyNeural", "edge:en-GB-SoniaNeural"]
    assert "edge:en-US-AriaNeural" not in pool["Hermione"], "the narrator keeps her own voice"


def test_after_the_curated_voices_come_the_narrators_own_accent(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG})
    harry = _pool()["Harry"]
    # Davis (en-US, uncurated) comes before William (en-AU, uncurated).
    assert harry.index("edge:en-US-DavisNeural") < harry.index("edge:en-AU-WilliamNeural")


def test_an_accent_with_no_voice_falls_back_to_the_storys_language(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG})
    pool = _pool(characters=["Worf"], genders={"Worf": "male"}, accents={"Worf": "kl-KL"})
    assert pool["Worf"] and all(":en-" in v for v in pool["Worf"])


def test_a_chosen_accent_still_wins(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG})
    pool = _pool(characters=["Fleur"], genders={"Fleur": "male"}, accents={"Fleur": "sq-AL"})
    assert pool["Fleur"] == ["edge:sq-AL-IlirNeural"]


def test_the_story_language_follows_a_server_narrator(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG, "server": SERVER_CATALOG})
    locale_of = {v.id: v.locale for v in EDGE_CATALOG + SERVER_CATALOG}
    pool = _pool(narrator_voice="server:eloquence-en-us-reed")
    assert pool["Harry"] and all(locale_of[v].startswith("en-") for v in pool["Harry"])
    # A German narrator: the only German male voice is the narrator's own, so Harry falls back
    # to every male voice rather than to none.
    pool = _pool(narrator_voice="server:eloquence-de-de-reed")
    assert "server:eloquence-de-de-reed" not in pool["Harry"]
    assert {locale_of[v] for v in pool["Harry"]} >= {"en-US", "af-ZA"}


def test_characters_can_be_kept_to_the_narrators_engine(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG, "server": SERVER_CATALOG})
    eloquence = [v.id for v in SERVER_CATALOG if v.group == "Eloquence"]
    pool = _pool(narrator_voice="server:eloquence-en-us-reed", character_voices=eloquence)
    assert pool["Harry"] == ["server:eloquence-en-us-rocko"]
    assert pool["Hermione"] == ["server:eloquence-en-us-shelley"]


def test_inside_a_cast_a_missing_gender_takes_another_voice_of_the_cast(monkeypatch):
    """SAM has no male or female voice. Leaving Harry's pool empty would hand him an Edge voice."""
    _install(monkeypatch, {"edge": EDGE_CATALOG, "server": SERVER_CATALOG})
    sam_only = ["server:sam-sam"]
    pool = _pool(narrator_voice="edge:en-US-AriaNeural", character_voices=sam_only)
    assert pool["Harry"] == ["server:sam-sam"]
    # And a cast of nothing but the narrator gives characters the narrator's voice.
    pool = _pool(narrator_voice="server:sam-sam", character_voices=sam_only)
    assert pool["Harry"] == ["server:sam-sam"]


def test_one_voice_gives_every_character_the_narrators_voice(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG, "server": SERVER_CATALOG})
    pool = _pool(
        characters=["Harry", "Hermione", "Someone"],
        genders={"Harry": "male", "Hermione": "female", "Someone": "neutral"},
        narrator_voice="server:eloquence-en-us-reed",
        character_voices=["server:eloquence-en-us-reed"],
    )
    assert pool == {name: ["server:eloquence-en-us-reed"] for name in ("Harry", "Hermione", "Someone")}
    mapper = tts.VoiceMapper(None)
    mapper.set_voice_pool(pool)
    assert {mapper.assign(n, g) for n, g in (("Harry", "male"), ("Hermione", "female"), ("Someone", "neutral"))} == {
        "server:eloquence-en-us-reed",
    }
    # The default Edge narrator too, whom the mapper otherwise steers characters away from.
    pool = _pool(character_voices=["edge:en-US-AriaNeural"])
    mapper = tts.VoiceMapper(None)
    mapper.set_voice_pool(pool)
    assert mapper.assign("Harry", "male") == "edge:en-US-AriaNeural"


def test_one_voice_skips_attribution_on_the_page():
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    assert 'value="narrator"' in page
    start = page.index("if character_voices == web_voices.CHARACTER_VOICES_NARRATOR:")
    assert 'attribution, model_size = "builtin", ""' in page[start:start + 400]


def test_a_cast_from_every_engine_is_dealt_engine_by_engine(monkeypatch):
    _install(monkeypatch, {"edge": EDGE_CATALOG, "server": SERVER_CATALOG})
    harry = _pool(mix_groups=True)["Harry"]
    families = [tts_providers.group_of(tts_providers.voice_by_id(v)) for v in harry[:4]]
    assert families == ["Microsoft Edge", "Eloquence", "Microsoft Edge", "Eloquence"]
    # Without mixing, the first family voices everyone first.
    plain = _pool()["Harry"]
    assert [tts_providers.voice_by_id(v).provider for v in plain[:3]] == ["edge", "edge", "edge"]


def test_generate_audiobook_passes_the_cast_on():
    import inspect

    parameters = inspect.signature(tts.generate_audiobook).parameters
    assert parameters["character_voices"].default is None
    assert parameters["mix_character_engines"].default is False


# ── The web page's voice choices ──────────────────────────────────

WEB_VOICES_PATH = Path(__file__).resolve().parents[1] / "docker" / "web_voices.py"
_spec = importlib.util.spec_from_file_location("web_voices", WEB_VOICES_PATH)
web_voices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web_voices)


def test_voice_names_are_short():
    assert web_voices.voice_name(_voice("edge", "en-US-AndrewMultilingualNeural", "en-US", "Male")) == "Andrew Multilingual"
    assert web_voices.voice_name(_voice("edge", "en-US-AriaNeural", "en-US", "Female")) == "Aria"
    server_voice = tts_providers.VoiceInfo(
        id="server:x", provider="server", short_name="x", locale="en-US", gender="Male",
        display="Reed (Eloquence)", group="Eloquence",
    )
    assert web_voices.voice_name(server_voice) == "Reed"


def test_the_page_gets_each_voices_family():
    page = web_voices.voices_for_page(EDGE_CATALOG[:1] + SERVER_CATALOG[:1])
    assert page["default"] == "edge:en-US-AriaNeural"
    assert [v["group"] for v in page["voices"]] == ["Microsoft Edge", "Eloquence"]
    assert set(page["voices"][1]) == {"id", "name", "locale", "gender", "group"}


def test_a_sample_is_in_the_voices_own_language():
    german = _voice("server", "eloquence-de-de-reed", "de-DE", "Male", "Eloquence")
    assert web_voices.sample_text(german).startswith("Hallo")
    unknown = _voice("server", "x", "tlh", "Male", "Klingon")
    assert web_voices.sample_text(unknown).startswith("Hello")


def test_the_cast_choice():
    catalog = EDGE_CATALOG + SERVER_CATALOG
    cast, mixed, providers = web_voices.character_cast("server:eloquence-en-us-reed", "engine", catalog)
    assert sorted(cast) == sorted(v.id for v in SERVER_CATALOG if v.group == "Eloquence")
    assert mixed is False
    assert providers == ["server"], "one engine keeps the render, fallbacks too, on its provider"
    cast, mixed, providers = web_voices.character_cast("", "engine", catalog)
    assert cast == [v.id for v in EDGE_CATALOG], "the default narrator is Edge's, so the cast is Edge's"
    assert providers == ["edge"]
    assert web_voices.character_cast("server:eloquence-en-us-reed", "any", catalog) == (None, True, None)
    assert web_voices.character_cast("server:eloquence-en-us-reed", "narrator", catalog) == (
        ["server:eloquence-en-us-reed"], False, ["server"],
    )
    assert web_voices.character_cast("", "narrator", catalog) == (["edge:en-US-AriaNeural"], False, ["edge"])
    # A narrator the server no longer has leaves the cast open rather than empty.
    assert web_voices.character_cast("server:gone", "engine", catalog) == (None, False, ["server"])


def test_a_cast_kept_to_one_provider_never_falls_back_to_edge(monkeypatch):
    """What the web job passes for one engine: the fallback ladder may not reach Edge's default
    voice, so a voice server that stops answering stops the render instead of changing its voice."""
    tried = []

    async def fake_segment(segment, voice, path, speech_rate=0):
        tried.append(voice)
        raise server_provider.VoiceServerUnreachable("The voice server at http://voices:8000 could not be reached.")

    monkeypatch.setattr(tts, "_generate_segment_audio", fake_segment)
    monkeypatch.setattr(tts, "_TTS_RETRY_PAUSE_SECONDS", 0)
    import asyncio

    segment = tts.Segment("Hello there.", speaker="Harry")
    result = asyncio.run(tts._generate_with_semaphore(
        asyncio.Semaphore(1), segment, "server:eloquence-en-us-rocko", Path("/nonexistent/x.mp3"), 0, 1,
        narrator_voice="server:eloquence-en-us-reed", enabled_providers=["server"],
    ))
    assert result is None
    assert tried == ["server:eloquence-en-us-rocko", "server:eloquence-en-us-rocko", "server:eloquence-en-us-reed"]


def test_the_page_job_carries_the_provider_limit():
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    assert "tts_providers=only_providers" in page
    assert "enabled_tts_providers=job.tts_providers" in page


def test_a_cast_nobody_can_list_still_keeps_characters_off_edge(monkeypatch):
    """The voice server not answering when a queued job renders: the cast's voices are missing
    from the catalog, and characters get the narrator rather than the mapper's Edge voices."""
    _install(monkeypatch, {"edge": EDGE_CATALOG})
    pool = _pool(narrator_voice="server:eloquence-en-us-reed", character_voices=["server:eloquence-en-us-reed"])
    assert pool == {"Harry": ["server:eloquence-en-us-reed"], "Hermione": ["server:eloquence-en-us-reed"]}


def test_a_remembered_failure_is_a_new_exception_each_time(monkeypatch, provider):
    server = FakeVoiceServer()
    address = server.address
    server.close()
    monkeypatch.setenv(server_provider.ADDRESS_VARIABLE, address)
    raised = []
    for _ in range(2):
        with pytest.raises(server_provider.VoiceServerUnreachable) as caught:
            provider.list_voices()
        raised.append(caught.value)
    assert raised[0] is not raised[1]
    assert str(raised[0]) == str(raised[1])


def test_the_sample_slot_is_freed_even_if_no_folder_can_be_made():
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    start = page.index("def voice_sample(")
    body = page[start:page.index("\n@app.", start)]
    assert body.index("_SAMPLE_SLOTS.acquire") < body.index("try:") < body.index("tempfile.mkdtemp")
    assert "_SAMPLE_SLOTS.release()" in body[body.index("finally:"):]


def test_a_saved_one_voice_choice_is_put_back_before_the_voice_list_is_asked_for():
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    start = page.index("async function loadVoices()")
    body = page[start:page.index("\n}\n", start)]
    assert body.index("castSelect.value = savedCast") < body.index('fetch("/voices")')
    assert body.index("showCastChoice()") < body.index('fetch("/voices")')
    assert "voicesUnavailable(" in body


def test_the_page_offers_the_choices_and_sends_them():
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    assert 'name="narrator_voice"' in page
    assert 'name="character_voices"' in page
    for choice in web_voices.CHARACTER_VOICE_CHOICES:
        assert f'value="{choice}"' in page
    assert 'fetch("/voices")' in page
    assert 'fetch("/voices/sample"' in page
    # The engine list only steers the voice list; it is not sent.
    assert 'id="voice-engine"' in page and 'name="voice_engine"' not in page


def test_a_saved_narrator_that_is_missing_is_not_swapped_quietly():
    """Only the speech server down: the list still has Edge's voices, and starting on the default
    would send the story to Microsoft instead of the voice chosen."""
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    start = page.index("if (saved && !savedVoice) {")
    block = page[start:page.index("\n  }\n", start)]
    assert 'new Option("Choose a narrator", "")' in block
    assert "voiceSelect.required = true" in block
    assert "sampleProblem(" in block


def test_a_saved_narrator_is_never_dropped_by_starting_before_the_list_is_ready():
    """The narrator list is switched off (and sends nothing) while the voice list loads or after it
    failed, so starting then would give a saved narrator's story to the default voice."""
    page = (WEB_VOICES_PATH.parent / "app.py").read_text(encoding="utf-8")
    start = page.index('$("#audio-form").addEventListener("submit"')
    handler = page[start:start + 900]
    guard = handler.index('if (voicesState !== "ready" && remembered(NARRATOR_KEY))')
    assert guard < handler.index("new FormData(e.target)")
    loader = page[page.index("async function loadVoices()"):page.index("engineSelect.addEventListener")]
    assert loader.count('voicesState = "failed"') == 2 and 'voicesState = "ready"' in loader
