"""Headless tests for the library browser and the separate adult-root
routing on the GUI side.

Skips (like test_gui_smoke) when wxPython isn't installed or no display is
available — run under ``xvfb-run pytest`` on Linux CI.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "GUI tests need a display server (run under xvfb-run).",
        allow_module_level=True,
    )

from ficary import prefs as _p
from ficary.library.index import SCHEMA_VERSION, LibraryIndex

_LIT = "https://www.literotica.com/s/spicy"
_FFN = "https://www.fanfiction.net/s/1/"
_AO3 = "https://archiveofourown.org/works/2"


class _StubPrefs:
    """Minimal prefs stand-in: only the adult-path key matters to the
    browser's row loading and filtering."""

    def __init__(self, adult_root: str = ""):
        self._adult = adult_root

    def get(self, key, default=None):
        if key == _p.KEY_LIBRARY_ADULT_PATH:
            return self._adult
        return "" if default is None else default

    def get_bool(self, key, default=False):
        return default

    def set(self, *args):
        pass


def _install_index(monkeypatch, main_root: Path, adult_root: Path) -> None:
    """Point LibraryIndex.load at a hand-built two-root index."""
    data = {
        "version": SCHEMA_VERSION,
        "libraries": {
            str(main_root): {
                "stories": {
                    _FFN: {
                        "relpath": "HP/One - A.epub", "title": "One",
                        "author": "A", "fandoms": ["Harry Potter"],
                        "format": "epub", "adapter": "ffn",
                    },
                    _AO3: {
                        "relpath": "Naruto/Two - B.epub", "title": "Two",
                        "author": "B", "fandoms": ["Naruto"],
                        "format": "epub", "adapter": "ao3",
                    },
                },
                "untrackable": [],
            },
            str(adult_root): {
                "stories": {
                    _LIT: {
                        "relpath": "Spicy - C.epub", "title": "Spicy",
                        "author": "C", "fandoms": ["Adult"],
                        "format": "epub", "adapter": "literotica",
                    },
                },
                "untrackable": [],
            },
        },
    }
    fake = LibraryIndex(main_root / "idx.json", data)
    monkeypatch.setattr(
        LibraryIndex, "load", classmethod(lambda cls, path=None: fake),
    )


def test_browser_hides_adult_until_toggled(wx_app, monkeypatch, tmp_path):
    from ficary.library.browser import LibraryBrowserFrame

    main_root = tmp_path / "lib"
    adult_root = tmp_path / "adult"
    main_root.mkdir()
    adult_root.mkdir()
    _install_index(monkeypatch, main_root, adult_root)

    parent = wx.Frame(None)
    frame = LibraryBrowserFrame(parent, _StubPrefs(str(adult_root)))
    try:
        assert len(frame._rows) == 3
        # Adult hidden by default: the Literotica story is out.
        visible = {r.title for r in frame._visible}
        assert visible == {"One", "Two"}
        assert frame._adult_hidden == 1

        # Toggle reveals it.
        frame.adult_chk.SetValue(True)
        frame._apply_filter()
        assert {r.title for r in frame._visible} == {"One", "Two", "Spicy"}

        # Search filters (case-insensitive, across title/author/fandom).
        frame.search_ctrl.SetValue("naruto")
        frame._apply_filter()
        assert [r.title for r in frame._visible] == ["Two"]

        # The adult row is flagged and labelled from the separate root.
        frame.search_ctrl.SetValue("")
        frame._apply_filter()
        spicy = next(r for r in frame._rows if r.title == "Spicy")
        assert spicy.is_adult is True
        assert spicy.library_label == "Adult"
    finally:
        frame.Destroy()
        parent.Destroy()


def _select_by_title(frame, title):
    """Select the visible row with ``title`` and return it, so the
    per-story action handlers (which read the list selection) operate on
    a known story."""
    for i, row in enumerate(frame._visible):
        if row.title == title:
            frame.list_ctrl.Select(i)
            frame.list_ctrl.Focus(i)
            return row
    raise AssertionError(f"{title!r} not in the visible rows")


def _entry(root, url):
    """Fetch an index entry the way the browser reads it — by exact
    stored key from ``stories_in`` — rather than ``lookup_by_url``, which
    canonicalises the URL and so wouldn't match the raw keys this test's
    hand-built fixture uses."""
    for stored_url, entry in LibraryIndex.load().stories_in(root):
        if stored_url == url:
            return entry
    return None


def test_browser_adult_override_toggles(wx_app, monkeypatch, tmp_path):
    """Mark Adult / Mark Not Adult writes an explicit override onto the
    index entry that wins over the site/folder-derived guess, both ways."""
    from ficary.library.browser import LibraryBrowserFrame

    main_root = tmp_path / "lib"
    adult_root = tmp_path / "adult"
    main_root.mkdir()
    adult_root.mkdir()
    _install_index(monkeypatch, main_root, adult_root)
    monkeypatch.setattr(LibraryIndex, "save", lambda self: None)

    parent = wx.Frame(None)
    frame = LibraryBrowserFrame(parent, _StubPrefs(str(adult_root)))
    try:
        # "One" is a plain FFN story — derived non-adult.
        one = _select_by_title(frame, "One")
        assert one.is_adult is False and one.adult_overridden is False

        frame._on_toggle_adult_flag(None)  # mark adult
        entry = _entry(main_root, _FFN)
        assert entry["adult"] is True
        # With Show adult off, it drops out of the view.
        assert "One" not in {r.title for r in frame._visible}

        # Reveal it, confirm the override is reflected, then clear it.
        frame.adult_chk.SetValue(True)
        frame._apply_filter()
        one = _select_by_title(frame, "One")
        assert one.is_adult is True and one.adult_overridden is True

        frame._on_toggle_adult_flag(None)  # mark not adult
        entry = _entry(main_root, _FFN)
        assert entry["adult"] is False
        one = _select_by_title(frame, "One")
        assert one.is_adult is False and one.adult_overridden is True
    finally:
        frame.Destroy()
        parent.Destroy()


