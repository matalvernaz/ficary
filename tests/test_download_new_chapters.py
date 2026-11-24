"""Two library commands and a launch check that goes by how active a story is.

Matt, 2026-11-24: the check at launch took most of an hour on a big
library, since it asked every site about every unfinished story every
day, at FanFiction.net's six seconds a story. Asking faster is what gets
a site's Cloudflare to start challenging every request, so the check now
asks less: a story updated in the last month is asked daily, in the last
six months weekly, older monthly. And downloading what is already known
to be waiting is its own command, Library > Download New Chapters
(Ctrl+Shift+N), apart from Check All Stories for Updates (Ctrl+U), which
asks the sites about everything.
"""

from __future__ import annotations

import os
import threading
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from ficary import cli
from ficary.library import launch_check
from ficary.library.index import LibraryIndex
from ficary.library.scanner import scan

from .library_fixtures import ficary_epub
from .test_new_chapters_at_launch import FakeSite, check, library, plug_in, url_of  # noqa: F401 - fixtures

DAY = 24 * 60 * 60
TODAY = date(2026, 11, 24)


# ── How often a story is asked at launch ─────────────────────────


class TestTheSchedule:
    @pytest.mark.parametrize(
        "updated, interval",
        [
            ("2026-11-19", DAY),            # five days ago: daily
            ("2026-10-25", DAY),            # thirty days ago: still daily
            ("2026-10-24", 7 * DAY),        # thirty-one: weekly
            ("2026-05-26", 7 * DAY),        # 182 days: still weekly
            ("2026-05-25", 30 * DAY),       # 183 days: monthly
            ("2024-01-01", 30 * DAY),       # years ago: monthly
            ("2026-11-19T10:00:00Z", DAY),  # a full timestamp counts by its date
            (None, DAY),                    # no date: daily, as before
            ("not a date", DAY),
        ],
    )
    def test_the_interval_goes_by_how_recently_it_updated(self, updated, interval):
        assert launch_check.recheck_interval_for(updated, today=TODAY) == interval

    def test_a_story_never_asked_about_is_due(self):
        now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        assert launch_check._due_at_launch({"story_updated": "2020-01-01"}, now=now)
        assert launch_check._due_at_launch(None, now=now)
        assert launch_check._due_at_launch({"last_probed": "garbage"}, now=now)

    def test_a_quiet_story_is_due_once_its_interval_has_passed(self):
        now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        quiet = {"story_updated": "2026-06-01", "last_probed": (now - timedelta(days=6)).isoformat()}
        assert not launch_check._due_at_launch(quiet, now=now)
        quiet["last_probed"] = (now - timedelta(days=7)).isoformat()
        assert launch_check._due_at_launch(quiet, now=now)


def set_index(index_path, root, changes: dict) -> None:
    index = LibraryIndex.load(index_path)
    stories = index.library_state(root)["stories"]
    for number, fields in changes.items():
        key = next(url for url in stories if f"/s/{number}" in url)
        stories[key].update(fields)
    index.save()


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


class TestTheLaunchCheckAsksLess:
    def test_only_stories_due_by_their_activity_are_asked(self, library, monkeypatch):
        root, index_path = library
        set_index(index_path, root, {
            101: {"story_updated": days_ago(5), "last_probed": ago(10)},    # active: daily
            102: {"story_updated": days_ago(100), "last_probed": ago(10)},  # quiet: weekly
            103: {"story_updated": days_ago(400), "last_probed": ago(10)},  # dormant: monthly
        })
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        check(root, index_path)
        assert sorted(site.asked) == sorted([url_of(101), url_of(102)])

    def test_three_days_on_only_the_active_story_is_asked(self, library, monkeypatch):
        root, index_path = library
        set_index(index_path, root, {
            101: {"story_updated": days_ago(5), "last_probed": ago(3)},
            102: {"story_updated": days_ago(100), "last_probed": ago(3)},
            103: {"story_updated": days_ago(400), "last_probed": ago(3)},
        })
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        check(root, index_path)
        assert site.asked == [url_of(101)]

    def test_a_dormant_story_waiting_already_is_still_counted(self, library, monkeypatch):
        """Known to have new chapters: counted without being asked again."""
        root, index_path = library
        set_index(index_path, root, {
            101: {"story_updated": days_ago(5), "last_probed": ago(3)},
            102: {"story_updated": days_ago(100), "last_probed": ago(3)},
            103: {"story_updated": days_ago(400), "last_probed": ago(3), "remote_chapter_count": 9},
        })
        site = FakeSite({url_of(101): 3, url_of(102): 2, url_of(103): 4})
        plug_in(monkeypatch, site)
        assert check(root, index_path) == 1
        assert url_of(103) not in site.asked


