"""Regression tests for the 2026-09-08 audit's audio and reader findings.

Each test names the finding it pins. The audit's own probes reproduced
the defects; these assert the corrected behaviour so it cannot regress.
"""
from __future__ import annotations

import io
import smtplib
import ssl
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ficary import attribution, mailer, tts
from ficary.audio.engine import CHANNEL_AMBIENT, AudioEngine, _Backend
from ficary.audio.events import Event, ReaderEvent
from ficary.logging_utils import redact_headers, redact_url
from ficary.models import Chapter
from ficary.soundscape.model import Sound, Soundscape
from ficary.soundscape.session import SoundscapeSession
from ficary.tts_providers import piper


# ── CORE-11: diagnostics must not log session cookies ──────────────

def test_redact_headers_keeps_cookie_name_drops_value():
    out = redact_headers({
        "Set-Cookie": "session=SECRET_VALUE; Secure; HttpOnly",
        "Authorization": "Bearer SECRET",
        "Server": "cloudflare",
    })
    assert out["Set-Cookie"] == "session=<redacted>"
    assert "SECRET_VALUE" not in str(out)
    assert out["Authorization"] == "<redacted>"
    assert out["Server"] == "cloudflare"


def test_redact_url_masks_credential_parameters():
    out = redact_url("https://example.invalid/a?token=abc&page=2")
    assert "abc" not in out
    assert "page=2" in out
    assert out.startswith("https://example.invalid/a?")


def test_fetch_diagnostic_does_not_log_cookie_values(caplog):
    from ficary.scraper import BaseScraper

    scraper = BaseScraper.__new__(BaseScraper)
    scraper._browser = "chrome"
    resp = SimpleNamespace(
        headers={"Set-Cookie": "session=SECRET_VALUE; Secure"},
        status_code=200,
        text="",
    )
    sess = SimpleNamespace(cookies=SimpleNamespace(jar=[]))
    with caplog.at_level("DEBUG", logger="ficary.scraper"):
        BaseScraper._log_fetch_diagnostic(scraper, resp, sess, "probe", "https://x/y")
    assert "SECRET_VALUE" not in caplog.text
    assert "Set-Cookie" in caplog.text


# ── AUD-A01: SMTP must verify the relay certificate ────────────────

def test_smtp_context_verifies_certificate_and_hostname():
    ctx = mailer._tls_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED


def test_send_file_passes_a_verifying_context(tmp_path, monkeypatch):
    attachment = tmp_path / "book.epub"
    attachment.write_bytes(b"epub")
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            seen["ssl_context"] = context

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            seen["sent"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mailer, "_config", lambda prefs=None: {
        "host": "smtp.invalid", "port": mailer.SMTP_SSL_PORT,
        "user": "u", "password": "p", "from_addr": "a@b.invalid",
    })
    mailer.send_file("to@x.invalid", attachment)
    assert seen["sent"] is True
    assert seen["ssl_context"].check_hostname is True
    assert seen["ssl_context"].verify_mode is ssl.CERT_REQUIRED


# ── AUD-A03: local-only synthesis must not reach a cloud voice ─────

def test_voice_allowed_respects_enabled_providers():
    assert tts._voice_allowed("edge:en-US-AriaNeural", None) is True
    assert tts._voice_allowed("edge:en-US-AriaNeural", ["piper"]) is False
    assert tts._voice_allowed("piper:en_US-amy-medium", ["piper"]) is True


def test_segment_fallback_never_uses_a_disabled_provider():
    import asyncio

    tried = []

    async def fake_synth(seg, voice, path, **kw):
        tried.append(voice)
        return False

    async def run():
        sem = asyncio.Semaphore(1)
        with patch.object(tts, "_generate_segment_audio", fake_synth), \
             patch.object(tts.asyncio, "sleep", side_effect=lambda *_: asyncio.sleep(0)):
            return await tts._generate_with_semaphore(
                sem, tts.Segment("Hello there.", speaker="A"),
                "piper:en_US-amy-medium", Path("/tmp/unused.mp3"), 0, 1,
                narrator_voice="piper:en_US-amy-medium",
                enabled_providers=["piper"],
            )

    assert asyncio.run(run()) is None
    assert tried, "the allowed local voice should still be attempted"
    assert all(v.startswith("piper:") for v in tried)