def test_browser_adult_override_beats_adult_site(wx_app, monkeypatch, tmp_path):
    """A false positive can be corrected: an override of False hides the
    [adult] flag even on a story the site-based rule would call adult."""
    from ficary.library.browser import LibraryBrowserFrame

    main_root = tmp_path / "lib"
    adult_root = tmp_path / "adult"
    main_root.mkdir()
    adult_root.mkdir()
    _install_index(monkeypatch, main_root, adult_root)
    monkeypatch.setattr(LibraryIndex, "save", lambda self: None)

    parent = wx.Frame(None)
    frame = LibraryBrowserFrame(parent, _StubPrefs(str(adult_root)))
    try:
        frame.adult_chk.SetValue(True)
        frame._apply_filter()
        spicy = _select_by_title(frame, "Spicy")  # Literotica → derived adult
        assert spicy.is_adult is True

        frame._on_toggle_adult_flag(None)  # override to not-adult
        entry = _entry(adult_root, _LIT)
        assert entry["adult"] is False
        spicy = _select_by_title(frame, "Spicy")
        assert spicy.is_adult is False and spicy.adult_overridden is True
    finally:
        frame.Destroy()
        parent.Destroy()


def test_browser_abandoned_toggle(wx_app, monkeypatch, tmp_path):
    """Mark Abandoned / Revive flips the index abandoned_at flag on the
    selected story and the row reflects it."""
    from ficary.library.browser import LibraryBrowserFrame

    main_root = tmp_path / "lib"
    adult_root = tmp_path / "adult"
    main_root.mkdir()
    adult_root.mkdir()
    _install_index(monkeypatch, main_root, adult_root)
    monkeypatch.setattr(LibraryIndex, "save", lambda self: None)

    parent = wx.Frame(None)
    frame = LibraryBrowserFrame(parent, _StubPrefs(str(adult_root)))
    try:
        two = _select_by_title(frame, "Two")
        assert two.is_abandoned is False

        frame._on_toggle_abandoned(None)  # mark abandoned
        entry = _entry(main_root, _AO3)
        assert entry.get("abandoned_at")
        two = _select_by_title(frame, "Two")
        assert two.is_abandoned is True
        # Button relabels to the reverse action for the selected row.
        assert "Revive" in frame.abandon_btn.GetLabel()

        frame._on_toggle_abandoned(None)  # revive
        entry = _entry(main_root, _AO3)
        assert "abandoned_at" not in entry
        two = _select_by_title(frame, "Two")
        assert two.is_abandoned is False
        assert "Mark" in frame.abandon_btn.GetLabel()
    finally:
        frame.Destroy()
        parent.Destroy()


def test_browser_reexport_roundtrip(wx_app, tmp_path):
    from ficary.exporters import export_epub
    from ficary.library.browser import _reexport_file
    from ficary.models import Chapter, Story

    story = Story(
        id=0, title="RT", author="Auth", summary="", url="https://x/y",
        chapters=[Chapter(number=1, title="Ch1", html="<p>Hello.</p>")],
    )
    epub_path = Path(export_epub(story, str(tmp_path)))
    assert epub_path.exists()

    for fmt in ("txt", "html"):
        out = Path(_reexport_file(epub_path, fmt, str(tmp_path)))
        assert out.exists() and out.stat().st_size > 0
        assert out.suffix == f".{fmt}"


def test_main_frame_opens_and_tracks_browser(wx_app):
    from ficary.gui import MainFrame

    frame = MainFrame()
    try:
        frame._open_library_browser()
        assert frame._browser_frame is not None
        # Re-opening focuses the existing one, doesn't stack a second.
        first = frame._browser_frame
        frame._open_library_browser()
        assert frame._browser_frame is first
        frame._browser_frame.Close()
        assert frame._browser_frame is None
    finally:
        frame.Destroy()


def test_gui_resolve_output_dir_routes_adult_to_separate_root(
    wx_app, monkeypatch, tmp_path,
):
    """The crux of the separate-adult-root feature on the GUI download
    path: an inside-library save of a Literotica story lands in the
    configured adult root, not <library>/Adult."""
    from ficary.gui import MainFrame
    from ficary.models import Story

    lib = tmp_path / "lib"
    adult = tmp_path / "adult"
    lib.mkdir()
    adult.mkdir()

    frame = MainFrame()
    try:
        real_get = frame.prefs.get

        def fake_get(key, default=None):
            if key == _p.KEY_LIBRARY_PATH:
                return str(lib)
            if key == _p.KEY_LIBRARY_ADULT_PATH:
                return str(adult)
            return real_get(key, default)

        monkeypatch.setattr(frame.prefs, "get", fake_get)

        params = replace(
            frame._snapshot_download_params(),
            raw_output_dir=str(lib),  # saving into the library root
            fmt="epub",
        )
        lit_story = Story(
            id=1, title="S", author="C", summary="", url=_LIT,
        )
        dest = frame._resolve_output_dir(lit_story, params)
        assert Path(dest).resolve() == adult.resolve()
        # (The FFN-not-hijacked case is covered without a GUI in
        # test_adult_library_root.py — exercising it here would trip the
        # first-time-fandom-folder modal, which can't be answered headless.)
    finally:
        frame.Destroy()