# ── Download New Chapters ────────────────────────────────────────

wx = pytest.importorskip("wx")


@pytest.fixture
def scanned_library(tmp_path):
    """Three unfinished stories in the default index (the library window's own)."""
    root = tmp_path / "library"
    root.mkdir()
    for number, chapters in ((101, 3), (102, 2), (103, 4)):
        ficary_epub(root, title=f"Story {number}", url=f"{url_of(number)}/1/", chapters=chapters, story_id=number)
    scan(root)
    return root.resolve()


def run_update(monkeypatch, root, *, only_waiting: bool):
    from ficary.library.gui import LibraryFrame

    queued: list[dict] = []
    monkeypatch.setattr(cli, "_run_update_queue", lambda queue, *args, **kwargs: queued.extend(queue))
    posted: list[str] = []
    window = types.SimpleNamespace(_post_status=posted.append)
    LibraryFrame._probe_one_root(
        window, root, force=False, refetch_all=False, recheck_interval=6 * 60 * 60,
        cancel_event=threading.Event(), only_waiting=only_waiting,
    )
    return queued, posted


class TestDownloadNewChapters:
    def test_only_the_stories_known_to_be_waiting_are_fetched_and_nothing_is_asked(self, scanned_library, monkeypatch):
        # 102 is overdue, so checking every story would ask its site;
        # 103 was checked a couple of hours ago, which that check lists
        # as "[skip] ... checked 2h ago".
        set_index(None, scanned_library, {
            101: {"remote_chapter_count": 5},
            102: {"last_probed": ago(2)},
            103: {"last_probed": ago(0.1)},
        })
        queued, posted = run_update(monkeypatch, scanned_library, only_waiting=True)
        assert [entry["url"] for entry in queued] == [next(e["url"] for e in queued if "/s/101" in e["url"])]
        # Every one already has its answer, so the queue asks no site.
        assert all("remote" in entry for entry in queued)
        assert not any("[skip]" in line for line in posted)

    def test_with_nothing_waiting_it_says_so_and_asks_nothing(self, scanned_library, monkeypatch):
        queued, posted = run_update(monkeypatch, scanned_library, only_waiting=False)
        queued_all = len(queued)
        queued, posted = run_update(monkeypatch, scanned_library, only_waiting=True)
        assert queued == []
        assert any("are known to have new chapters" in line and "Check All Stories for Updates (Ctrl+U)" in line for line in posted)
        assert queued_all == 3, "Check All Stories for Updates still asks about every story"

    def test_the_library_window_command_asks_for_only_waiting_and_nothing_else(self):
        from ficary.library.gui import LibraryFrame

        asked = {}
        window = types.SimpleNamespace(
            _update_running=False,
            _on_check_updates=lambda event, **options: asked.update(options),
        )
        window._on_download_new_chapters = lambda event: LibraryFrame._on_download_new_chapters(window, event)
        LibraryFrame.trigger_download_new_chapters(window)
        assert asked == {"force": False, "refetch_all": False, "only_waiting": True}


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")
class TestTheMenu:
    def library_menu_labels(self, frame) -> list[str]:
        bar = frame.GetMenuBar()
        menu = bar.GetMenu(bar.FindMenu("Library"))
        return [item.GetItemLabel() for item in menu.GetMenuItems()]

    def test_the_two_commands_are_on_the_library_menu(self, wx_app):
        from ficary import gui

        frame = gui.MainFrame()
        try:
            labels = self.library_menu_labels(frame)
            assert "Download &New Chapters\tCtrl+Shift+N" in labels
            assert "Check All Stories for &Updates\tCtrl+U" in labels
            assert labels.index("Download &New Chapters\tCtrl+Shift+N") < labels.index("Check All Stories for &Updates\tCtrl+U")
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_the_menu_command_opens_the_library_window_to_download_them(self, wx_app, monkeypatch):
        from ficary import gui

        frame = gui.MainFrame()
        try:
            asked = []
            monkeypatch.setattr(frame, "_on_library_menu", lambda event, **options: asked.append(options))
            frame._on_download_new_chapters(None)
            assert asked == [{"download_new_chapters": True}]
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()
