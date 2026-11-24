"""The screen reader says when a download, a library update or an audiobook finishes.

Chosen for the library-first main window. It goes through prism (PyPI ``prismatoid``) and speaks only through a screen reader: with none running prism's best choice is the computer's own voice, which must not start talking to someone who does not use a screen reader. All speech is on one thread, because the Windows screen readers are COM objects tied to the thread that made them. These tests use a stand-in prism; ``ficary --check-announcements`` run inside each release build proves the real one loads.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
import weakref
from pathlib import Path

import pytest

from ficary import announce
from ficary import prefs as prefs_module

REPO = Path(__file__).resolve().parents[1]


class Features:
    def __init__(self, *, screen_reader=True, output=True, speak=True):
        self.is_screen_reader = screen_reader
        self.supports_output = output
        self.supports_speak = speak


class Backend:
    def __init__(self, said: list, *, name="NVDA", **features):
        self.name = name
        self.features = Features(**features)
        self.said = said

    def output(self, text, interrupt=True):
        self.said.append(("output", text, interrupt, threading.current_thread().name, self.name))

    def speak(self, text, interrupt=True):
        self.said.append(("speak", text, interrupt, threading.current_thread().name, self.name))


def fake_prism(said: list, **backend_options):
    """A prism module whose best backend is described by ``backend_options``."""
    module = types.ModuleType("prism")
    module.contexts = []
    # Weak references to every backend made, oldest first: whether one is
    # still alive says whether Ficary kept it.
    module.made = []

    class Context:
        def __init__(self):
            module.contexts.append(threading.current_thread().name)

        def create_best(self):
            if backend_options.get("raise_on_create"):
                raise ValueError("Invalid or unsupported backend")
            options = {key: value for key, value in backend_options.items() if key != "raise_on_create"}
            backend = Backend(said, **options)
            module.made.append(weakref.ref(backend))
            return backend

    module.Context = Context
    return module


@pytest.fixture(autouse=True)
def quick_check(monkeypatch):
    """The check command waits a second after speaking; not in these tests unless one asks."""
    monkeypatch.setattr(announce, "_CHECK_SETTLE_SECONDS", 0, raising=False)


@pytest.fixture
def fresh_speaker(monkeypatch):
    """A new speech thread and queue for each test, so one test's speaker cannot answer for another."""
    import queue

    monkeypatch.setattr(announce, "_queue", queue.Queue())
    monkeypatch.setattr(announce, "_thread", None)
    yield
    announce._queue.put(None)


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class TestSpeaking:
    def test_a_screen_reader_says_it_through_output_without_cutting_in(self, fresh_speaker, monkeypatch):
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said))
        announce.announce("Downloaded The Loaf.")
        assert wait_for(lambda: said)
        assert said[0][:3] == ("output", "Downloaded The Loaf.", False)

    def test_a_reader_without_output_is_used_through_speak(self, fresh_speaker, monkeypatch):
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said, output=False))
        announce.announce("Downloaded The Loaf.")
        assert wait_for(lambda: said)
        assert said[0][0] == "speak"

    def test_the_computers_own_voice_is_never_used(self, fresh_speaker, monkeypatch):
        """No screen reader running: prism would pick a speech engine. Nothing is said."""
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said, name="SAPI", screen_reader=False))
        announce.announce("Downloaded The Loaf.")
        time.sleep(0.3)
        assert said == []

    def test_everything_happens_on_one_thread_in_order(self, fresh_speaker, monkeypatch):
        said = []
        module = fake_prism(said)
        monkeypatch.setitem(sys.modules, "prism", module)
        for number in range(3):
            announce.announce(f"Message {number}.")
        assert wait_for(lambda: len(said) == 3)
        assert [entry[1] for entry in said] == ["Message 0.", "Message 1.", "Message 2."]
        assert {entry[3] for entry in said} == {"ficary-announce"}
        assert module.contexts == ["ficary-announce"]

    def test_announcing_returns_at_once(self, fresh_speaker, monkeypatch):
        """The caller is a download worker or the window; speaking must not hold it up."""
        said = []
        slow = fake_prism(said)
        real_create = slow.Context.create_best

        def slow_create(self):
            time.sleep(0.5)
            return real_create(self)

        slow.Context.create_best = slow_create
        monkeypatch.setitem(sys.modules, "prism", slow)
        started = time.monotonic()
        announce.announce("Downloaded The Loaf.")
        assert time.monotonic() - started < 0.2
        assert wait_for(lambda: said)

    @pytest.mark.parametrize("text", ["", "   ", "\x00"])
    def test_nothing_is_said_for_an_empty_message(self, fresh_speaker, monkeypatch, text):
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said))
        announce.announce(text)
        time.sleep(0.2)
        assert said == []

    def test_a_nul_in_a_title_does_not_lose_the_message(self, fresh_speaker, monkeypatch):
        """prism refuses text with a NUL in it."""
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said))
        announce.announce("Downloaded The\x00 Loaf.")
        assert wait_for(lambda: said)
        assert said[0][1] == "Downloaded The Loaf."

    def test_no_prism_is_quiet_and_not_retried(self, fresh_speaker, monkeypatch):
        attempts = []
        import builtins

        real_import = builtins.__import__

        def no_prism(name, *args, **kwargs):
            if name == "prism":
                attempts.append(name)
                raise ImportError("No module named 'prism'")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "prism", raising=False)
        monkeypatch.setattr(builtins, "__import__", no_prism)
        announce.announce("One.")
        announce.announce("Two.")
        assert wait_for(lambda: announce._queue.unfinished_tasks == 0 or announce._queue.empty())
        time.sleep(0.2)
        assert attempts == ["prism"]

    def test_a_voice_that_cannot_be_made_now_is_tried_again_next_time(self, fresh_speaker, monkeypatch):
        """A screen reader started after Ficary should still be heard."""
        said = []
        module = fake_prism(said, raise_on_create=True)
        monkeypatch.setitem(sys.modules, "prism", module)
        announce.announce("Nobody hears this.")
        time.sleep(0.2)
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said))
        module.Context.create_best = lambda self: Backend(said)
        announce.announce("Somebody hears this.")
        assert wait_for(lambda: said)
        assert said[0][1] == "Somebody hears this."

    def test_one_backend_is_kept_for_every_message(self, fresh_speaker, monkeypatch):
        """prism's VoiceOver backend says a message a moment after the call
        returns, and freeing the backend before then cancels it. A new
        backend for each message would also have VoiceOver announce the
        window every time."""
        said = []
        module = fake_prism(said)
        monkeypatch.setitem(sys.modules, "prism", module)
        for number in range(3):
            announce.announce(f"Message {number}.")
        assert wait_for(lambda: len(said) == 3)
        assert len(module.made) == 1
        assert module.made[0]() is not None, "the backend was freed straight after speaking"

    def test_a_reader_that_stops_answering_is_found_again(self, fresh_speaker, monkeypatch):
        """NVDA's backend does not reconnect after NVDA restarts: the call
        fails, and the message goes through a new backend."""
        said = []
        module = fake_prism(said)
        monkeypatch.setitem(sys.modules, "prism", module)
        announce.announce("Before the restart.")
        assert wait_for(lambda: len(said) == 1)

        def restarted(text, interrupt=True):
            raise RuntimeError("The RPC server is unavailable.")

        module.made[0]().output = restarted
        announce.announce("After the restart.")
        assert wait_for(lambda: len(said) == 2)
        assert said[1][1] == "After the restart."
        assert len(module.made) == 2
        assert module.made[1]() is not None

    def test_the_computers_own_voice_is_not_kept(self, fresh_speaker, monkeypatch):
        """With no screen reader at the first message, the speech engine is
        not kept: the screen reader started afterwards says the next one."""
        said = []
        module = fake_prism(said)
        backends = iter([Backend(said, name="SAPI", screen_reader=False), Backend(said, name="NVDA")])
        module.Context.create_best = lambda self: next(backends)
        monkeypatch.setitem(sys.modules, "prism", module)
        announce.announce("Nobody hears this.")
        announce.announce("NVDA says this.")
        assert wait_for(lambda: said)
        time.sleep(0.1)
        assert said == [("output", "NVDA says this.", False, "ficary-announce", "NVDA")]


