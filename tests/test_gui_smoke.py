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


def test_erotica_site_change_scopes_tag_picker(wx_app):
    """Selecting a specific site re-scopes the tag picker to that site's
    searchable tags and drops picked tags it can't search."""
    from ficary.gui import MainFrame
    from ficary.gui_search import SearchFrame, _erotica_search_spec
    from ficary.erotica.search import tags_for_site

    frame = MainFrame()
    try:
        sf = SearchFrame(frame, "erotica", _erotica_search_spec())
        master = sf._erotica_tags_master
        femdom = next(lbl for lbl in master if lbl.startswith("femdom "))
        feet = next(lbl for lbl in master if lbl.startswith("feet "))
        # feet is not an MCStories tag; femdom is.
        sf.text_ctrls["tags"].SetValue(f"{femdom}, {feet}")
        sf.filter_ctrls["sites_choice"].SetStringSelection(
            "MCStories (mcstories)",
        )

        class _Evt:
            def Skip(self):
                pass

        sf._on_erotica_site_change(_Evt())

        assert len(sf.multi_options["tags"]) == len(tags_for_site("mcstories"))
        box = sf.text_ctrls["tags"].GetValue()
        assert "femdom" in box and "feet" not in box
        assert "MCStories" in sf.erotica_tag_status.GetLabel()
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


_PREFS_LABELED_FIELDS = [
    ("name_template_ctrl", "Default filename template:"),
    ("format_ctrl", "Default format:"),
    ("html_style_ctrl", "Default HTML layout:"),
    ("webnovel_cookie_ctrl", "Webnovel.com cookie:"),
    ("ao3_cookie_ctrl", "AO3 cookie:"),
    ("scribblehub_cookie_ctrl", "ScribbleHub cookie:"),
    ("subscribestar_cookie_ctrl", "SubscribeStar cookie:"),
    ("speech_rate_ctrl", "Default speech rate (%):"),
    ("attribution_ctrl", "Default attribution backend:"),
    ("attribution_size_ctrl", "Default model size (BookNLP only):"),
    ("abs_url_ctrl", "Server URL:"),
    ("abs_token_ctrl", "API token:"),
    ("abs_library_ctrl", "Library:"),
    ("abs_folder_ctrl", "Folder:"),
    ("pushover_token_ctrl", "Pushover API token:"),
    ("pushover_user_ctrl", "Pushover user key:"),
    ("discord_webhook_ctrl", "Discord webhook URL:"),
    ("notify_email_ctrl", "Notification email address:"),
    ("watch_interval_ctrl", "Poll interval:"),
    ("log_level_ctrl", "Log level:"),
]


def test_preferences_msaa_label_association(wx_app):
    """Every labeled Preferences field must have ITS OWN StaticText as
    the nearest preceding sibling in creation order, with no focusable
    control in between.

    This models how MSAA on Windows infers an edit/combo's accessible
    name: walk backward through the parent's children for the nearest
    StaticText, giving up if another interactive control intervenes.
    The old row helper created each control BEFORE its label, so NVDA
    read every field with the previous row's label (or a help
    paragraph) and the first field of each tab as unlabeled — exactly
    what the user's speech-viewer capture showed."""
    import wx
    from ficary.gui import MainFrame
    from ficary.preferences import PreferencesDialog

    interactive = (wx.TextCtrl, wx.Choice, wx.Button, wx.CheckBox, wx.SpinCtrl)
    frame = MainFrame()
    dlg = PreferencesDialog(frame, frame.prefs)
    try:
        for attr, expected in _PREFS_LABELED_FIELDS:
            ctrl = getattr(dlg, attr)
            siblings = list(ctrl.GetParent().GetChildren())
            idx = next(
                i for i, w in enumerate(siblings) if w is ctrl
            )
            label = None
            for w in reversed(siblings[:idx]):
                if isinstance(w, wx.StaticText):
                    label = w.GetLabelText()
                    break
                if isinstance(w, interactive):
                    break
            assert label == expected, (
                f"{attr}: MSAA would read label {label!r}, "
                f"expected {expected!r}"
            )
    finally:
        dlg.Destroy()
        frame.Destroy()


