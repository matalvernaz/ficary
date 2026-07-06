"""Headless GUI smoke test.

Boots ``MainFrame`` plus the satellite frames (Search, Watchlist,
Library) under a wx ``App`` and tears them down. Catches the class
of regression that AST-level checks miss: a Bind() that referenced
a renamed handler, a menu item with an empty label, an event
handler that crashes during initial-state setup, a satellite frame
constructor whose signature drifted away from its caller.

Skips silently when:

* wxPython isn't installed (CI without GTK headers, or a CLI-only
  install). The test is a pure correctness signal — environments
  that can't render shouldn't fail it.
* ``DISPLAY`` isn't set (no X server, no xvfb-run wrapper). On
  Linux CI this means ``xvfb-run pytest`` or installing
  ``pytest-xvfb``; locally a workstation already has a display.
"""

from __future__ import annotations

import os
import sys

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "GUI smoke test needs a display server (run under xvfb-run "
        "or set DISPLAY=:0).",
        allow_module_level=True,
    )


def test_main_frame_constructs(wx_app):
    """MainFrame's __init__ wires every menu, toolbar, and bind in
    one shot — a regression that breaks any of those raises here."""
    from ficary import gui

    frame = gui.MainFrame()
    try:
        title = frame.GetTitle()
        assert "ficary" in title.lower()

        menubar = frame.GetMenuBar()
        assert menubar is not None
        assert menubar.GetMenuCount() >= 1

        # Every non-separator item must have a non-empty label —
        # an empty label means SetItemLabel was passed "" by mistake
        # (a renamed constant collapsing to None) and will read as
        # blank in NVDA.
        for i in range(menubar.GetMenuCount()):
            menu = menubar.GetMenu(i)
            for item in menu.GetMenuItems():
                if item.GetKind() == wx.ITEM_SEPARATOR:
                    continue
                label = item.GetItemLabelText() or item.GetItemLabel()
                assert label, (
                    f"empty label on menu '{menubar.GetMenuLabelText(i)}' "
                    f"item id={item.GetId()}"
                )
    finally:
        frame.Destroy()


def test_merge_series_snapshot_roundtrips(wx_app, monkeypatch):
    """The "combine series into one book" option lives in Preferences now
    (the round-10 declutter moved it off the main window), so the snapshot
    reads KEY_MERGE_SERIES from prefs rather than a checkbox.

    This also guards the snapshot's prefs-read path as a whole:
    ``_snapshot_download_params`` must ``import prefs as _p`` locally to
    reach KEY_MERGE_SERIES / KEY_FICHUB / the cookie keys. A missing import
    there raised NameError on every call, and because ``_on_download``
    snapshots before it branches, that silently killed *every* GUI download
    in 2.7.0-2.8.0 — the wx event handler swallows the traceback to stderr,
    which a windowed build has nowhere to show. Assert the snapshot both
    succeeds and reflects the pref.

    Monkeypatches ``get_bool`` rather than calling ``set_bool`` so the test
    doesn't write to the real on-disk prefs file.
    """
    from ficary.gui import MainFrame
    from ficary import prefs as _p

    frame = MainFrame()
    try:
        real_get_bool = frame.prefs.get_bool

        def get_bool_with(merge_value):
            def _fake(key, default=None):
                if key == _p.KEY_MERGE_SERIES:
                    return merge_value
                return real_get_bool(key, default)
            return _fake

        monkeypatch.setattr(frame.prefs, "get_bool", get_bool_with(False))
        assert frame._snapshot_download_params().merge_series is False
        monkeypatch.setattr(frame.prefs, "get_bool", get_bool_with(True))
        assert frame._snapshot_download_params().merge_series is True
    finally:
        frame.Destroy()


def test_show_update_dialog_helper_callable(wx_app):
    """``_show_update_dialog`` is the four-button update prompt
    introduced in 2.3.2. Confirms the helper survives import
    without actually showing the modal."""
    from ficary import gui

    assert callable(gui._show_update_dialog)