class TestTheCheck:
    def test_a_screen_reader_is_sent_the_test_sentence(self, monkeypatch):
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said))
        result = announce.check()
        assert result.working and result.screen_reader and result.backend == "NVDA"
        assert said[0][1] == "Ficary can talk to your screen reader."

    def test_the_check_waits_for_the_reader_before_exiting(self, monkeypatch):
        """VoiceOver says the sentence a moment after the call returns, and
        the command exits as soon as the check is done."""
        monkeypatch.setitem(sys.modules, "prism", fake_prism([]))
        monkeypatch.setattr(announce, "_CHECK_SETTLE_SECONDS", 0.3)
        started = time.monotonic()
        assert announce.check().screen_reader
        assert time.monotonic() - started >= 0.3

    @pytest.mark.parametrize(
        "options",
        [{"name": "SAPI", "screen_reader": False}, {"output": False, "speak": False}],
        ids=["no screen reader", "a reader that cannot take it"],
    )
    def test_the_check_does_not_wait_when_nothing_was_said(self, monkeypatch, options):
        monkeypatch.setitem(sys.modules, "prism", fake_prism([], **options))
        monkeypatch.setattr(announce, "_CHECK_SETTLE_SECONDS", 5)
        started = time.monotonic()
        announce.check()
        assert time.monotonic() - started < 1

    def test_the_check_uses_one_backend(self, monkeypatch):
        module = fake_prism([])
        monkeypatch.setitem(sys.modules, "prism", module)
        announce.check()
        assert len(module.made) == 1

    def test_no_screen_reader_running(self, monkeypatch):
        said = []
        monkeypatch.setitem(sys.modules, "prism", fake_prism(said, name="SAPI", screen_reader=False))
        result = announce.check()
        assert result.working and not result.screen_reader and result.backend == "SAPI"
        assert said == []

    def test_no_voice_of_any_kind_still_counts_as_working(self, monkeypatch):
        """What a build machine without speech gives: prism loads and finds nothing to speak with."""
        monkeypatch.setitem(sys.modules, "prism", fake_prism([], raise_on_create=True))
        result = announce.check()
        assert result.working and not result.screen_reader
        assert "no voice of any kind" in result.detail

    def test_prism_missing_is_not_working(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "prism", None)
        result = announce.check()
        assert not result.working
        assert "ImportError" in result.detail or "ModuleNotFoundError" in result.detail

    @pytest.mark.parametrize(
        "options, status",
        [({}, 0), ({"name": "SAPI", "screen_reader": False}, 2), ({"raise_on_create": True}, 2), (None, 1)],
    )
    def test_the_command_says_so_and_exits_with_the_right_status(self, monkeypatch, capsys, options, status):
        from ficary import cli

        monkeypatch.setitem(sys.modules, "prism", None if options is None else fake_prism([], **options))
        assert cli._handle_check_announcements() == status
        out = capsys.readouterr().out
        assert out.startswith("Screen reader announcement check")
        assert "Result:" in out

    def test_the_option_is_on_the_command_line(self):
        from ficary.cli import _build_parser

        assert _build_parser().parse_args(["--check-announcements"]).check_announcements is True