def test_pasted_literotica_series_url_merge_downloads(wx_app, monkeypatch, tmp_path):
    """A pasted /series/se/<id> Literotica URL must route through the
    series-merge path end to end: batch detection, series listing,
    per-part download, merge, export. Network layers are stubbed with
    shapes verified live; everything from _run_download down is real."""
    import wx
    from ficary.gui import MainFrame
    from ficary import prefs as _p
    from ficary.models import Story, Chapter
    from ficary.erotica.literotica import LiteroticaScraper

    url = "https://www.literotica.com/series/se/216601253"

    def fake_series(self, u):
        return "You Stupid Slut", [
            f"https://www.literotica.com/s/you-stupid-slut-pt-{i:02d}"
            for i in (1, 2, 3)
        ]

    def fake_download(self, u, progress_callback=None, **kw):
        n = u.rsplit("-", 1)[-1]
        return Story(
            id=int(n), title=f"You Stupid Slut Pt. {n}", author="a",
            summary="", url=u,
            chapters=[Chapter(number=1, title=f"Pt {n}", html="<p>x</p>")],
        )

    monkeypatch.setattr(
        LiteroticaScraper, "scrape_series_works", fake_series)
    monkeypatch.setattr(LiteroticaScraper, "download", fake_download)

    frame = MainFrame()
    logs = []
    try:
        monkeypatch.setattr(frame, "_log", lambda m: logs.append(str(m)))
        frame.output_ctrl.SetValue(str(tmp_path))
        real_get_bool = frame.prefs.get_bool
        monkeypatch.setattr(
            frame.prefs, "get_bool",
            lambda k, d=False: True if k == _p.KEY_MERGE_SERIES
            else real_get_bool(k, d),
        )
        assert frame._is_batch_url(url) is True
        params = frame._snapshot_download_params()
        assert params.merge_series is True
        frame._run_download(url, params=params)
    finally:
        frame.Destroy()

    joined = "\n".join(logs)
    assert "Series: You Stupid Slut" in joined, joined
    saved = [ln for ln in logs if ln.startswith("Saved:")]
    assert saved, f"series merge never saved a file; log was:\n{joined}"
    exports = list(tmp_path.rglob("*.epub")) + list(tmp_path.rglob("*.html"))
    assert exports, "no exported file on disk"


def test_add_story_window_hosts_download_form(wx_app):
    """The download form lives in a persistent, hidden Add Story window
    so the main window is just the library. Opening shows it; closing
    hides (never destroys) it, so every reference into its controls —
    _snapshot_download_params, prefs sync — stays valid."""
    from ficary.gui import MainFrame
    frame = MainFrame()
    try:
        asf = frame.add_story_frame
        assert not asf.IsShown()  # hidden at startup
        # Form controls are parented to the Add Story panel, not the
        # main window's root panel.
        assert frame.url_ctrl.GetParent() is frame._add_story_panel
        assert frame.output_ctrl.GetParent() is frame._add_story_panel
        assert frame.format_ctrl.GetParent() is frame._add_story_panel

        frame._on_add_story()
        assert asf.IsShown()  # opens on demand
        # The snapshot still reads the relocated controls.
        params = frame._snapshot_download_params()
        assert params.fmt in ("epub", "html", "txt", "audio")

        asf.Close()
        assert not asf.IsShown()       # close hides...
        assert bool(frame.url_ctrl)    # ...but the controls survive
        frame._on_add_story()          # and it reopens fine
        assert asf.IsShown()
    finally:
        frame.Destroy()


def test_add_story_stays_open_for_picker_urls(wx_app, monkeypatch, tmp_path):
    """Author/bookmark URLs answer the Download click with a picker
    dialog. Dismissing the Add Story window at click time hid the
    focused window while the picker was opening, which could strand
    keyboard focus on the library list behind the modal — a
    screen-reader user heard the form vanish and never found the story
    list. The window must stay up until the picker resolves; plain
    story URLs and series URLs (no picker) still dismiss immediately."""
    from ficary.gui import MainFrame

    frame = MainFrame()
    try:
        frame.output_ctrl.SetValue(str(tmp_path))
        # Neutralise the flows behind the decision point: no network,
        # no busy-state bookkeeping between the three calls.
        monkeypatch.setattr(frame, "_run_download", lambda *a, **k: None)
        monkeypatch.setattr(frame, "_enqueue_site_job", lambda *a, **k: None)
        monkeypatch.setattr(frame, "_set_busy", lambda *a, **k: None)

        # Plain story URL: enqueued, window dismissed at click.
        frame._on_add_story()
        frame.url_ctrl.SetValue("https://www.fanfiction.net/s/1234/1/Some-Story")
        frame._on_download(None)
        assert not frame.add_story_frame.IsShown()

        # Series URL: batch fan-out with no picker, dismissed at click.
        frame._on_add_story()
        frame.url_ctrl.SetValue("https://archiveofourown.org/series/123456")
        frame._on_download(None)
        assert not frame.add_story_frame.IsShown()

        # Author page: picker flow — the window must survive the click.
        frame._on_add_story()
        frame.url_ctrl.SetValue("https://www.fanfiction.net/u/1234567/Some-Author")
        frame._on_download(None)
        assert frame.add_story_frame.IsShown()
    finally:
        frame.Destroy()


def test_scraper_for_use_cache_false(wx_app):
    """Audit #1 (GUI path): a fresh-copy re-pull threads use_cache=False
    through _scraper_for so cached chapters aren't served stale."""
    from ficary.gui import MainFrame
    frame = MainFrame()
    try:
        url = "https://www.fanfiction.net/s/12345/1/"
        assert frame._scraper_for(url).use_cache is True
        assert frame._scraper_for(url, use_cache=False).use_cache is False
    finally:
        frame.Destroy()


