"""A watchlist poll that has to wait for another one says so.

Only one poll runs at a time. The background poll can hold that turn for minutes when it downloads what it finds, and a poll asked for by hand in the Watchlist window then sat on "Polling..." with nothing to say why.
"""

from __future__ import annotations

import threading

import pytest

from ficary import watchlist
from ficary.watchlist import WatchlistStore, run_once


class _Prefs:
    def get(self, key, default=None):
        return default


def wait_for(event: threading.Event, seconds: float = 5.0) -> bool:
    return event.wait(seconds)


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "watchlist.json")


class TestRunOnce:
    def test_a_poll_with_nothing_else_running_does_not_say_it_waited(self, store):
        waits = []
        assert run_once(store, _Prefs(), on_wait=lambda: waits.append("waited")) == []
        assert waits == []

    def test_a_poll_that_has_to_wait_says_so_once_and_then_runs(self, store):
        waiting = threading.Event()
        waits = []
        results = []

        def on_wait():
            waits.append("waited")
            waiting.set()

        def poll():
            results.append(run_once(store, _Prefs(), on_wait=on_wait))

        assert watchlist._RUN_ONCE_LOCK.acquire(timeout=5)
        try:
            thread = threading.Thread(target=poll, daemon=True)
            thread.start()
            assert wait_for(waiting), "the waiting poll never said it was waiting"
            # It is told before the wait, and it really is waiting: nothing has come back yet.
            thread.join(0.2)
            assert thread.is_alive()
            assert results == []
        finally:
            watchlist._RUN_ONCE_LOCK.release()
        thread.join(5)
        assert not thread.is_alive()
        assert results == [[]]
        assert waits == ["waited"]

    def test_a_poll_can_be_asked_for_without_a_callback_and_still_waits_its_turn(self, store):
        results = []
        assert watchlist._RUN_ONCE_LOCK.acquire(timeout=5)
        try:
            thread = threading.Thread(target=lambda: results.append(run_once(store, _Prefs())), daemon=True)
            thread.start()
            thread.join(0.2)
            assert thread.is_alive()
        finally:
            watchlist._RUN_ONCE_LOCK.release()
        thread.join(5)
        assert results == [[]]

    def test_a_callback_that_raises_does_not_stop_the_poll(self, store):
        called = threading.Event()
        results = []

        def on_wait():
            called.set()
            raise RuntimeError("the window has gone")

        assert watchlist._RUN_ONCE_LOCK.acquire(timeout=5)
        try:
            thread = threading.Thread(target=lambda: results.append(run_once(store, _Prefs(), on_wait=on_wait)), daemon=True)
            thread.start()
            assert wait_for(called)
        finally:
            watchlist._RUN_ONCE_LOCK.release()
        thread.join(5)
        assert results == [[]]

    def test_the_turn_is_given_up_when_a_poll_fails(self, store, monkeypatch):
        def broken():
            raise RuntimeError("the store cannot be listed")

        monkeypatch.setattr(store, "all", broken)
        with pytest.raises(RuntimeError, match="cannot be listed"):
            run_once(store, _Prefs())
        assert watchlist._RUN_ONCE_LOCK.acquire(blocking=False), "the failed poll kept the turn"
        watchlist._RUN_ONCE_LOCK.release()


class _Label:
    def __init__(self):
        self.text = "Polling all enabled watches..."

    def SetLabel(self, text):
        self.text = text


class _Window:
    """Stands in for the Watchlist window: just what the two methods under test read."""

    def __init__(self, *, alive=True, polling=True):
        self._alive = alive
        self._polling = polling
        self.status_ctrl = _Label()
        self.prefs = _Prefs()
        self.finished = []

    def _on_poll_waiting(self):
        """Only its identity matters: the test checks this is what the poll is given."""

    def _show_poll_waiting(self):
        """Only its identity matters: the test checks this is what gets posted to the window's thread."""

    def _on_poll_done(self, results, fatal_error, label):
        self.finished.append((results, fatal_error, label))


class TestTheWatchlistWindow:
    @pytest.fixture
    def gui_watchlist(self):
        return pytest.importorskip("ficary.gui_watchlist")

    def test_the_status_line_says_the_poll_is_waiting(self, gui_watchlist):
        window = _Window()
        gui_watchlist.WatchlistFrame._show_poll_waiting(window)
        assert window.status_ctrl.text == gui_watchlist.POLL_WAITING_MESSAGE
        assert "already running" in window.status_ctrl.text

    def test_a_late_message_does_not_overwrite_the_result_of_a_finished_poll(self, gui_watchlist):
        window = _Window(polling=False)
        window.status_ctrl.text = "Poll complete (all enabled watches): no changes."
        gui_watchlist.WatchlistFrame._show_poll_waiting(window)
        assert window.status_ctrl.text == "Poll complete (all enabled watches): no changes."

    def test_a_closed_window_is_left_alone(self, gui_watchlist, monkeypatch):
        window = _Window(alive=False)
        posted = []
        monkeypatch.setattr(gui_watchlist.wx, "CallAfter", lambda *call: posted.append(call))
        gui_watchlist.WatchlistFrame._on_poll_waiting(window)
        assert posted == []
        gui_watchlist.WatchlistFrame._show_poll_waiting(window)
        assert window.status_ctrl.text == "Polling all enabled watches..."

    def test_the_wait_is_handed_to_the_window_thread(self, gui_watchlist, monkeypatch):
        """The callback runs on the poll's own thread, where touching the window is not allowed."""
        window = _Window()
        posted = []
        monkeypatch.setattr(gui_watchlist.wx, "CallAfter", lambda *call: posted.append(call))
        gui_watchlist.WatchlistFrame._on_poll_waiting(window)
        assert posted == [(window._show_poll_waiting,)]
        assert window.status_ctrl.text == "Polling all enabled watches..."

    def test_a_poll_asked_for_in_the_window_passes_the_callback(self, gui_watchlist, monkeypatch, tmp_path):
        window = _Window(alive=False)
        seen = {}

        def fake_run_once(store, prefs, **options):
            seen.update(options)
            return []

        monkeypatch.setattr(gui_watchlist, "run_once", fake_run_once)
        monkeypatch.setattr(
            gui_watchlist.WatchlistStore, "load_default",
            classmethod(lambda cls: WatchlistStore(tmp_path / "watchlist.json")),
        )
        gui_watchlist.WatchlistFrame._run_poll_worker(window, None, "all enabled watches")
        assert seen["on_wait"] == window._on_poll_waiting