# ── AUD-A06: incomplete chapters must not be cached as complete ────

def _incomplete_render(tmp_path, lost_text="LOST sentence that is long enough."):
    cache = tmp_path / "cache"; cache.mkdir()
    build = tmp_path / "build"; build.mkdir()
    segments = [
        tts.Segment("Successful sentence that is long enough to stand alone.", speaker="A"),
        tts.Segment(lost_text, speaker="B"),
    ]
    story = SimpleNamespace(
        id="t", title="T", author="A", url="https://example.invalid",
        metadata={}, chapters=[Chapter(number=1, title="One", html="<p>x</p>")],
    )
    calls = []

    async def synth(seg, voice, path, **kw):
        calls.append(seg.text)
        if seg.text.startswith("LOST"):
            return False
        path.write_bytes(seg.text.encode())
        return True

    async def heading(*a, **k):
        return False

    def concat(cmd, **kw):
        source = Path(cmd[cmd.index("-i") + 1])
        inputs = [Path(line[len("file '"):-1]) for line in source.read_text().splitlines()]
        Path(cmd[-1]).write_bytes(b"|".join(p.read_bytes() for p in inputs))
        return SimpleNamespace(returncode=0, stderr="")

    def mux(chapters, story, out, *a, **kw):
        out.write_bytes(b"|".join(p.read_bytes() for p, _ in chapters))

    options = dict(
        story=story, output_dir=tmp_path, build_tmp=build, cache_root=cache,
        mapper=tts.VoiceMapper(), narrator=tts.NARRATOR_VOICE, speech_rate=0,
        progress_callback=None, all_segments=[segments],
    )
    patches = (
        patch.object(tts, "_generate_segment_audio", synth),
        patch.object(tts, "_make_silence_clip", return_value=None),
        patch.object(tts, "_run_silent", concat),
        patch.object(tts, "_synthesize_heading", heading),
        patch.object(tts, "build_m4b", mux),
    )
    return options, calls, cache, patches


def test_partial_chapter_is_not_cached_and_retries_next_render(tmp_path):
    options, calls, cache, patches = _incomplete_render(tmp_path)
    reports = []
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        tts._generate_audiobook_inner(**options, incomplete_callback=lambda msg, rows: reports.append(msg))
        first = len(calls)
        assert not list(cache.glob("*.mp3")), "a partial body must stay out of the cache"
        tts._generate_audiobook_inner(**options)
        assert len(calls) > first, "the retry must re-attempt the failed segment"
    assert reports and "missing speech" in reports[0]


def test_complete_chapter_still_caches(tmp_path):
    options, calls, cache, patches = _incomplete_render(tmp_path, lost_text="Fine sentence that is long enough.")
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        tts._generate_audiobook_inner(**options)
        assert list(cache.glob("*.mp3")), "a complete chapter must still be cached"
        first = len(calls)
        tts._generate_audiobook_inner(**options)
        assert len(calls) == first, "a cached complete chapter must not re-synthesise"


# ── AUD-A07: cancellation must abort the tail of a render ──────────

def test_cancel_during_last_heading_aborts_instead_of_returning_a_book(tmp_path):
    options, _calls, _cache, patches = _incomplete_render(tmp_path, lost_text="Fine sentence that is long enough.")
    cancel = threading.Event()

    async def cancel_heading(*a, **k):
        cancel.set()
        return False

    with patches[0], patches[1], patches[2], patches[4], \
         patch.object(tts, "_synthesize_heading", cancel_heading):
        with pytest.raises(tts.AudiobookCancelled):
            tts._generate_audiobook_inner(**options, cancel_event=cancel)


