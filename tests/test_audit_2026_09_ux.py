"""Regression tests for the 2026-09-08 audit's desktop findings.

Geometry and focus behaviour need a real toolkit, so the native cases
skip without a display the same way ``test_gui_smoke`` does.
"""
from __future__ import annotations

import os
import threading

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "These tests need a display server (run under xvfb-run).",
        allow_module_level=True,
    )


class _MemoryPrefs:
    def __init__(self, values=None):
        self._d = dict(values or {})

    def get(self, key, default=None):
        value = self._d.get(key)
        return value if value not in (None, "") else (default or "")

    def get_bool(self, key, default=False):
        return bool(self._d.get(key, default))

    def set(self, key, value):
        self._d[key] = value

    def set_bool(self, key, value):
        self._d[key] = bool(value)

    def flush(self):
        pass


# ── UX-01: preferences must survive a restart ──────────────────────

def test_a_colliding_native_config_path_falls_back_to_our_own_file(
    wx_app, tmp_path, monkeypatch,
):
    from ficary import portable, prefs as prefs_mod

    data_dir = tmp_path / ".ficary"
    data_dir.mkdir()
    monkeypatch.setattr(portable, "_cached_root", data_dir)
    monkeypatch.setattr(
        wx.FileConfig, "GetLocalFileName",
        staticmethod(lambda *a, **k: str(data_dir)),
    )

    first = prefs_mod.Prefs()
    first.set("audit_probe", "synthetic-value")
    assert first.last_save_error == ""

    second = prefs_mod.Prefs()
    assert second.get("audit_probe") == "synthetic-value", (
        "a setting must survive a new Prefs instance"
    )
    assert portable.settings_file().exists()


# ── UX-02: a closed search window must release the busy flag ───────

def test_closing_a_running_search_releases_the_app(wx_app):
    from unittest.mock import patch

    from ficary.gui import MainFrame
    from ficary.gui_search import SearchFrame, _ffn_search_spec

    started, release, finished = (threading.Event() for _ in range(3))

    def fake_search(*a, **k):
        started.set()
        assert release.wait(3)
        finished.set()
        return [], 2

    main = MainFrame()
    try:
        with patch("ficary.search.fetch_until_limit", fake_search):
            sf = SearchFrame(main, "ffn", _ffn_search_spec())
            sf.Show()
            sf.query_ctrl.SetValue("audit-only-query")
            sf._on_search()
            assert started.wait(2)
            assert main._global_busy is True
            sf.Close()
            for _ in range(10):
                wx.Yield()
            release.set()
            assert finished.wait(2)
            for _ in range(20):
                wx.Yield()
            assert main._global_busy is False, (
                "a closed search must not leave the app blocked"
            )
    finally:
        main.Destroy()


def test_a_stale_search_cannot_clear_a_newer_operation(wx_app):
    from ficary.gui import MainFrame

    main = MainFrame()
    try:
        stale, current = object(), object()
        main._set_busy(True, kind="search", owner=stale)
        main._set_busy(True, kind="download", owner=current)
        main._set_busy(False, owner=stale)
        assert main._global_busy is True
        main._set_busy(False, owner=current)
        assert main._global_busy is False
    finally:
        main.Destroy()


# ── UX-03: every preference field is reachable ─────────────────────

def test_credential_fields_have_height_and_scroll_into_view(wx_app):
    from ficary.preferences import PreferencesDialog

    frame = wx.Frame(None)
    dlg = PreferencesDialog(frame, _MemoryPrefs())
    try:
        dlg.Show()
        dlg.Layout()
        page = dlg.ao3_cookie_ctrl.GetParent()
        while page is not None and not hasattr(page, "setup_scrolling"):
            page = page.GetParent()
        assert page is not None, "credential fields must live on a scrolling page"
        assert page.GetVirtualSize().height > page.GetClientSize().height, (
            "the page must scroll rather than clip its last fields"
        )

        for name in (
            "ao3_cookie_ctrl", "ao3_user_agent_ctrl",
            "scribblehub_cookie_ctrl", "subscribestar_cookie_ctrl",
        ):
            ctrl = getattr(dlg, name)
            assert ctrl.GetSize().height > 0, f"{name} has no drawable height"
            # What SetupScrolling runs on focus. Calling it directly keeps
            # the check off the event loop, which is unstable to pump
            # repeatedly from a test process.
            page.ScrollChildIntoView(ctrl)
            offset = ctrl.GetPosition().y + page.CalcScrolledPosition(0, 0)[1]
            assert 0 <= offset < page.GetClientSize().height, (
                f"{name} is not scrolled into view when focused"
            )
    finally:
        dlg.Destroy()
        frame.Destroy()


# ── UX-04: search filters must fit ─────────────────────────────────

@pytest.mark.parametrize("site", ["ffn", "ao3", "royalroad"])
def test_search_windows_open_wide_enough_for_their_filters(wx_app, site):
    from ficary.gui import MainFrame
    from ficary import gui_search

    spec = {
        "ffn": gui_search._ffn_search_spec,
        "ao3": gui_search._ao3_search_spec,
        "royalroad": gui_search._royalroad_search_spec,
    }[site]()
    main = MainFrame()
    try:
        sf = gui_search.SearchFrame(main, site, spec)
        try:
            needed = sf.GetChildren()[0].GetSizer().GetMinSize()
            assert sf.GetSize().width >= needed.width, (
                "filters would be drawn outside the window"
            )
        finally:
            sf.Destroy()
    finally:
        main.Destroy()


