"""Checking the library for new chapters when Ficary opens.

The maintainer chose this for the library-first main window: when Ficary opens it quietly asks each site how many chapters the stories it has not checked in the last day now have, and says in one line how many stories have new chapters. Nothing is downloaded; Library > Check for Story Updates (Ctrl+U) does that, and finds what the check found without asking the sites again. It takes turns with downloads for each site, stops for Stop, Ctrl+U and closing, and can be switched off in Preferences.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from ficary import cli
from ficary import prefs as prefs_module
from ficary.download_queue import site_slot
from ficary.library import launch_check
from ficary.library.index import LibraryIndex
from ficary.library.refresh import DEFAULT_GUI_RECHECK_INTERVAL_S, build_refresh_queue
from ficary.library.scanner import scan
from ficary.scraper import CloudflareChallengeError

from .library_fixtures import ficary_epub

SITE = "launchcheck-test-site"


class FakeSiteClass:
    site_name = SITE


class FakeSite:
    """Answers chapter counts from a table, and writes down every question."""

    site_name = SITE
    concurrency = 1

    def __init__(self, counts: dict):
        self.counts = counts
        self.asked: list[str] = []
        self.lock = threading.Lock()

    def probe_chapter_count(self, url):
        with self.lock:
            self.asked.append(url)
        answer = self.counts[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    get_chapter_count = probe_chapter_count


def url_of(number: int) -> str:
    return f"https://www.fanfiction.net/s/{number}"


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library of three unfinished stories with 3, 2 and 4 chapters, scanned into its own index."""
    root = tmp_path / "library"
    root.mkdir()
    for number, chapters in ((101, 3), (102, 2), (103, 4)):
        ficary_epub(root, title=f"Story {number}", url=f"{url_of(number)}/1/", chapters=chapters, story_id=number)
    index_path = tmp_path / "library-index.json"
    scan(root, index_path=index_path)
    monkeypatch.setattr(cli, "_detect_site", lambda url: FakeSiteClass)
    downloads.clear()
    # Recorded, not raised: a download would run on a queue worker, where an exception is caught and reported as a failed story.
    monkeypatch.setattr(cli, "_download_one", lambda url, *a, **k: downloads.append(url) or True)
    return root.resolve(), index_path


downloads: list[str] = []


def plug_in(monkeypatch, site: FakeSite) -> None:
    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: site)


def stories(index_path: Path, root: Path) -> dict:
    return dict(LibraryIndex.load(index_path).stories_in(root))


def check(root, index_path, **options):
    cancel = options.pop("cancel_event", threading.Event())
    return launch_check.check_for_new_chapters([root], cancel_event=cancel, index_path=index_path, **options)


class TestTheCheck:
    def test_it_counts_the_stories_with_new_chapters(self, library, monkeypatch):
        root, index_path = library
        plug_in(monkeypatch, FakeSite({url_of(101): 5, url_of(102): 2, url_of(103): 6}))
        assert check(root, index_path) == 2

    def test_the_answers_go_into_the_index(self, library, monkeypatch):
        root, index_path = library
        plug_in(monkeypatch, FakeSite({url_of(101): 5, url_of(102): 2, url_of(103): 4}))
        check(root, index_path)
        recorded = stories(index_path, root)
        assert recorded[url_of(101)]["remote_chapter_count"] == 5
        assert recorded[url_of(102)]["remote_chapter_count"] == 2
        assert all(entry.get("last_probed") for entry in recorded.values())

    def test_ctrl_u_then_downloads_them_without_asking_again(self, library, monkeypatch):
        """What the check found is what an update downloads: the stories come back with their new count already known."""
        root, index_path = library
        plug_in(monkeypatch, FakeSite({url_of(101): 5, url_of(102): 2, url_of(103): 4}))
        check(root, index_path)
        queue, _skipped = build_refresh_queue(
            root, index_path=index_path, skip_complete=True,
            recheck_interval_s=DEFAULT_GUI_RECHECK_INTERVAL_S, progress=lambda line: None,
        )
        assert [(entry["url"], entry.get("remote")) for entry in queue] == [(url_of(101), 5)]

    def test_stories_checked_in_the_last_day_are_not_asked_about(self, library, monkeypatch):
        root, index_path = library
        index = LibraryIndex.load(index_path)
        index.mark_probed(root, {url_of(102): 2})
        site = FakeSite({url_of(101): 3, url_of(103): 4})
        plug_in(monkeypatch, site)
        check(root, index_path)
        assert sorted(site.asked) == [url_of(101), url_of(103)]

    def test_a_story_already_known_to_be_waiting_is_counted_without_asking(self, library, monkeypatch):
        root, index_path = library
        index = LibraryIndex.load(index_path)
        index.mark_probed(root, {url_of(101): 9})
        site = FakeSite({url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        assert check(root, index_path) == 1
        assert url_of(101) not in site.asked

    def test_it_never_downloads(self, library, monkeypatch):
        from ficary.download_queue import DownloadQueues

        root, index_path = library
        plug_in(monkeypatch, FakeSite({url_of(101): 5, url_of(102): 2, url_of(103): 6}))
        check(root, index_path)
        # Give anything that was queued a moment to run.
        for _ in range(50):
            if SITE not in DownloadQueues.snapshot():
                break
            time.sleep(0.05)
        assert downloads == []

    def test_nothing_waiting_is_a_count_of_nought(self, library, monkeypatch):
        root, index_path = library
        plug_in(monkeypatch, FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4}))
        assert check(root, index_path) == 0

    def test_the_cloudflare_breaker_still_applies(self, library, monkeypatch):
        """Same loop as Ctrl+U: a site challenging every request stops being asked."""
        root, index_path = library
        challenged = CloudflareChallengeError("https://www.fanfiction.net is challenged")
        site = FakeSite({url_of(101): challenged, url_of(102): challenged, url_of(103): challenged})
        plug_in(monkeypatch, site)
        monkeypatch.setattr(cli, "_CF_BREAKER_THRESHOLD", 2)
        check(root, index_path)
        assert len(site.asked) == 2