class TestPackaging:
    def test_the_announce_extra_and_the_desktop_downloads_include_prism(self):
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert 'announce = ["prismatoid~=0.18.4"]' in text
        all_line = next(line for line in text.splitlines() if line.startswith("all = "))
        assert "prismatoid~=0.18.4" in all_line

    def test_the_server_image_does_not_install_it(self):
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        assert "prism" not in dockerfile
        assert '"/src[epub,audio]"' in dockerfile

    def test_it_is_an_optional_feature_for_source_installs(self):
        from ficary import optional_features

        feature = optional_features.FEATURES["announce"]
        assert feature["pip_name"] == "prismatoid~=0.18.4"
        assert feature["import_name"] == "prism"
        assert "announce" in optional_features.available()

    @pytest.mark.parametrize("name, binary", [
        ("windows", "dist\\ficary\\ficary.exe --check-announcements"),
        ("macos", "dist/ficary.app/Contents/MacOS/ficary --check-announcements"),
        ("linux", "dist/ficary/ficary --check-announcements"),
    ])
    def test_every_build_bundles_prism_and_proves_it_loads(self, name, binary):
        workflow = (REPO / ".github" / "workflows" / f"build-{name}.yml").read_text(encoding="utf-8")
        assert "--collect-all prism" in workflow
        assert "--additional-hooks-dir tools/pyinstaller_hooks" in workflow
        assert binary in workflow
        step = workflow[workflow.index("Check the screen-reader announcements load from the bundle"):]
        step = step[: step.index("- name:", 10)] if "- name:" in step[10:] else step
        # 0 (a reader spoke) and 2 (no reader on the build machine) pass; anything else fails the build.
        assert ("-eq 0 -or $status -eq 2" in step) or ('-ne 0 ] && [ "$status" -ne 2' in step)

    def test_the_hook_brings_in_the_speech_bridge(self, monkeypatch):
        """Run the hook the way PyInstaller would, with a stand-in for its helper."""
        seen = {}
        hooks = types.ModuleType("PyInstaller.utils.hooks")

        def collect_dynamic_libs(package, search_patterns=None):
            seen["package"] = package
            seen["patterns"] = search_patterns
            return [("prism/_native/_prism_cffi.pyd", "prism/_native")]

        hooks.collect_dynamic_libs = collect_dynamic_libs
        for name in ("PyInstaller", "PyInstaller.utils"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
        namespace: dict = {}
        exec((REPO / "tools" / "pyinstaller_hooks" / "hook-prism.py").read_text(encoding="utf-8"), namespace)
        assert seen["package"] == "prism"
        assert "*.pyd" in seen["patterns"] and "*.so" in seen["patterns"]
        assert namespace["hiddenimports"] == ["_cffi_backend"]


# ── In the windows ───────────────────────────────────────────────

wx = pytest.importorskip("wx")
needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


@pytest.fixture
def window(wx_app, monkeypatch, tmp_path):
    from ficary import gui

    frame = gui.MainFrame()
    values = {}

    def get_bool(key, default=False):
        return bool(values.get(key, prefs_module.DEFAULTS.get(key, default)))

    monkeypatch.setattr(frame.prefs, "get_bool", get_bool)
    frame.values = values
    frame.announced = []
    monkeypatch.setattr(announce, "announce", lambda text: frame.announced.append(text))
    frame.exported = []

    def export(story, params, update_path=None, **options):
        frame.exported.append(story.url)
        return str(tmp_path / f"{story.id}.epub")

    monkeypatch.setattr(frame, "_export_story", export)
    yield frame
    frame._clip_timer.Stop()
    frame.Destroy()


class OneStory:
    site_name = "royalroad"
    concurrency = 1

    def __init__(self, title="The Loaf", fail=None):
        self.title = title
        self.fail = fail

    def parse_story_id(self, url):
        return 7

    def is_forum_url(self, url):
        return False

    def is_author_url(self, url):
        return False

    def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
        from ficary.models import Chapter, Story

        if self.fail:
            raise self.fail
        story = Story(id=7, title=self.title, author="Nobody", summary="", url=url)
        story.chapters.append(Chapter(1, "One", "<p>x</p>"))
        if progress_callback:
            progress_callback(1, 1, "One", False)
        return story


def params(window, fmt="epub"):
    from ficary import gui

    return gui._DownloadParams(
        fmt=fmt, raw_output_dir="", filename_template="{title}",
        hr_as_stars=False, strip_notes=False, llm_strip_notes=False,
        stop_generation=window._stop_generation,
    )


URL = "https://www.royalroad.com/fiction/7/the-loaf"


@needs_display
class TestTheMainWindow:
    def test_a_finished_download_is_announced(self, window, monkeypatch):
        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: OneStory())
        window._run_download(URL, params=params(window))
        assert window.announced == ["Downloaded The Loaf."]

    def test_a_finished_audiobook_says_so(self, window, monkeypatch):
        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: OneStory())
        window._run_download(URL, params=params(window, fmt="audio"))
        assert window.announced == ["Audiobook finished: The Loaf."]

    def test_a_failed_download_is_announced_without_reading_out_the_address(self, window, monkeypatch):
        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: OneStory(fail=RuntimeError("HTTP 500")))
        window._run_download(URL, params=params(window))
        assert window.announced == ["A download failed. The status pane says why."]

    def test_a_stopped_download_is_not_announced(self, window, monkeypatch):
        """The user pressed Stop and heard it stop."""
        story_params = params(window)
        window._on_stop()
        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: OneStory())
        window._run_download(URL, params=story_params)
        assert window.announced == []

    def test_nothing_is_announced_when_switched_off(self, window, monkeypatch):
        window.values[prefs_module.KEY_ANNOUNCE_FINISHED] = False
        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: OneStory())
        window._run_download(URL, params=params(window))
        assert window.announced == []

    def test_a_batch_is_announced_once_at_the_end(self, window):
        urls = [f"https://www.royalroad.com/fiction/{number}/a" for number in (1, 2, 3)]
        window._batch_download(urls, OneStory(), params=params(window))
        assert window.announced == ["Batch complete: 3 succeeded, 0 failed out of 3."]

    def test_a_stopped_batch_is_not_announced(self, window):
        urls = [f"https://www.royalroad.com/fiction/{number}/a" for number in (1, 2, 3)]
        batch_params = params(window)
        window._on_stop()
        window._batch_download(urls, OneStory(), params=batch_params)
        assert window.announced == []


