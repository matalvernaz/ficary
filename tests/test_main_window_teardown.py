"""A window that is destroyed leaves nothing running behind it.

Closing the main window tidies up in its close handler. Destroying it without closing skipped that, and its log-flush timer kept running with a deleted window as its owner. The next tick fired into freed memory: the test suite, which destroys windows that way, died with a bus error and then a segfault once enough windows had been made.

The status pane and the library list did have destroy handlers, but they asked ``event.GetEventObject() is self``, which never holds while a window is being torn down (wx hands back a different wrapper), so they never stopped anything either.
"""

from __future__ import annotations

import logging
import os

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip("builds real windows (run under xvfb-run -a)", allow_module_level=True)

from ficary.download_queue import DownloadQueues  # noqa: E402


def delete_pending_windows():
    """Top-level windows are deleted when the app next yields, not when Destroy() is called."""
    for _ in range(3):
        wx.SafeYield()


@pytest.fixture
def window(wx_app):
    from ficary.gui import MainFrame

    frame = MainFrame()
    frame._clip_timer.Start(2000)
    yield frame


class TestDestroyedWithoutClosing:
    def test_both_timers_stop(self, window):
        log_timer, clip_timer = window._log_timer, window._clip_timer
        assert log_timer.IsRunning() and clip_timer.IsRunning()
        window.Destroy()
        delete_pending_windows()
        try:
            assert not log_timer.IsRunning()
            assert not clip_timer.IsRunning()
        finally:
            # Without the fix, stop them here so this test cannot crash the rest of the run.
            log_timer.Stop()
            clip_timer.Stop()

    def test_its_log_handler_leaves_the_root_logger(self, window):
        handler = window._wx_log_handler
        assert handler in logging.getLogger().handlers
        window.Destroy()
        delete_pending_windows()
        try:
            assert handler not in logging.getLogger().handlers
        finally:
            logging.getLogger().removeHandler(handler)

    def test_it_stops_listening_to_the_download_queues(self, window):
        listener = window._on_site_queue_change
        assert listener in DownloadQueues._listeners
        window.Destroy()
        delete_pending_windows()
        try:
            assert listener not in DownloadQueues._listeners
        finally:
            DownloadQueues.remove_listener(listener)

    def test_a_child_window_going_away_does_not_stop_the_main_windows_timers(self, window):
        child = wx.Panel(window)
        child.Destroy()
        delete_pending_windows()
        try:
            assert window._log_timer.IsRunning()
        finally:
            window._clip_timer.Stop()
            window.Destroy()
            delete_pending_windows()

    def test_closing_normally_still_works_and_the_destroy_after_it_is_harmless(self, window, monkeypatch):
        from ficary import prefs as prefs_module

        real_get_bool = window.prefs.get_bool
        monkeypatch.setattr(
            window.prefs, "get_bool",
            lambda key, *args, **kwargs: False if key == prefs_module.KEY_CONFIRM_CANCEL_ON_CLOSE else real_get_bool(key, *args, **kwargs),
        )
        monkeypatch.setattr(window, "_save_prefs", lambda: None)
        log_timer = window._log_timer
        window.Close(force=True)
        delete_pending_windows()
        assert not log_timer.IsRunning()


class TestTheStatusPane:
    def test_its_timer_stops_when_its_window_goes(self, wx_app):
        from ficary.gui_status_log import StatusLogCtrl

        frame = wx.Frame(None)
        pane = StatusLogCtrl(frame)
        timer = pane._timer
        assert timer.IsRunning()
        frame.Destroy()
        delete_pending_windows()
        try:
            assert not timer.IsRunning()
        finally:
            timer.Stop()


class TestTellingTheWindowFromItsChildren:
    def test_the_check_matches_on_the_window_id(self):
        from ficary.gui_help import is_own_destroy

        class Event:
            def __init__(self, window_id):
                self.window_id = window_id

            def GetId(self):
                return self.window_id

        assert is_own_destroy(-2006, Event(-2006))
        assert not is_own_destroy(-2006, Event(-2007))

    def test_a_window_too_far_gone_to_answer_is_not_mistaken_for_this_one(self):
        from ficary.gui_help import is_own_destroy

        class Gone:
            def GetId(self):
                raise RuntimeError("wrapped C/C++ object of type Window has been deleted")

        assert not is_own_destroy(-2006, Gone())

    def test_the_old_check_really_does_fail_for_a_frame(self, wx_app):
        """Why the handlers compare ids: this is what they used to rely on."""
        frame = wx.Frame(None)
        seen = []
        frame.Bind(wx.EVT_WINDOW_DESTROY, lambda event: (seen.append(event.GetEventObject() is frame), event.Skip()))
        frame.Destroy()
        delete_pending_windows()
        assert seen and seen[0] is False