class TestNothingToCheck:
    def test_no_library_folders(self, tmp_path):
        assert launch_check.check_for_new_chapters([], cancel_event=threading.Event(), index_path=tmp_path / "index.json") is None

    def test_an_empty_index(self, tmp_path):
        root = tmp_path / "library"
        root.mkdir()
        assert check(root, tmp_path / "index.json") is None


class TestTakingTurns:
    def test_each_check_waits_for_a_download_from_the_same_site(self, library, monkeypatch):
        root, index_path = library
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        holding = threading.Event()
        release = threading.Event()

        def a_download():
            with site_slot(SITE):
                holding.set()
                release.wait(10)

        downloader = threading.Thread(target=a_download, daemon=True)
        downloader.start()
        assert holding.wait(5)
        result = {}
        checker = threading.Thread(target=lambda: result.setdefault("count", check(root, index_path)), daemon=True)
        checker.start()
        time.sleep(0.5)
        try:
            assert site.asked == [], "the check asked the site while a download had its turn"
        finally:
            release.set()
        checker.join(10)
        downloader.join(5)
        assert result["count"] == 0
        assert len(site.asked) == 3

    def test_stopping_while_waiting_ends_the_check_at_once(self, library, monkeypatch):
        """Parked behind somebody's long download, the check must not hold up Stop or closing Ficary."""
        root, index_path = library
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        holding = threading.Event()
        release = threading.Event()

        def a_long_download():
            with site_slot(SITE):
                holding.set()
                release.wait(30)

        downloader = threading.Thread(target=a_long_download, daemon=True)
        downloader.start()
        assert holding.wait(5)
        cancel = threading.Event()
        result = {}
        checker = threading.Thread(target=lambda: result.setdefault("count", check(root, index_path, cancel_event=cancel)), daemon=True)
        checker.start()
        time.sleep(0.3)
        started = time.monotonic()
        cancel.set()
        checker.join(5)
        stopped_after = time.monotonic() - started
        release.set()
        downloader.join(5)
        assert not checker.is_alive()
        assert stopped_after < 2
        assert result["count"] is None
        assert site.asked == []

    def test_a_slot_wait_without_a_cancel_event_still_just_waits(self):
        """Every other caller of the slot is unchanged."""
        name = "launchcheck-plain-slot"
        holding = threading.Event()
        release = threading.Event()
        got_it = threading.Event()

        def holder():
            with site_slot(name):
                holding.set()
                release.wait(5)

        def waiter():
            with site_slot(name):
                got_it.set()

        first = threading.Thread(target=holder, daemon=True)
        first.start()
        assert holding.wait(5)
        second = threading.Thread(target=waiter, daemon=True)
        second.start()
        time.sleep(0.3)
        assert not got_it.is_set()
        release.set()
        assert got_it.wait(5)

    def test_a_cancel_before_it_starts_asks_nothing(self, library, monkeypatch):
        root, index_path = library
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        cancel = threading.Event()
        cancel.set()
        assert check(root, index_path, cancel_event=cancel) is None
        assert site.asked == []


