"""The batched status pane: queued from any thread, written once per
tick, trimmed to a ceiling.

Skips (like test_gui_smoke) when wxPython isn't installed or no display
is available — run under ``xvfb-run pytest`` on Linux CI.
"""

from __future__ import annotations

import os
import threading

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "GUI tests need a display server (run under xvfb-run).",
        allow_module_level=True,
    )

from ficary.gui_status_log import StatusLogCtrl


@pytest.fixture
def pane(wx_app):
    parent = wx.Frame(None)
    ctrl = StatusLogCtrl(parent)
    try:
        yield ctrl
    finally:
        parent.Destroy()


def test_posts_wait_for_a_flush_then_land_as_lines(pane):
    pane.post_line("one")
    pane.post_line("two   ")
    assert pane.GetValue() == "", "nothing is written per post"
    assert pane.pending_count() == 2
    pane.flush()
    assert pane.GetValue() == "one\ntwo\n"
    assert pane.pending_count() == 0
    # An empty flush is a no-op, not an empty append.
    pane.flush()
    assert pane.GetValue() == "one\ntwo\n"


def test_worker_threads_post_directly_without_marshalling(pane):
    def worker(tag):
        for i in range(200):
            pane.post_line(f"{tag} {i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pane.pending_count() == 400
    pane.flush()
    lines = pane.GetValue().splitlines()
    assert len(lines) == 400
    assert sorted(lines)[0].split()[0] in ("a", "b")


def test_trims_to_the_configured_ceiling(wx_app):
    parent = wx.Frame(None)
    ctrl = StatusLogCtrl(parent, max_lines=20, trim_to_lines=10)
    try:
        for i in range(30):
            ctrl.post_line(f"line {i}")
        ctrl.flush()
        lines = ctrl.GetValue().splitlines()
        assert len(lines) <= 20
        assert lines[-1] == "line 29", "the newest lines survive"
        assert "line 0" not in lines, "the oldest lines are the ones dropped"
    finally:
        parent.Destroy()


def test_rejects_a_trim_target_at_or_above_the_ceiling(wx_app):
    parent = wx.Frame(None)
    try:
        with pytest.raises(ValueError):
            StatusLogCtrl(parent, max_lines=10, trim_to_lines=10)
    finally:
        parent.Destroy()


def test_is_read_only_and_does_not_wrap(pane):
    style = pane.GetWindowStyleFlag()
    assert style & wx.TE_READONLY
    assert style & wx.TE_MULTILINE
    assert style & wx.TE_DONTWRAP