@needs_display
class TestTheLibraryWindow:
    def library(self, window):
        from ficary.library.gui import LibraryFrame

        return LibraryFrame(window, window.prefs)

    def test_the_update_summary_is_announced_when_it_ends(self, window):
        library = self.library(window)
        try:
            library._update_cancel_event = threading.Event()
            library._update_running = True
            library._post_status("Library update complete — 2 updated, 40 up to date, 0 failed, 3 skipped.")
            library._update_finished()
            assert window.announced == ["Library update complete — 2 updated, 40 up to date, 0 failed, 3 skipped."]
        finally:
            library.Destroy()

    def test_two_library_folders_are_said_together(self, window):
        library = self.library(window)
        try:
            library._update_cancel_event = threading.Event()
            library._post_status("Library update complete — 1 updated, 2 up to date, 0 failed, 0 skipped.")
            library._post_status("Library update complete — 0 updated, 5 up to date, 0 failed, 0 skipped.")
            library._update_finished()
            assert len(window.announced) == 1
            assert window.announced[0].count("Library update complete") == 2
        finally:
            library.Destroy()

    def test_a_cancelled_update_is_not_announced(self, window):
        library = self.library(window)
        try:
            library._update_cancel_event = threading.Event()
            library._update_cancel_event.set()
            library._post_status("Library update cancelled — 0 updated, 2 up to date, 0 failed, 0 skipped.")
            library._update_finished()
            assert window.announced == []
        finally:
            library.Destroy()

    def test_other_lines_are_not_announced(self, window):
        library = self.library(window)
        try:
            library._update_cancel_event = threading.Event()
            library._post_status("  [probe 1/3] story.epub: 4 chapter(s) upstream")
            library._update_finished()
            assert window.announced == []
        finally:
            library.Destroy()


@needs_display
class TestPreferences:
    def test_the_setting_is_on_the_general_page_on_by_default_and_saved(self, wx_app):
        from ficary.preferences import PreferencesDialog

        values = {}

        class Prefs:
            def get(self, key, default=None):
                return values.get(key, default)

            def get_bool(self, key, default=False):
                return bool(values.get(key, prefs_module.DEFAULTS.get(key, default)))

            def set(self, key, value):
                values[key] = value

            def set_bool(self, key, value):
                values[key] = bool(value)

            def get_int(self, key, default=0):
                return int(values.get(key, default) or 0)

            def set_int(self, key, value):
                values[key] = int(value)

        frame = wx.Frame(None)
        try:
            dialog = PreferencesDialog(frame, Prefs())
            assert dialog.announce_ctrl.GetLabel() == "Have your screen &reader say when a download finishes"
            assert dialog.announce_ctrl.GetValue() is True
            dialog.announce_ctrl.SetValue(False)
            dialog._save()
            assert values[prefs_module.KEY_ANNOUNCE_FINISHED] is False
            dialog.Destroy()
        finally:
            frame.Destroy()
