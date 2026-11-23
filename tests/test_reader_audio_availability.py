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
    def test_the_other_builds_report_without_gating(self, workflow_name):
        """Nothing is bundled there; the result depends on the machine the program later runs on, so it must not fail a release."""
        check = step_running(build_steps(workflow_name), "--check-reader-audio")
        assert "|| echo" in check["run"]