class TestWhereItLooks:
    def test_the_library_and_the_separate_adult_folder(self, tmp_path):
        main = tmp_path / "main"
        adult = tmp_path / "adult"
        main.mkdir()
        adult.mkdir()
        values = {prefs_module.KEY_LIBRARY_PATH: str(main), prefs_module.KEY_LIBRARY_ADULT_PATH: str(adult)}

        class Prefs:
            def get(self, key, default=None):
                return values.get(key, default)

        assert launch_check.library_roots(Prefs()) == [main.resolve(), adult.resolve()]

    def test_missing_and_repeated_folders_are_left_out(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        values = {prefs_module.KEY_LIBRARY_PATH: str(main), prefs_module.KEY_LIBRARY_ADULT_PATH: str(main)}

        class Prefs:
            def get(self, key, default=None):
                return values.get(key, default)

        assert launch_check.library_roots(Prefs()) == [main.resolve()]
        values[prefs_module.KEY_LIBRARY_PATH] = str(tmp_path / "gone")
        values[prefs_module.KEY_LIBRARY_ADULT_PATH] = ""
        assert launch_check.library_roots(Prefs()) == []


class TestTheLine:
    @pytest.mark.parametrize(
        "waiting, line",
        [
            (0, "No stories in your library have new chapters."),
            (1, "1 story in your library has new chapters. Library > Check for Story Updates (Ctrl+U) downloads it."),
            (4, "4 stories in your library have new chapters. Library > Check for Story Updates (Ctrl+U) downloads them."),
        ],
    )
    def test_what_the_status_pane_says(self, waiting, line):
        assert launch_check.waiting_summary(waiting) == line

    def test_it_is_on_unless_switched_off(self):
        assert prefs_module.DEFAULTS[prefs_module.KEY_CHECK_STORIES_AT_LAUNCH] is True

    def test_it_waits_a_day_before_asking_again(self):
        assert launch_check.LAUNCH_RECHECK_INTERVAL_S == 24 * 60 * 60


# ── In the window ────────────────────────────────────────────────

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


@pytest.fixture
def window(monkeypatch):
    wx = pytest.importorskip("wx")
    from ficary.gui import MainFrame

    app = wx.GetApp() or wx.App()
    frame = MainFrame()
    values = {}

    def get(key, default=None):
        return values.get(key, default)

    def get_bool(key, default=False):
        return bool(values.get(key, prefs_module.DEFAULTS.get(key, default)))

    monkeypatch.setattr(frame.prefs, "get", get)
    monkeypatch.setattr(frame.prefs, "get_bool", get_bool)
    frame.values = values
    with frame._log_lock:
        frame._log_queue.clear()
    yield frame
    frame._clip_timer.Stop()
    frame.Destroy()
    del app


def logged(frame) -> str:
    with frame._log_lock:
        return "".join(frame._log_queue)


@needs_display
class TestTheWindow:
    def started_threads(self, frame, monkeypatch):
        from ficary import gui

        started = []

        class Thread:
            def __init__(self, target=None, args=(), daemon=None, name=None, **options):
                self.name = name

            def start(self):
                started.append(self.name)

        monkeypatch.setattr(gui.threading, "Thread", Thread)
        frame.original_story_check()
        return started

    def test_it_starts_at_launch_when_there_is_a_library(self, window, monkeypatch, tmp_path):
        window.values[prefs_module.KEY_LIBRARY_PATH] = str(tmp_path)
        assert self.started_threads(window, monkeypatch) == ["ficary-launch-story-check"]

    def test_not_when_switched_off(self, window, monkeypatch, tmp_path):
        window.values[prefs_module.KEY_LIBRARY_PATH] = str(tmp_path)
        window.values[prefs_module.KEY_CHECK_STORIES_AT_LAUNCH] = False
        assert self.started_threads(window, monkeypatch) == []

    def test_not_without_a_library(self, window, monkeypatch):
        assert self.started_threads(window, monkeypatch) == []

    def test_the_window_starts_it_when_it_opens(self, monkeypatch):
        """The suite holds it back; check the window asks for it."""
        wx = pytest.importorskip("wx")
        from ficary import gui

        asked = []
        monkeypatch.setattr(gui.MainFrame, "_start_story_check_at_launch", lambda self: asked.append(True))
        app = wx.GetApp() or wx.App()
        frame = gui.MainFrame()
        try:
            assert asked == [True]
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            del app

    def test_the_result_is_one_line(self, window, monkeypatch, tmp_path):
        monkeypatch.setattr(launch_check, "check_for_new_chapters", lambda roots, cancel_event: 3)
        window._run_story_check([tmp_path])
        assert logged(window).strip() == launch_check.waiting_summary(3)

    def announced(self, monkeypatch):
        from ficary import announce

        said = []
        monkeypatch.setattr(announce, "announce", lambda text: said.append(text))
        return said

    def test_new_chapters_are_said_by_the_screen_reader_too(self, window, monkeypatch, tmp_path):
        """A line in the status pane is easy to miss at launch."""
        said = self.announced(monkeypatch)
        monkeypatch.setattr(launch_check, "check_for_new_chapters", lambda roots, cancel_event: 3)
        window._run_story_check([tmp_path])
        assert said == [launch_check.waiting_summary(3)]

    def test_nothing_waiting_is_not_said(self, window, monkeypatch, tmp_path):
        said = self.announced(monkeypatch)
        monkeypatch.setattr(launch_check, "check_for_new_chapters", lambda roots, cancel_event: 0)
        window._run_story_check([tmp_path])
        assert said == []
        assert logged(window).strip() == launch_check.waiting_summary(0)

    def test_not_said_when_announcements_are_switched_off(self, window, monkeypatch, tmp_path):
        said = self.announced(monkeypatch)
        window.values[prefs_module.KEY_ANNOUNCE_FINISHED] = False
        monkeypatch.setattr(launch_check, "check_for_new_chapters", lambda roots, cancel_event: 3)
        window._run_story_check([tmp_path])
        assert said == []

    def test_a_check_with_nothing_to_report_says_nothing(self, window, monkeypatch, tmp_path):
        monkeypatch.setattr(launch_check, "check_for_new_chapters", lambda roots, cancel_event: None)
        window._run_story_check([tmp_path])
        assert logged(window) == ""

    def test_a_check_that_fails_says_nothing_in_the_status_pane(self, window, monkeypatch, tmp_path, caplog):
        def broken(roots, cancel_event):
            raise RuntimeError("the index is unreadable")

        monkeypatch.setattr(launch_check, "check_for_new_chapters", broken)
        window._run_story_check([tmp_path])
        assert logged(window) == ""
        assert "Checking the library for new chapters failed" in caplog.text

    def test_stop_stops_it(self, window):
        window._on_stop()
        assert window._story_check_cancel.is_set()

    def test_the_library_windows_update_stops_it(self, window, monkeypatch, tmp_path):
        from ficary.library import gui as library_gui

        monkeypatch.setattr(library_gui.threading, "Thread", lambda *a, **k: type("NotStarted", (), {"start": lambda self: None})())
        library = library_gui.LibraryFrame(window, window.prefs)
        try:
            monkeypatch.setattr(library, "_current_path", lambda: tmp_path)
            library._on_check_updates(None)
            assert window._story_check_cancel.is_set()
        finally:
            library._update_running = False
            library.Destroy()

    def test_closing_the_window_stops_it(self, monkeypatch):
        wx = pytest.importorskip("wx")
        from ficary.gui import MainFrame

        app = wx.GetApp() or wx.App()
        frame = MainFrame()
        cancel = frame._story_check_cancel
        frame._clip_timer.Stop()
        frame.Destroy()
        for _ in range(3):
            wx.SafeYield()
        assert cancel.is_set()
        del app


@needs_display
class TestPreferences:
    def test_the_setting_is_on_the_general_page_and_saved(self, monkeypatch):
        wx = pytest.importorskip("wx")
        from ficary.preferences import PreferencesDialog

        app = wx.GetApp() or wx.App()
        values = {prefs_module.KEY_CHECK_STORIES_AT_LAUNCH: True}

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
            assert dialog.check_stories_ctrl.GetLabel() == "Check the library for new c&hapters on launch"
            assert dialog.check_stories_ctrl.GetValue() is True
            dialog.check_stories_ctrl.SetValue(False)
            dialog._save()
            assert values[prefs_module.KEY_CHECK_STORIES_AT_LAUNCH] is False
            dialog.Destroy()
        finally:
            frame.Destroy()
            del app