# ── AUD-A08: a stale soundscape build must not stop the new one ────

class _BlockingBackend(_Backend):
    available = True

    def __init__(self):
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.n = 0
        self.stopped = []

    def load(self, path, *, looping):
        with self.lock:
            self.n += 1
            handle = self.n
        if handle == 1:
            self.blocked.set()
            assert self.release.wait(3)
        return handle

    def stop(self, handle):
        self.stopped.append(handle)


def test_obsolete_soundscape_build_stops_only_its_own_handles():
    backend = _BlockingBackend()
    engine = AudioEngine(backend)
    old = Soundscape("Old", [Sound("old.wav")])
    new = Soundscape("New", [Sound("new.wav")])
    with patch("ficary.soundscape.session.library.resolve_source",
               side_effect=lambda source: Path(source)):
        session = SoundscapeSession(engine, old)
        session._on_event(Event(ReaderEvent.READER_OPENED, story_key="k"))
        assert backend.blocked.wait(3)
        stale = session._build_thread
        session.set_soundscape(new)
        session._join_build()
        live = list(engine._channels[CHANNEL_AMBIENT].handles)
        backend.release.set()
        stale.join(3)
        assert list(engine._channels[CHANNEL_AMBIENT].handles) == live
        assert backend.stopped == [1]
        session.close()
        engine.shutdown()


# ── AUD-A09: a failed LLM run must be visible to its caller ────────

def test_llm_failure_key_matches_between_dispatcher_and_caller():
    cfg = {"provider": "ollama", "model": "audit"}
    attribution.clear_failures()
    segs = [tts.Segment('"Hi."', speaker=None)]
    with patch.object(attribution, "_refine_with_llm", side_effect=RuntimeError("down")):
        attribution.refine_speakers(segs, '"Hi."', backend="llm", llm_config=cfg)
    assert attribution.has_failed("llm", None, llm_config=cfg) is True
    attribution.clear_failures("llm", None, llm_config=cfg)
    assert attribution.has_failed("llm", None, llm_config=cfg) is False


# ── AUD-A02: Piper must install from a nested archive ──────────────