def test_hide_add_story_refocuses_library(wx_app):
    """Audit #3: dismissing the Add Story window (Escape/close) hides it
    AND refocuses the library list, and a non-vetoable close (forced
    shutdown) is allowed through rather than blocked."""
    from ficary.gui import MainFrame
    frame = MainFrame()
    try:
        frame._on_add_story()
        assert frame.add_story_frame.IsShown()
        # Escape/menu path: event is None → hide + refocus.
        frame._hide_add_story()
        assert not frame.add_story_frame.IsShown()

        # A user-initiated (vetoable) close hides, keeps the frame alive.
        frame._on_add_story()

        class _Evt:
            def __init__(self, can): self._c = can; self.vetoed = False; self.skipped = False
            def CanVeto(self): return self._c
            def Veto(self): self.vetoed = True
            def Skip(self): self.skipped = True

        ev = _Evt(True)
        frame._hide_add_story(ev)
        assert ev.vetoed and not ev.skipped
        assert not frame.add_story_frame.IsShown()

        # A forced shutdown (CanVeto False) must be let through.
        ev2 = _Evt(False)
        frame._hide_add_story(ev2)
        assert ev2.skipped and not ev2.vetoed
    finally:
        frame.Destroy()


def test_library_window_scan_refreshes_embedded_list(wx_app, monkeypatch):
    """A scan/update run from the separate Library window must reload the
    main window's embedded list, or that list keeps showing its startup
    snapshot (blank story_updated, missing new stories) — which reads as
    "the update-date sort doesn't work" because the column is empty."""
    from ficary.gui import MainFrame
    from ficary.library.gui import LibraryFrame

    frame = MainFrame()
    lib = LibraryFrame(frame, frame.prefs)
    try:
        calls = {"n": 0}
        monkeypatch.setattr(
            frame, "_refresh_library_panel",
            lambda: calls.__setitem__("n", calls["n"] + 1),
        )
        # Scan completion notifies the main window.
        lib._scan_finished([])
        assert calls["n"] == 1
        # Update completion does too.
        lib._update_finished()
        assert calls["n"] == 2
        # And closing the window is a backstop refresh.
        frame._notify_library_frame_closed()
        assert calls["n"] == 3
    finally:
        lib.Destroy()
        frame.Destroy()


def test_no_default_save_location(wx_app):
    """Save-to starts empty — no hardcoded ~/Downloads default (which on
    frozen builds landed inside the portable app folder)."""
    from ficary.gui import MainFrame
    frame = MainFrame()
    try:
        assert frame.output_ctrl.GetValue() == ""
    finally:
        frame.Destroy()


def test_require_save_target_uses_library_without_prompting(wx_app, monkeypatch):
    """With a library configured, _require_save_target seeds Save-to from
    it and returns True — no folder-picker modal."""
    from ficary.gui import MainFrame
    from ficary import prefs as _p
    frame = MainFrame()
    try:
        real = frame.prefs.get
        monkeypatch.setattr(
            frame.prefs, "get",
            lambda k, d=None: "/tmp" if k == _p.KEY_LIBRARY_PATH else real(k, d),
        )
        frame.output_ctrl.SetValue("")
        assert frame._require_save_target() is True
        assert frame.output_ctrl.GetValue() == "/tmp"
    finally:
        frame.Destroy()


def test_require_save_target_respects_explicit_output(wx_app):
    """An explicit Save-to short-circuits — no library needed, no modal."""
    from ficary.gui import MainFrame
    frame = MainFrame()
    try:
        frame.output_ctrl.SetValue("/tmp/staging")
        assert frame._require_save_target() is True
    finally:
        frame.Destroy()


def test_update_covers_main_and_adult_roots(wx_app):
    """Check-for-Updates probes the separate adult root, not just the
    main library. _update_roots is the seam that decides which roots the
    per-root probe loop visits."""
    import tempfile
    from pathlib import Path
    from ficary.gui import MainFrame
    from ficary.library.gui import LibraryFrame

    frame = MainFrame()
    lf = LibraryFrame(frame, frame.prefs)
    try:
        main = tempfile.mkdtemp()
        adult = tempfile.mkdtemp()
        lf.path_ctrl.SetValue(main)
        lf.adult_path_ctrl.SetValue(adult)
        roots = [str(r) for r in lf._update_roots(Path(main))]
        assert str(Path(main)) in roots
        assert str(Path(adult)) in roots

        # No adult root configured → main only, no phantom entry.
        lf.adult_path_ctrl.SetValue("")
        assert lf._update_roots(Path(main)) == [Path(main)]

        # Adult path equal to main (or a non-existent dir) isn't added twice.
        lf.adult_path_ctrl.SetValue(main)
        assert lf._update_roots(Path(main)) == [Path(main)]
    finally:
        lf.Destroy()
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
