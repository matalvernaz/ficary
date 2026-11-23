"""Tests that every way of starting a download first makes sure there is somewhere to save.

Pressing Download asked for a library folder when none was set. Two other ways in did not: the clipboard watch, and Show Parts in a search window. With no library and an empty Save to box, their downloads were written into whichever folder the program happened to be running from, which for the Windows download is the program's own folder.
"""

from __future__ import annotations

import os

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "these tests build real windows (run under xvfb-run -a)",
        allow_module_level=True,
    )

from ficary import prefs as prefs_module  # noqa: E402

STORY_URL = "https://www.fanfiction.net/s/12345/1/Some-Story"


class FolderPicker:
    """Stands in for the folder picker. ``answer`` is the folder the user chose, or None for Cancel."""

    answer = None
    shown = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ShowModal(self):
        type(self).shown += 1
        return wx.ID_OK if type(self).answer else wx.ID_CANCEL

    def GetPath(self):
        return type(self).answer


@pytest.fixture
def frame(wx_app, monkeypatch):
    """A main window with no library configured, an empty Save to box, and downloads captured instead of run."""
    from ficary.gui import MainFrame

    window = MainFrame()
    library = {"path": ""}
    real_get = window.prefs.get

    def get(key, default=None):
        if key == prefs_module.KEY_LIBRARY_PATH:
            return library["path"]
        return real_get(key, default)

    def set_value(key, value):
        if key == prefs_module.KEY_LIBRARY_PATH:
            library["path"] = value

    monkeypatch.setattr(window.prefs, "get", get)
    monkeypatch.setattr(window.prefs, "set", set_value)
    window.output_ctrl.SetValue("")
    window.started = []
    monkeypatch.setattr(
        window, "_enqueue_site_job",
        lambda url, job, **options: window.started.append(url),
    )
    FolderPicker.answer = None
    FolderPicker.shown = 0
    monkeypatch.setattr(wx, "DirDialog", FolderPicker)
    window.library = library
    yield window
    window._clip_timer.Stop()
    window.Destroy()


class TestQuietCheck:
    def test_nothing_set_means_not_ready(self, frame):
        assert frame._save_target_ready() is False
        assert FolderPicker.shown == 0

    def test_a_library_makes_it_ready_and_fills_in_save_to(self, frame):
        frame.library["path"] = "/tmp/library"
        assert frame._save_target_ready() is True
        assert frame.output_ctrl.GetValue() == "/tmp/library"

    def test_a_save_to_folder_makes_it_ready(self, frame):
        frame.output_ctrl.SetValue("/tmp/staging")
        assert frame._save_target_ready() is True


class TestClipboardWatch:
    def turn_on(self, frame):
        frame.watch_btn.SetValue(True)
        frame._on_watch_toggle(None)

    def test_turning_it_on_asks_where_to_save(self, frame):
        FolderPicker.answer = "/tmp/chosen"
        self.turn_on(frame)
        assert FolderPicker.shown == 1
        assert frame._watching is True
        assert frame.library["path"] == "/tmp/chosen"

    def test_cancelling_the_question_leaves_the_watch_off(self, frame):
        self.turn_on(frame)
        assert FolderPicker.shown == 1
        assert frame._watching is False
        assert frame.watch_btn.GetValue() is False
        assert frame._clip_timer.IsRunning() is False

    def test_it_does_not_ask_when_a_folder_is_already_set(self, frame):
        frame.library["path"] = "/tmp/library"
        self.turn_on(frame)
        assert FolderPicker.shown == 0
        assert frame._watching is True

    def copy_link(self, frame, monkeypatch):
        """Put a story link on the clipboard and let the watch notice it."""
        monkeypatch.setattr(frame, "_get_clipboard", lambda: STORY_URL)
        frame._last_clip = ""
        frame._on_clip_timer(None)

    def test_a_copied_link_is_not_downloaded_with_nowhere_to_save(
        self, frame, monkeypatch,
    ):
        """The Save to folder can be emptied after the watch has started. The timer cannot ask, so it must refuse."""
        frame.library["path"] = "/tmp/library"
        self.turn_on(frame)
        frame.library["path"] = ""
        frame.output_ctrl.SetValue("")
        self.copy_link(frame, monkeypatch)
        assert frame.started == []
        assert FolderPicker.shown == 0

    def test_a_refused_link_can_be_copied_again_later(self, frame, monkeypatch):
        frame.library["path"] = "/tmp/library"
        self.turn_on(frame)
        frame.library["path"] = ""
        frame.output_ctrl.SetValue("")
        self.copy_link(frame, monkeypatch)
        frame.library["path"] = "/tmp/library"
        self.copy_link(frame, monkeypatch)
        assert len(frame.started) == 1

    def test_a_copied_link_is_downloaded_when_there_is_a_folder(
        self, frame, monkeypatch,
    ):
        frame.library["path"] = "/tmp/library"
        self.turn_on(frame)
        self.copy_link(frame, monkeypatch)
        assert len(frame.started) == 1


class TestShowParts:
    def pick_a_part(self, frame, monkeypatch):
        """Open a search window, choose a part of a series in Show Parts, and close the window again."""
        from ficary import gui_search

        class PartsDialog:
            def __init__(self, *args, **kwargs):
                pass

            def ShowModal(self):
                return wx.ID_OK

            def picked_url(self):
                return STORY_URL

            def Destroy(self):
                pass

        monkeypatch.setattr(gui_search, "SeriesPartsDialog", PartsDialog)
        window = gui_search.SearchFrame(frame, "ffn", gui_search._ffn_search_spec())
        try:
            window.results = [{
                "title": "A Series", "is_series": True,
                "series_parts": [{"title": "Part 1", "url": STORY_URL}],
            }]
            monkeypatch.setattr(window.results_ctrl, "GetFirstSelected", lambda: 0)
            window._on_show_parts()
        finally:
            window.Destroy()

    def test_it_asks_where_to_save_first(self, frame, monkeypatch):
        FolderPicker.answer = "/tmp/chosen"
        self.pick_a_part(frame, monkeypatch)
        assert FolderPicker.shown == 1
        assert len(frame.started) == 1

    def test_cancelling_the_question_downloads_nothing(self, frame, monkeypatch):
        self.pick_a_part(frame, monkeypatch)
        assert FolderPicker.shown == 1
        assert frame.started == []

    def test_it_just_downloads_when_a_folder_is_set(self, frame, monkeypatch):
        frame.library["path"] = "/tmp/library"
        self.pick_a_part(frame, monkeypatch)
        assert FolderPicker.shown == 0
        assert len(frame.started) == 1