def test_search_frame_roundtrip(wx_app):
    """SearchFrame's constructor signature drifted away from its
    caller in past refactors — round-trip catches the next time."""
    from ficary.gui import MainFrame
    from ficary.gui_search import SearchFrame, _ffn_search_spec

    frame = MainFrame()
    try:
        sf = SearchFrame(frame, "ffn", _ffn_search_spec())
        sf.Destroy()
    finally:
        frame.Destroy()


def test_watchlist_frame_roundtrip(wx_app):
    """WatchlistFrame opens and closes without firing the poller."""
    from ficary.gui import MainFrame
    from ficary.gui_watchlist import WatchlistFrame

    frame = MainFrame()
    try:
        wf = WatchlistFrame(frame)
        wf.Destroy()
    finally:
        frame.Destroy()


def test_library_frame_roundtrip(wx_app):
    """LibraryFrame's prefs argument is required — a refactor that
    flips to lazy ``self.prefs`` in MainFrame would break this."""
    from ficary.gui import MainFrame
    from ficary.library.gui import LibraryFrame

    frame = MainFrame()
    try:
        lf = LibraryFrame(frame, frame.prefs)
        lf.Destroy()
    finally:
        frame.Destroy()


def test_announce_label_updates_label_and_accessible_name(wx_app):
    """``_announce_label`` mirrors the visible label into the MSAA
    accessible name. Without that mirror, NVDA on Windows reads the
    *initial* name forever — the user never hears the status flip
    from ``(not installed)`` to ``(installing...)`` to ``(installed)``
    that the sighted UI shows immediately.

    We verify the public observable (``GetName()`` matches the new
    label after the call) rather than the MSAA event itself, which
    is platform-internal."""
    from ficary.gui import _announce_label

    frame = wx.Frame(None)
    try:
        text = wx.StaticText(frame, label="(initial)")
        text.SetName("(initial)")
        _announce_label(text, "(installing...)")
        assert text.GetLabel() == "(installing...)"
        assert text.GetName() == "(installing...)"
        _announce_label(text, "(installed)")
        assert text.GetLabel() == "(installed)"
        assert text.GetName() == "(installed)"
    finally:
        frame.Destroy()


def test_idle_event_pumps_clean(wx_app):
    """Some MainFrame init paths schedule wx.CallAfter to populate
    the recent-files list, log pane, etc. Pumping idle once flushes
    those handlers in a controlled context so we catch any crash
    here instead of in production."""
    from ficary.gui import MainFrame

    frame = MainFrame()
    try:
        wx.SafeYield()
        frame.ProcessEvent(wx.IdleEvent())
    finally:
        frame.Destroy()


def test_add_from_url_list_dialog_constructs(wx_app):
    """The 2.4.0 bulk-import dialog has six interactive widgets
    (URL field, max-results spin, Extract button, list, three
    select-* buttons, OK/Cancel). Construction without a parent
    catches the class of regression where a Bind() targets a
    renamed handler — which on the GUI side never crashes the
    test suite, only production."""
    from ficary.gui import MainFrame
    from ficary.gui_dialogs import AddFromUrlListDialog

    frame = MainFrame()
    try:
        dlg = AddFromUrlListDialog(frame)
        try:
            assert dlg.GetTitle() == "Add from URL list"
            # OK is disabled until extraction populates the list
            assert not dlg.ok_btn.IsEnabled()
            # SetName landed on every widget the user tabs to —
            # NVDA reads the name when focus arrives, so a missing
            # name shows up as a blank announcement.
            assert dlg.url_ctrl.GetName()
            assert dlg.max_ctrl.GetName()
            assert dlg.list_ctrl.GetName()
            # picked_works on an unpopulated dialog returns []
            assert dlg.picked_works() == []
        finally:
            dlg.Destroy()
    finally:
        frame.Destroy()