# ── UX-06 / UX-07: connection tests use draft settings, off-thread ──

def test_fetch_libraries_runs_off_the_gui_thread_and_does_not_persist(
    wx_app, monkeypatch,
):
    from ficary import audiobookshelf, prefs as _p
    from ficary.preferences import PreferencesDialog

    prefs = _MemoryPrefs({
        _p.KEY_ABS_URL: "https://old.invalid",
        _p.KEY_ABS_TOKEN: "synthetic-old-token",
    })
    seen = {}
    done = threading.Event()

    def fake_list_libraries(cfg_prefs=None, **kw):
        seen["main_thread"] = threading.current_thread() is threading.main_thread()
        seen["url"] = cfg_prefs.get(_p.KEY_ABS_URL)
        seen["token"] = cfg_prefs.get(_p.KEY_ABS_TOKEN)
        done.set()
        return [{"id": "lib-1", "name": "Books", "folders": []}]

    monkeypatch.setattr(audiobookshelf, "list_libraries", fake_list_libraries)
    # Capture the completion callback rather than posting it: pumping the
    # event loop from a test while a worker posts into it is a wx
    # re-entrancy hazard, and the callback itself is exercised below by
    # calling it directly on the main thread.
    posted = []
    monkeypatch.setattr(
        "ficary.preferences.wx.CallAfter",
        lambda fn, *a, **k: posted.append((fn, a, k)),
    )

    frame = wx.Frame(None)
    dlg = PreferencesDialog(frame, prefs)
    try:
        dlg.abs_url_ctrl.SetValue("https://new.invalid")
        dlg.abs_token_ctrl.SetValue("synthetic-new-token")
        dlg._on_abs_fetch_libraries(None)
        assert done.wait(3)
        for _ in range(50):
            if posted:
                break
            threading.Event().wait(0.02)
        assert posted, "the worker must report back to the GUI thread"
        fn, args, kwargs = posted[-1]
        fn(*args, **kwargs)

        assert seen["main_thread"] is False, "the request blocked the event loop"
        assert dlg.abs_library_ctrl.GetCount() == 1
        assert dlg.abs_fetch_btn.IsEnabled()
        assert seen["url"] == "https://new.invalid", "must use the draft settings"
        assert seen["token"] == "synthetic-new-token"
        # Cancel: the dialog is closed without OK.
        assert prefs.get(_p.KEY_ABS_URL) == "https://old.invalid"
        assert prefs.get(_p.KEY_ABS_TOKEN) == "synthetic-old-token"
    finally:
        dlg.Destroy()
        frame.Destroy()


# ── UX-08: an update rewrites the file it was given ────────────────

def test_gui_update_rewrites_the_renamed_file(wx_app, tmp_path, monkeypatch):
    from unittest.mock import patch

    from ficary.gui import MainFrame
    from ficary.models import Chapter, Story

    book = tmp_path / "My renamed book.txt"
    book.write_text("placeholder", encoding="utf-8")

    fresh = Story(
        1, "Synthetic title", "Synthetic author", "",
        "https://www.fanfiction.net/s/1",
        chapters=[
            Chapter(1, "One", "<p>first</p>"),
            Chapter(2, "Two", "<p>second</p>"),
        ],
    )

    main = MainFrame()
    try:
        params = main._snapshot_download_params()
        from dataclasses import replace

        params = replace(params, fmt="txt", raw_output_dir=str(tmp_path))
        with patch.object(main, "_auto_index_download"):
            path = main._export_story(fresh, params, update_path=book)
        assert path == book, "the update must write the file it was given"
        assert list(tmp_path.glob("*.txt")) == [book], (
            "no second copy under the templated name"
        )
        assert "second" in book.read_text(encoding="utf-8")
    finally:
        main.Destroy()


# ── UX-09: the manual matches the shipped controls ─────────────────

def test_readme_getting_started_matches_the_menus(wx_app):
    """Every shortcut the manual teaches must exist in the menu bar."""
    from pathlib import Path

    from ficary.gui import MainFrame

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8",
    )
    main = MainFrame()
    try:
        bar = main.GetMenuBar()
        labels = []
        for i in range(bar.GetMenuCount()):
            for item in bar.GetMenu(i).GetMenuItems():
                if item.GetKind() != wx.ITEM_SEPARATOR:
                    labels.append(item.GetItemLabel())
        joined = " | ".join(labels)
    finally:
        main.Destroy()

    # The download form is behind Add Story, and the manual says so.
    assert "Add Story" in readme
    assert "Add Story" in joined and "Ctrl+D" in joined
    # Ctrl+Shift+L takes a listing URL, not a text file.
    assert "download a whole list of links from a text file" not in readme
    assert "URL list" in joined
    # The library folder is not set from Preferences.
    assert "library\nfolder in Preferences" not in readme
