"""Tests for how Ficary finds out, and says, whether the in-app reader can play sound.

They exist because the Windows download shipped the PyOpenAL package without the OpenAL library it loads (soft_oal_64.dll), so the reader's app voice and soundscapes could not play, and the message about it told people to install a feature that was already installed. The cause was invisible to the test suite: the engine's tests use a stand-in backend, and nothing looked at what a release build actually contains.

The error classes below are stand-ins for PyOpenAL's own, matched by name the way the engine matches them. Their names and the errors they are raised for were read from the PyOpenAL 0.7.11a1 wheel and then seen for real on Linux: a missing library raises ExternalLibraryError from the package's import, and a sound device that will not open raises ALCError ("ALC_INVALID_VALUE") or OalError.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ficary import cli, optional_features
from ficary.audio import engine

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


class ExternalLibraryError(Exception):
    """Stand-in for openal.library_loader.ExternalLibraryError."""


class OalError(Exception):
    """Stand-in for openal.OalError."""


class ALCError(Exception):
    """Stand-in for openal.alc.ALCError."""


LIBRARY_MISSING = ExternalLibraryError("OpenAL library couldn't be found")
DEVICE_RETURNED_NOTHING = OalError("Default OpenAL device couldn't be opened")
DEVICE_ERROR_CODE = ALCError("ALC_INVALID_VALUE")
PACKAGE_MISSING = ModuleNotFoundError("No module named 'openal'")


def make_backend_fail_with(monkeypatch, error: Exception) -> None:
    """Make starting OpenAL fail the way the real package fails."""

    def refuse():
        raise error

    monkeypatch.setattr(engine, "_OpenALBackend", refuse)


class WorkingBackend(engine._Backend):
    """A backend that started, for tests of what happens after a retry."""

    available = True

    def __init__(self):
        self.shut_down = False

    def shutdown(self):
        self.shut_down = True


# ── Telling the causes apart ─────────────────────────────────────


class TestWhyAudioCouldNotStart:
    @pytest.mark.parametrize(
        "error, reason",
        [
            (PACKAGE_MISSING, engine.UNAVAILABLE_NO_PACKAGE),
            (ImportError("DLL load failed while importing openal"), engine.UNAVAILABLE_NO_PACKAGE),
            (LIBRARY_MISSING, engine.UNAVAILABLE_NO_LIBRARY),
            (DEVICE_RETURNED_NOTHING, engine.UNAVAILABLE_NO_DEVICE),
            (DEVICE_ERROR_CODE, engine.UNAVAILABLE_NO_DEVICE),
            (RuntimeError("something else"), engine.UNAVAILABLE_OTHER),
        ],
    )
    def test_each_failure_is_given_its_own_reason(self, monkeypatch, error, reason):
        make_backend_fail_with(monkeypatch, error)
        audio = engine.AudioEngine()
        assert audio.available is False
        assert audio.unavailable_reason == reason
        assert audio.unavailable_detail == str(error)

    def test_a_working_engine_has_no_reason(self):
        audio = engine.AudioEngine(backend=WorkingBackend())
        assert audio.available is True
        assert audio.unavailable_reason is None
        assert audio.unavailable_detail == ""

    def test_a_missing_package_is_logged_quietly(self, monkeypatch, caplog):
        """Not having installed an optional feature is an ordinary state, not a fault."""
        make_backend_fail_with(monkeypatch, PACKAGE_MISSING)
        with caplog.at_level(logging.DEBUG, logger="ficary.audio.engine"):
            engine.AudioEngine()
        assert [record.levelno for record in caplog.records] == [logging.INFO]

    @pytest.mark.parametrize(
        "error", [LIBRARY_MISSING, DEVICE_RETURNED_NOTHING, DEVICE_ERROR_CODE],
    )
    def test_a_broken_install_or_device_is_logged_as_a_warning(
        self, monkeypatch, caplog, error,
    ):
        make_backend_fail_with(monkeypatch, error)
        with caplog.at_level(logging.DEBUG, logger="ficary.audio.engine"):
            engine.AudioEngine()
        assert [record.levelno for record in caplog.records] == [logging.WARNING]
        assert str(error) in caplog.text


# ── Saying it to the user ────────────────────────────────────────


class TestExplanation:
    def test_missing_package_names_the_feature_as_its_window_does(self):
        text = engine.describe_unavailable(engine.UNAVAILABLE_NO_PACKAGE)
        assert optional_features.FEATURES["playback"]["display"] in text
        assert "Optional Features" in text

    def test_missing_library_on_windows_does_not_send_people_to_install_anything(
        self, monkeypatch,
    ):
        """On the Windows download the package is always installed. Telling the user to install it led nowhere."""
        monkeypatch.setattr(engine.sys, "platform", "win32")
        text = engine.describe_unavailable(engine.UNAVAILABLE_NO_LIBRARY)
        assert "soft_oal_64.dll" in text
        assert "Optional Features" not in text
        assert "Install" not in text

    def test_missing_library_on_linux_names_the_system_package(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "linux")
        text = engine.describe_unavailable(engine.UNAVAILABLE_NO_LIBRARY)
        assert "libopenal1" in text

    def test_missing_library_on_a_mac_does_not_mention_other_systems(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "darwin")
        text = engine.describe_unavailable(engine.UNAVAILABLE_NO_LIBRARY)
        assert "Mac" in text
        assert "apt" not in text
        assert ".dll" not in text

    def test_no_sound_device_points_at_the_device(self):
        text = engine.describe_unavailable(engine.UNAVAILABLE_NO_DEVICE)
        assert "speakers or headphones" in text
        assert "Optional Features" not in text

    def test_an_unknown_failure_shows_its_own_text(self):
        text = engine.describe_unavailable(engine.UNAVAILABLE_OTHER, "boom")
        assert "boom" in text

    def test_no_reason_at_all_still_says_something(self):
        assert engine.describe_unavailable(None)

    @pytest.mark.parametrize(
        "reason",
        [
            engine.UNAVAILABLE_NO_PACKAGE, engine.UNAVAILABLE_NO_LIBRARY,
            engine.UNAVAILABLE_NO_DEVICE, engine.UNAVAILABLE_OTHER,
        ],
    )
    def test_no_explanation_blames_two_things_at_once(self, reason):
        """The old message said "PyOpenAL or an audio device is missing", which names the cause without knowing it."""
        text = engine.describe_unavailable(reason, "detail")
        assert "PyOpenAL or" not in text


# ── Trying again ─────────────────────────────────────────────────


class TestRetry:
    def test_audio_that_was_not_ready_at_first_is_picked_up_later(self, monkeypatch):
        """The engine is made once, when the first reader opens. Headphones connected after that used to need a restart of the app."""
        make_backend_fail_with(monkeypatch, DEVICE_RETURNED_NOTHING)
        audio = engine.AudioEngine()
        assert audio.available is False

        monkeypatch.setattr(engine, "_OpenALBackend", WorkingBackend)
        assert audio.retry_backend() is True
        assert audio.available is True
        assert audio.unavailable_reason is None

    def test_a_retry_that_still_fails_keeps_the_newest_reason(self, monkeypatch):
        make_backend_fail_with(monkeypatch, PACKAGE_MISSING)
        audio = engine.AudioEngine()
        make_backend_fail_with(monkeypatch, DEVICE_ERROR_CODE)
        assert audio.retry_backend() is False
        assert audio.unavailable_reason == engine.UNAVAILABLE_NO_DEVICE

    def test_a_working_backend_is_left_alone(self, monkeypatch):
        working = WorkingBackend()
        audio = engine.AudioEngine(backend=working)

        def must_not_be_called():
            raise AssertionError("a working backend must not be replaced")

        monkeypatch.setattr(engine, "_OpenALBackend", must_not_be_called)
        assert audio.retry_backend() is True
        assert audio._backend is working


# ── The reader's own message ─────────────────────────────────────


class TestReaderMessage:
    def press_play(self, monkeypatch, audio):
        """Press Play in App voice mode on a reader whose audio is ``audio``, and return the message boxes shown."""
        wx = pytest.importorskip("wx")
        from ficary.reader import gui as reader_gui

        shown: list[str] = []
        monkeypatch.setattr(
            wx, "MessageBox", lambda message, *args, **kwargs: shown.append(message),
        )
        started: list = []
        reader = SimpleNamespace(
            mode=SimpleNamespace(GetSelection=lambda: 1),
            _live=None,
            _paused=False,
            _engine=audio,
            _resolving_voice=False,
            _new_playback_gen=lambda: 1,
            _resolve_voice_and_play=lambda generation: started.append(generation),
        )
        monkeypatch.setattr(
            reader_gui.threading, "Thread",
            lambda target, args=(), **kwargs: SimpleNamespace(start=lambda: target(*args)),
        )
        reader_gui.ReaderFrame._on_play_pause(reader, None)
        return shown, started

    def test_says_what_is_actually_missing(self, monkeypatch):
        monkeypatch.setattr(engine.sys, "platform", "win32")
        make_backend_fail_with(monkeypatch, LIBRARY_MISSING)
        shown, started = self.press_play(monkeypatch, engine.AudioEngine())
        assert len(shown) == 1
        assert "soft_oal_64.dll" in shown[0]
        assert "Screen reader mode still works" in shown[0]
        assert started == []

    def test_tries_again_before_complaining(self, monkeypatch):
        make_backend_fail_with(monkeypatch, DEVICE_RETURNED_NOTHING)
        audio = engine.AudioEngine()
        monkeypatch.setattr(engine, "_OpenALBackend", WorkingBackend)
        shown, started = self.press_play(monkeypatch, audio)
        assert shown == []
        assert started == [1]


# ── The command-line self-check ──────────────────────────────────


class TestSelfCheck:
    def run_check(self, monkeypatch, capsys, error):
        if error is None:
            monkeypatch.setattr(engine, "_OpenALBackend", WorkingBackend)
        else:
            make_backend_fail_with(monkeypatch, error)
        status = cli._handle_check_reader_audio()
        return status, capsys.readouterr().out

    def test_all_well(self, monkeypatch, capsys):
        status, out = self.run_check(monkeypatch, capsys, None)
        assert status == 0
        assert "PyOpenAL package: found" in out
        assert "OpenAL sound library: found" in out
        assert "Sound output: opened" in out
        assert "can play" in out

    def test_library_missing_is_a_failure(self, monkeypatch, capsys):
        status, out = self.run_check(monkeypatch, capsys, LIBRARY_MISSING)
        assert status == 1
        assert "PyOpenAL package: found" in out
        assert "OpenAL sound library: NOT found" in out
        # Nothing is known about the sound device if the library never loaded.
        assert "Sound output" not in out

    def test_package_missing_is_a_failure(self, monkeypatch, capsys):
        status, out = self.run_check(monkeypatch, capsys, PACKAGE_MISSING)
        assert status == 1
        assert "PyOpenAL package: NOT found" in out
        assert "OpenAL sound library" not in out

    @pytest.mark.parametrize("error", [DEVICE_RETURNED_NOTHING, DEVICE_ERROR_CODE])
    def test_no_sound_device_has_its_own_exit_status(self, monkeypatch, capsys, error):
        """A build server has no sound device. It still needs to tell "the library is in the bundle" from "it is missing"."""
        status, out = self.run_check(monkeypatch, capsys, error)
        assert status == 2
        assert "OpenAL sound library: found" in out
        assert "Sound output: could NOT be opened" in out

    def test_the_check_does_not_also_log_the_failure(self, monkeypatch, capsys, caplog):
        """The check says it in its own words; the engine's log line on top would say it twice."""
        with caplog.at_level(logging.DEBUG, logger="ficary.audio.engine"):
            self.run_check(monkeypatch, capsys, LIBRARY_MISSING)
        assert caplog.records == []

    def test_the_check_shuts_down_what_it_started(self, monkeypatch):
        started: list[WorkingBackend] = []

        def start():
            backend = WorkingBackend()
            started.append(backend)
            return backend

        monkeypatch.setattr(engine, "_OpenALBackend", start)
        probe = engine.probe_audio()
        assert probe.available is True
        assert [backend.shut_down for backend in started] == [True]

    def test_the_option_runs_the_check_and_exits_with_its_status(self, monkeypatch):
        # A status nothing else uses. Running with no story to download exits with 2, the same number the check uses for "no sound device", so 2 here would pass even if the option did nothing.
        unmistakable_status = 7
        ran: list[bool] = []

        def check() -> int:
            ran.append(True)
            return unmistakable_status

        monkeypatch.setattr(cli, "_handle_check_reader_audio", check)
        with pytest.raises(SystemExit) as stopped:
            cli.main(["--check-reader-audio"])
        assert ran == [True]
        assert stopped.value.code == unmistakable_status