def _nested_piper_tar(dest: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in (
            ("piper/piper", b"#!/bin/sh\nexit 0\n", 0o755),
            ("piper/libexample.so", b"lib", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
    dest.write_bytes(buf.getvalue())


def test_nested_unix_archive_installs_an_executable_file(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    archive = tmp_path / "mock.tar.gz"
    _nested_piper_tar(archive)

    class FakeResp:
        def __init__(self, data):
            self._d = data

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    logs = []
    with patch.object(piper, "piper_binary_dir", return_value=root), \
         patch.object(piper, "_piper_release_asset", return_value=("mock.tar.gz", "tar")), \
         patch("urllib.request.urlopen", lambda *a, **k: FakeResp(archive.read_bytes())), \
         patch("shutil.which", return_value=None):
        assert piper.install_piper_binary(log_callback=logs.append) is True
        exe = piper.piper_executable()
        assert exe is not None and Path(exe).is_file()
        assert (root / "libexample.so").is_file()
        assert not (root / ".staging").exists()


def test_a_directory_is_never_reported_as_the_piper_binary(tmp_path):
    root = tmp_path / "install"
    (root / "piper").mkdir(parents=True)
    with patch.object(piper, "piper_binary_dir", return_value=root), \
         patch("shutil.which", return_value=None):
        assert piper.piper_executable() is None


# ── AUD-A11: one render's cleanup must not clear another's cancel ──

def test_concurrent_renders_keep_their_own_cancel_events():
    from ficary.gui import MainFrame

    frame = MagicMock()
    frame._render_cancel = None
    frame._render_cancels = []
    frame._render_cancel_lock = threading.Lock()
    frame._log = lambda *a, **k: None
    frame._resolve_output_dir.return_value = "/tmp/unused"
    frame.format_ctrl.GetString.return_value = "audio"

    params = SimpleNamespace(
        fmt="audio", audio_backend="builtin", audio_size=None, speech_rate=0,
        attribution_llm_config=None, llm_render_config=None,
        llm_strip_notes=False, enabled_tts_providers=(), strip_notes=False,
        hr_as_stars=False, chapter_notes="keep", send_to_abs=False,
        raw_output_dir="/tmp/unused", filename_template="{title}",
    )
    starts = {"A": threading.Event(), "B": threading.Event()}
    releases = {"A": threading.Event(), "B": threading.Event()}
    events = {}

    def render(story, *a, **kwargs):
        events[story.id] = kwargs["cancel_event"]
        starts[story.id].set()
        assert releases[story.id].wait(3)
        return None

    with patch.object(tts, "generate_audiobook", render), \
         patch("ficary.gui.wx.CallAfter", side_effect=lambda cb, *a, **k: cb(*a, **k)):
        a = threading.Thread(target=MainFrame._export_story,
                             args=(frame, SimpleNamespace(id="A"), params))
        b = threading.Thread(target=MainFrame._export_story,
                             args=(frame, SimpleNamespace(id="B"), params))
        a.start()
        assert starts["A"].wait(3)
        b.start()
        assert starts["B"].wait(3)
        releases["A"].set()
        a.join(3)
        assert frame._render_cancels == [events["B"]]
        MainFrame._on_cancel_render(frame, None)
        assert events["B"].is_set()
        releases["B"].set()
        b.join(3)
        assert frame._render_cancels == []


# ── AUD-A10: the four LLM setting combinations are independent ─────

@pytest.mark.parametrize(
    "strip,llm_strip,backend,want_an,want_attr",
    [
        (False, False, "llm", False, True),
        (True, True, "builtin", True, False),
        (True, True, "llm", True, True),
        (False, False, "builtin", False, False),
    ],
)
def test_llm_attribution_and_note_stripping_snapshot_independently(
    strip, llm_strip, backend, want_an, want_attr,
):
    from ficary.gui import MainFrame

    frame = MagicMock()
    frame.format_ctrl.GetString.return_value = "audio"
    frame.output_ctrl.GetValue.return_value = "/tmp/unused"
    frame.name_ctrl.GetValue.return_value = "{title}"
    frame.strip_notes_ctrl.GetValue.return_value = strip
    frame.llm_strip_notes_ctrl.GetValue.return_value = llm_strip
    frame.hr_stars_ctrl.GetValue.return_value = False
    frame._selected_attribution_backend.return_value = backend
    frame._selected_size.return_value = None
    frame._llm_config_for_render.return_value = {"provider": "ollama", "model": "m"}
    frame._enabled_tts_providers.return_value = ["piper"]
    frame.speech_rate_ctrl.GetValue.return_value = 0
    frame.abs_send_ctrl.GetValue.return_value = False
    frame.prefs.get.return_value = ""
    frame.prefs.get_bool.return_value = False

    params = MainFrame._snapshot_download_params(frame)
    assert (params.llm_render_config is not None) is want_an
    assert (params.attribution_llm_config is not None) is want_attr


def test_llm_note_stripping_works_without_llm_attribution(monkeypatch):
    """``generate_audiobook`` must honour an A/N config on its own."""
    seen = []
    monkeypatch.setattr(
        tts, "_llm_strip_an_paragraphs",
        lambda text, cfg: seen.append(cfg) or text,
    )
    story = SimpleNamespace(
        id="t", title="T", author="A", url="https://example.invalid",
        metadata={}, chapters=[Chapter(number=1, title="One", html="<p>Body text here.</p>")],
    )
    cfg = {"provider": "ollama", "model": "m"}
    monkeypatch.setattr(tts, "_check_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(tts, "_generate_audiobook_inner", lambda **kw: Path("/tmp/x.m4b"))
    tts.generate_audiobook(
        story, "/tmp", strip_notes=True, attribution_backend="builtin",
        llm_an_config=cfg,
    )
    assert seen == [cfg]