# ── What gets installed, and what gets shipped ───────────────────


def pyopenal_requirements() -> list[str]:
    """Every PyOpenAL requirement the project states, wherever it states it."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # The comment beside the requirement quotes the form that does not work, so comment lines are left out.
    settings = "\n".join(
        line for line in pyproject.splitlines() if not line.lstrip().startswith("#")
    )
    stated = re.findall(r'"(PyOpenAL[^"]*)"', settings)
    stated.append(optional_features.FEATURES["playback"]["pip_name"])
    return stated


class TestRequirement:
    def test_it_is_stated_in_all_three_places(self):
        # The playback extra, the all extra, and the Optional Features entry.
        assert len(pyopenal_requirements()) == 3

    def test_every_statement_names_a_prerelease(self):
        """Every PyOpenAL release is a pre-release. pip 25.1 answers "PyOpenAL>=0.7" with "no matching distribution", and only a requirement that itself names a pre-release is accepted by every pip. A tidy-looking ">=0.7" is the bug."""
        for requirement in pyopenal_requirements():
            version = requirement.split(">=", 1)[1]
            assert re.search(r"\d(a|b|rc)\d", version), requirement

    def test_all_statements_agree(self):
        assert len(set(pyopenal_requirements())) == 1


def build_steps(workflow_name: str) -> list[dict]:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
    return data["jobs"]["build"]["steps"]


def step_running(steps: list[dict], text: str) -> dict:
    matching = [step for step in steps if text in (step.get("run") or "")]
    assert len(matching) == 1, f"expected one step running {text!r}, found {len(matching)}"
    return matching[0]


class TestReleaseBuilds:
    def test_the_windows_build_packs_the_sound_library(self):
        """PyInstaller copies a package's Python modules by itself, but not a DLL sitting beside them."""
        build = step_running(build_steps("build-windows.yml"), "pyinstaller ")
        assert "--collect-binaries openal" in build["run"]

    def test_the_windows_build_fails_if_the_library_does_not_load(self):
        steps = build_steps("build-windows.yml")
        check = step_running(steps, "--check-reader-audio")
        script = check["run"]
        # Loaded with a device (0) and loaded without one (2) both pass; anything else stops the build.
        assert "-eq 0" in script and "-eq 2" in script
        assert "exit 1" in script
        # The check has to run on the bundle that gets zipped, so after the build and before the zip.
        names = [step.get("name", "") for step in steps]
        assert names.index("Build executable") < names.index(check["name"]) < names.index("Create portable zip")

    @pytest.mark.parametrize("workflow_name", ["build-macos.yml", "build-linux.yml"])
    def test_the_other_builds_fail_too_if_the_library_does_not_load(self, workflow_name):
        """Nothing is bundled on a Mac or on Linux, but the frozen program still has to find the system's copy. The first run of this check on a Mac build found that it could not."""
        check = step_running(build_steps(workflow_name), "--check-reader-audio")
        script = check["run"]
        assert '"$status" -ne 0' in script and '"$status" -ne 2' in script
        assert "exit 1" in script
        # A plain call would stop the step at the first non-zero status, before 2 could be let through.
        assert "|| status=$?" in script

    def test_the_linux_build_machine_has_a_system_copy_to_find(self):
        """Without one installed, the Linux check could only ever report "not found" and would prove nothing."""
        install = step_running(build_steps("build-linux.yml"), "apt-get install")
        # Only the commands count. The script's own comments mention the package too.
        commands = [
            line for line in install["run"].splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert any("libopenal1" in line for line in commands)


# ── A voice that keeps failing ───────────────────────────────────


class WSServerHandshakeError(Exception):
    """Stand-in for aiohttp's error of the same name, which is what the Edge voice service's refusal arrives as. The text is the real one, minus its tokens."""


EDGE_REFUSED = WSServerHandshakeError(
    "403, message='Invalid response status', "
    "url='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1'"
)


class InstantEngine:
    """The part of the audio engine the live voice uses. Every clip "finishes" the moment it is played."""

    def __init__(self):
        self.played = []
        self.events = []

    def play_file(self, path, channel, on_done=None):
        self.played.append(path)
        if on_done:
            on_done()

    def pause(self, channel):
        pass

    def resume(self, channel):
        pass

    def stop(self, channel):
        pass

    def emit(self, event):
        self.events.append(event)


def read_aloud(tmp_path, paragraphs: int, fails, *, with_failure_callback=True):
    """Read ``paragraphs`` one-line paragraphs aloud with a voice that fails on the paragraph numbers in ``fails``. Returns what happened."""
    from ficary.audio.events import ReaderEvent
    from ficary.reader.live_tts import LiveTTSController

    attempts: dict[int, int] = {}

    def voice(voice_name, text, out, *, rate=None):
        number = int(text.split()[1].rstrip("."))
        attempts[number] = attempts.get(number, 0) + 1
        if number in fails:
            raise EDGE_REFUSED
        out.write_bytes(b"\x00")

    audio = InstantEngine()
    completed: list[int] = []
    failed: list = []
    controller = LiveTTSController(
        audio, voice="edge:test", synth=voice, tmp_dir=tmp_path,
        on_complete=completed.append,
        on_failed=failed.append if with_failure_callback else None,
    )
    text = "\n\n".join(f"Paragraph {number}." for number in range(1, paragraphs + 1))
    controller.start(text, chapter_number=1)
    controller._worker.join(timeout=10.0)
    assert not controller._worker.is_alive()
    stops = [event for event in audio.events if event.kind == ReaderEvent.TTS_STOPPED]
    return SimpleNamespace(
        attempts=attempts, played=len(audio.played), completed=completed,
        failed=failed, stop_reason=stops[-1].payload.get("reason") if stops else None,
    )


class TestVoiceThatKeepsFailing:
    def test_one_bad_paragraph_is_skipped_and_the_chapter_is_still_read(self, tmp_path):
        result = read_aloud(tmp_path, 4, fails={2})
        assert result.played == 3
        assert result.completed == [1]
        assert result.failed == []
        assert result.stop_reason == "completed"

    def test_a_chapter_where_nothing_could_be_spoken_is_not_a_read_chapter(self, tmp_path):
        """Reported as completed, the reader moved on to the next chapter and failed there too."""
        result = read_aloud(tmp_path, 2, fails={1, 2})
        assert result.played == 0
        assert result.completed == []
        assert result.failed == [EDGE_REFUSED]
        assert result.stop_reason == "failed"

    def test_three_failures_in_a_row_end_the_chapter(self, tmp_path):
        result = read_aloud(tmp_path, 9, fails={2, 3, 4, 5, 6, 7, 8, 9})
        assert result.played == 1
        assert result.completed == []
        assert len(result.failed) == 1
        # The paragraph after the third failure may already have been asked for ahead of time; nothing beyond it is.
        assert max(result.attempts) <= 5

    def test_a_spoken_paragraph_resets_the_count(self, tmp_path):
        result = read_aloud(tmp_path, 6, fails={1, 2, 4, 5})
        assert result.played == 2
        assert result.completed == [4]
        assert result.failed == []

    def test_a_caller_with_no_failure_handler_still_hears_about_it(self, tmp_path):
        result = read_aloud(tmp_path, 2, fails={1, 2}, with_failure_callback=False)
        assert result.completed == [2]

    def test_each_failed_attempt_is_one_line_in_the_log(self, tmp_path, caplog):
        """It used to be a forty-line traceback per attempt."""
        with caplog.at_level(logging.WARNING, logger="ficary.reader.live_tts"):
            read_aloud(tmp_path, 1, fails={1})
        assert len(caplog.records) == 2
        for record in caplog.records:
            assert record.exc_info is None
            assert "WSServerHandshakeError" in record.getMessage()


class TestVoiceErrorExplanation:
    def explain(self, error):
        from ficary.tts_providers import explain_synthesis_error

        return explain_synthesis_error(error)

    def test_a_refusal_by_the_edge_service_says_to_update(self):
        text = self.explain(EDGE_REFUSED)
        assert "refused" in text
        assert "updating Ficary" in text
        assert "Piper" in text
        # The address and its tokens are no use to the person reading this.
        assert "wss://" not in text

    def test_no_connection_points_at_the_connection(self):
        class ClientConnectorError(Exception):
            """Stand-in for aiohttp's error of the same name."""

        for error in (ClientConnectorError("cannot connect"), TimeoutError(), ConnectionResetError()):
            assert "could not be reached" in self.explain(error)

    def test_an_unknown_error_shows_its_own_text(self):
        assert "disk full" in self.explain(OSError("disk full"))

    def test_no_error_at_all_still_says_something(self):
        assert self.explain(None)


class TestReaderWhenTheVoiceFails:
    def test_it_says_why_and_stays_on_the_chapter(self, monkeypatch):
        wx = pytest.importorskip("wx")
        from ficary.reader import gui as reader_gui

        shown: list[str] = []
        monkeypatch.setattr(
            wx, "MessageBox", lambda message, *args, **kwargs: shown.append(message),
        )

        def must_not_move_on(*args, **kwargs):
            raise AssertionError("a voice that failed must not advance the chapter")

        reader = SimpleNamespace(
            _alive=True,
            _playback_current=lambda generation: True,
            _load_chapter=must_not_move_on,
            _begin_playback=must_not_move_on,
        )
        reader_gui.ReaderFrame._on_tts_failed(reader, EDGE_REFUSED, 1)
        assert len(shown) == 1
        assert "could not read this chapter aloud" in shown[0]
        assert "refused" in shown[0]

    def test_a_failure_from_a_cancelled_request_is_ignored(self, monkeypatch):
        wx = pytest.importorskip("wx")
        from ficary.reader import gui as reader_gui

        shown: list[str] = []
        monkeypatch.setattr(
            wx, "MessageBox", lambda message, *args, **kwargs: shown.append(message),
        )
        reader = SimpleNamespace(_alive=True, _playback_current=lambda generation: False)
        reader_gui.ReaderFrame._on_tts_failed(reader, EDGE_REFUSED, 1)
        assert shown == []


def edge_tts_requirements() -> list[str]:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    settings = "\n".join(
        line for line in pyproject.splitlines() if not line.lstrip().startswith("#")
    )
    stated = re.findall(r'"(edge-tts[^"]*)"', settings)
    stated.append(optional_features.FEATURES["audio"]["pip_name"])
    return stated


class TestEdgeVoiceRequirement:
    def test_every_statement_asks_for_a_version_the_service_still_accepts(self):
        """7.2.8 was refused with a 403 on 2026-11-23; 7.3.1 was served. An install that satisfies an older floor cannot speak."""
        stated = edge_tts_requirements()
        assert len(stated) == 3
        for requirement in stated:
            version = tuple(int(part) for part in requirement.split(">=", 1)[1].split("."))
            assert version >= (7, 3, 1), requirement


class TestReaderIsToldWhenTheVoiceFails:
    def test_the_reader_hands_the_voice_a_failure_handler(self, monkeypatch):
        """The handler exists; this checks the reader actually gives it to the voice it starts, and that it leads to the failure message and not to the next chapter."""
        wx = pytest.importorskip("wx")
        from ficary.reader import gui as reader_gui

        monkeypatch.setattr(wx, "CallAfter", lambda function, *args: function(*args))
        made: list[dict] = []

        class RecordingController:
            def __init__(self, audio, **options):
                made.append(options)

            def start(self, text, chapter_number, start_offset=0):
                pass

        monkeypatch.setattr(reader_gui, "LiveTTSController", RecordingController)
        reports: list = []
        reader = SimpleNamespace(
            _resolving_voice=True,
            _playback_current=lambda generation: True,
            _alive=True,
            mode=SimpleNamespace(GetSelection=lambda: 1),
            _paused=False,
            _last_voice="",
            _resume_offset=lambda: 0,
            _dispose_live=lambda: None,
            _engine=object(),
            prefs=SimpleNamespace(get=lambda key: "0"),
            source=SimpleNamespace(story_key="story"),
            _update_voice_status=lambda: None,
            _current_rc=SimpleNamespace(text="Some text."),
            _current_chapter=1,
            _listen_offset=0,
            _live=None,
            _highlight_chunk=lambda chunk, generation: None,
            _on_tts_complete=lambda failed, generation: reports.append(("complete", failed)),
            _on_tts_failed=lambda error, generation: reports.append(("failed", error)),
        )
        reader_gui.ReaderFrame._begin_playback(reader, "edge:test", 5)
        assert len(made) == 1
        made[0]["on_failed"](EDGE_REFUSED)
        assert reports == [("failed", EDGE_REFUSED)]


# ── Finding the library on a Mac ─────────────────────────────────


def mac_library_lookup(name):
    """What ``ctypes.util.find_library`` answers on a real Mac (macOS 27), for the names PyOpenAL asks for and for the one that works.

    Apple's OpenAL lives only in the system's shared library cache, where the lookup is case-sensitive: "openal" finds nothing, "OpenAL" finds the framework.
    """
    answers = {
        "soft_oal": None,
        "libsoft_oal": None,
        "OpenAL32": None,
        "libOpenAL32": None,
        "openal": None,
        "libopenal": None,
        "OpenAL": "/System/Library/Frameworks/OpenAL.framework/OpenAL",
    }
    return answers.get(name)


class TestFindingTheLibraryOnAMac:
    def test_the_name_pyopenal_asks_for_is_answered_with_apples_framework(self, monkeypatch):
        import ctypes.util

        monkeypatch.setattr(engine.sys, "platform", "darwin")
        monkeypatch.setattr(ctypes.util, "find_library", mac_library_lookup)
        assert ctypes.util.find_library("openal") is None
        with engine._openal_lookup_help():
            assert ctypes.util.find_library("openal") == (
                "/System/Library/Frameworks/OpenAL.framework/OpenAL"
            )

    def test_other_names_are_left_alone(self, monkeypatch):
        import ctypes.util

        monkeypatch.setattr(engine.sys, "platform", "darwin")
        monkeypatch.setattr(ctypes.util, "find_library", mac_library_lookup)
        with engine._openal_lookup_help():
            assert ctypes.util.find_library("soft_oal") is None
            assert ctypes.util.find_library("OpenAL32") is None
            assert ctypes.util.find_library("libopenal") is None

    def test_a_copy_the_ordinary_lookup_finds_is_used_as_it_is(self, monkeypatch):
        """Someone who installed OpenAL Soft themselves gets theirs, not Apple's."""
        import ctypes.util

        monkeypatch.setattr(engine.sys, "platform", "darwin")
        monkeypatch.setattr(
            ctypes.util, "find_library",
            lambda name: "/usr/local/lib/libopenal.dylib" if name == "openal" else None,
        )
        with engine._openal_lookup_help():
            assert ctypes.util.find_library("openal") == "/usr/local/lib/libopenal.dylib"

    def test_the_stand_in_is_gone_afterwards_even_after_an_error(self, monkeypatch):
        import ctypes.util

        monkeypatch.setattr(engine.sys, "platform", "darwin")
        monkeypatch.setattr(ctypes.util, "find_library", mac_library_lookup)
        with pytest.raises(RuntimeError):
            with engine._openal_lookup_help():
                raise RuntimeError("import failed")
        assert ctypes.util.find_library is mac_library_lookup
        assert ctypes.util.find_library("openal") is None

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_nothing_is_touched_on_other_systems(self, monkeypatch, platform):
        import ctypes.util

        monkeypatch.setattr(engine.sys, "platform", platform)
        monkeypatch.setattr(ctypes.util, "find_library", mac_library_lookup)
        with engine._openal_lookup_help():
            assert ctypes.util.find_library is mac_library_lookup

    def test_the_backend_imports_pyopenal_with_the_help_in_place(self, monkeypatch):
        """The help is only any use if it is active at the moment PyOpenAL is imported."""
        import ctypes.util
        import sys as real_sys
        import types

        monkeypatch.setattr(engine.sys, "platform", "darwin")
        monkeypatch.setattr(ctypes.util, "find_library", mac_library_lookup)
        seen: list = []

        class ImportingOpenal(types.ModuleType):
            """Stands in for the openal package; looking up its library is what importing the real one does."""

            def oalInit(self):
                pass

        fake = ImportingOpenal("openal")

        class Finder:
            def find_spec(self, name, path=None, target=None):
                if name != "openal":
                    return None
                seen.append(ctypes.util.find_library("openal"))
                real_sys.modules["openal"] = fake
                import importlib.machinery

                return importlib.machinery.ModuleSpec("openal", loader=self)

            def create_module(self, spec):
                return fake

            def exec_module(self, module):
                pass

        finder = Finder()
        monkeypatch.delitem(real_sys.modules, "openal", raising=False)
        monkeypatch.setattr(real_sys, "meta_path", [finder, *real_sys.meta_path])
        engine._OpenALBackend()
        assert seen == ["/System/Library/Frameworks/OpenAL.framework/OpenAL"]
        monkeypatch.delitem(real_sys.modules, "openal", raising=False)
