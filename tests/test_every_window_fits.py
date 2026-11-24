"""Every window and dialog shows all of its text and controls at its normal size: nothing cut off at the right-hand edge, squeezed, or below the bottom outside an area that scrolls.

From the visual walk of 2026-11-24 (tools/visual_walk.py, on Windows and Linux): instructions were cut off mid-sentence in five dialogs, Optional Features had its Install buttons past the right-hand edge and its log below the bottom with nothing to scroll, and the main window was taller than a 768-pixel laptop screen. A screen reader reads all of it either way; these are what a sighted helper sees. Linux squeezes what doesn't fit where Windows cuts it off, so the checks catch both.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")

# Controls whose text must be shown whole.
_TEXT_CONTROL_KINDS = ("StaticText", "Button", "CheckBox", "RadioButton")


def _settle(wx, rounds=8):
    for _ in range(rounds):
        wx.SafeYield()


def _scrolls(window, wx) -> bool:
    """Whether ``window`` is, or sits in, an area that scrolls."""
    while window is not None and not isinstance(window, wx.TopLevelWindow):
        if isinstance(window, wx.ScrolledWindow):
            return True
        window = window.GetParent()
    return False


def misfits(top, wx) -> list[str]:
    """What in the top-level window ``top`` doesn't show whole: squeezed, past the right-hand edge, or below the bottom outside a scrolled area."""
    origin = top.ClientToScreen(wx.Point(0, 0))
    client = top.GetClientSize()
    right_edge = origin.x + client.width
    bottom_edge = origin.y + client.height
    problems = []

    def look(window):
        for child in window.GetChildren():
            if isinstance(child, wx.TopLevelWindow) or not child.IsShown():
                continue
            kind = next((name for name in _TEXT_CONTROL_KINDS if isinstance(child, getattr(wx, name))), None)
            if kind is not None:
                label = " ".join(child.GetLabel().split())[:60]
                rect = child.GetScreenRect()
                if child.GetSize().width < child.GetBestSize().width - 2:
                    problems.append(f"{kind} squeezed: {label!r}")
                if not _scrolls(child, wx):
                    if rect.GetRight() > right_edge + 1:
                        problems.append(f"{kind} past the right-hand edge: {label!r}")
                    if rect.GetBottom() > bottom_edge + 1:
                        problems.append(f"{kind} below the bottom: {label!r}")
            look(child)

    look(top)
    return problems


def _notebook_in(window, wx):
    for child in window.GetChildren():
        if isinstance(child, wx.Notebook):
            return child
        found = _notebook_in(child, wx)
        if found is not None:
            return found
    return None


def all_misfits(top, wx) -> list[str]:
    """``misfits`` for the window, page by page when it has a notebook."""
    notebook = _notebook_in(top, wx)
    if notebook is None:
        return misfits(top, wx)
    problems = []
    for page in range(notebook.GetPageCount()):
        notebook.SetSelection(page)
        _settle(wx)
        problems += [f"{notebook.GetPageText(page)} page: {problem}" for problem in misfits(top, wx)]
    return problems


@pytest.fixture
def main_window(wx_app, tmp_path):
    import wx

    from ficary import gui
    from ficary import prefs as prefs_module
    from ficary.exporters import export_epub
    from ficary.library.scanner import scan
    from ficary.models import Chapter, Story

    root = tmp_path / "Fanfic"
    root.mkdir()
    books = []
    for number, title in enumerate(["The Long Road Home", "Lanterns at Low Tide"], 1):
        story = Story(id=number, title=title, author="Someone Writes", summary="A made-up story.", url=f"https://www.fanfiction.net/s/{number}/1/")
        story.metadata = {"status": "In-Progress", "category": "Original"}
        story.chapters = [Chapter(number=n, title=f"Chapter {n} - Somewhere new", html=f"<p>Chapter {n}.</p>") for n in range(1, 4)]
        books.append(Path(export_epub(story, str(root))))
    scan(root)
    frame = gui.MainFrame()
    frame.prefs.set(prefs_module.KEY_LIBRARY_PATH, str(root))
    frame.made_up_root = root
    frame.made_up_books = books
    yield frame
    frame._clip_timer.Stop()
    if frame._library_frame:
        frame._library_frame.Destroy()
    frame.Destroy()
    _settle(wx)


def _works():
    return [
        {"title": "The Long Road Home", "author": "Someone Writes", "words": "36,000", "chapters": "12", "rating": "T", "status": "In-Progress", "updated": "2026-11-20", "url": "https://www.fanfiction.net/s/1/1/", "section": "own"},
        {"title": "A Favourite of Theirs", "author": "Other Person", "words": "120,000", "chapters": "40", "rating": "M", "status": "Complete", "updated": "2025-03-02", "url": "https://www.fanfiction.net/s/9/1/", "section": "favorites"},
    ]


def _open(name, main):
    """The window called ``name``, made with made-up data."""
    from ficary import gui, gui_dialogs, gui_watchlist
    from ficary.gui_search import SearchFrame
    from ficary.library.browser import LibraryBrowserFrame
    from ficary.library.gui import AbandonedStoriesDialog, LibraryFrame, ReorganizePreviewDialog, ReviewDialog
    from ficary.library.index import LibraryIndex
    from ficary.library.reorganizer import MoveOp
    from ficary.preferences import PreferencesDialog
    from ficary.reader.gui import ReaderFrame, _BookmarksDialog
    from ficary.reader.source import StorySource
    from ficary.soundscape.editor import SoundscapeEditorDialog

    root, books = main.made_up_root, main.made_up_books
    voice_map = SimpleNamespace(mapping={}, save=lambda: None)
    makers = {
        "add story": lambda: main.add_story_frame,
        "library": lambda: LibraryFrame(main, main.prefs),
        "library browser": lambda: LibraryBrowserFrame(main, main.prefs),
        "reorganize preview": lambda: ReorganizePreviewDialog(main, root=root, moves=[MoveOp(source=books[0], target=root / "Original" / books[0].name, source_url="https://www.fanfiction.net/s/1/1/")]),
        "review ambiguous": lambda: ReviewDialog(main, idx=LibraryIndex.load(), root=root, untrackable=[{"relpath": "Misc/Some Old Download.epub", "title": "Some Old Download", "author": "Unknown"}]),
        "abandoned stories": lambda: AbandonedStoriesDialog(main, root),
        "preferences": lambda: PreferencesDialog(main, main.prefs, main_frame=main),
        "story picker": lambda: gui_dialogs.StoryPickerDialog(main, "Stories by Someone Writes", _works(), prefs=main.prefs),
        "multi picker": lambda: gui_dialogs.MultiPickerDialog(main, "Genres", ["Adventure", "Drama", "Fantasy"], initial=["Drama"]),
        "series parts": lambda: gui_dialogs.SeriesPartsDialog(main, "A Made-up Series", [{"title": "Part One", "author": "Someone Writes", "words": "12,000", "rating": "T", "url": "https://example.org/1"}]),
        "optional features": lambda: gui_dialogs.OptionalFeaturesDialog(main),
        "tts providers": lambda: gui_dialogs.TtsProvidersDialog(main, main.prefs),
        "ai settings": lambda: gui_dialogs.LlmSettingsDialog(main, main.prefs),
        "add from url list": lambda: gui_dialogs.AddFromUrlListDialog(main),
        "voice preview": lambda: gui_dialogs.VoicePreviewDialog(main, [{"name": "Harry", "gender": "male", "voice": "en-GB-RyanNeural", "count": 42}], voice_map, "en-US-AriaNeural"),
        "update progress": lambda: gui._UpdateProgressDialog(main, "v9.9.9", threading.Event()),
        "watchlist": lambda: gui_watchlist.WatchlistFrame(main),
        "add url watch": lambda: gui_watchlist.AddURLWatchDialog(main, gui_watchlist.WATCH_TYPE_STORY, channels=["Status pane", "Email"]),
        "add search watch": lambda: gui_watchlist.AddSearchWatchDialog(main, channels=["Status pane", "Email"]),
        "reader": lambda: ReaderFrame(main, main.prefs, StorySource.from_file(books[0], url="https://www.fanfiction.net/s/1/1/", title="The Long Road Home", author="Someone Writes")),
        "bookmarks": lambda: _BookmarksDialog(main, [SimpleNamespace(name="Where I stopped", chapter_number=3, excerpt="The wind came off the ridge")]),
        "soundscape editor": lambda: SoundscapeEditorDialog(main),
    }
    for _accelerator, site_key, spec_function, _label in main._SEARCH_MENU_ITEMS:
        makers[f"search {site_key}"] = lambda site_key=site_key, spec_function=spec_function: SearchFrame(main, site_key, spec_function())
    return makers[name]()


WINDOWS = [
    "add story", "library", "library browser", "reorganize preview", "review ambiguous", "abandoned stories",
    "preferences", "story picker", "multi picker", "series parts", "optional features", "tts providers",
    "ai settings", "add from url list", "voice preview", "update progress", "watchlist", "add url watch",
    "add search watch", "reader", "bookmarks", "soundscape editor",
    "search ffn", "search ao3", "search royalroad", "search wattpad", "search erotica",
]


@needs_display
@pytest.mark.parametrize("name", WINDOWS)
def test_everything_in_the_window_shows_whole(main_window, name):
    import wx

    window = _open(name, main_window)
    try:
        window.Show()
        window.Layout()
        _settle(wx)
        assert all_misfits(window, wx) == []
    finally:
        if window is main_window.add_story_frame:
            window.Hide()
        else:
            window.Destroy()
        _settle(wx)


@needs_display
def test_the_main_window_shows_whole(main_window):
    import wx

    main_window.Show()
    main_window.Layout()
    _settle(wx)
    assert misfits(main_window, wx) == []


@needs_display
def test_the_add_story_window_shows_whole_with_the_audiobook_settings(main_window):
    import wx

    window = main_window.add_story_frame
    choice = main_window.format_ctrl
    try:
        window.Show()
        choice.SetSelection(choice.FindString("audio"))
        main_window._update_audio_panel_visibility()
        _settle(wx)
        assert misfits(window, wx) == []
    finally:
        choice.SetSelection(choice.FindString("epub"))
        main_window._update_audio_panel_visibility()
        window.Hide()


@needs_display
def test_tabbing_to_the_last_install_button_brings_it_into_view(main_window):
    """Optional Features scrolls, so a sighted keyboard user sees each button Tab reaches."""
    import wx

    from ficary import gui_dialogs

    dialog = gui_dialogs.OptionalFeaturesDialog(main_window)
    try:
        dialog.Show()
        _settle(wx)
        last = list(dialog._buttons.values())[-1]
        last.SetFocus()
        _settle(wx)
        area = last.GetParent()
        assert isinstance(area, wx.ScrolledWindow)
        visible = wx.Rect(area.ClientToScreen(wx.Point(0, 0)), area.GetClientSize())
        assert visible.Contains(last.GetScreenRect())
    finally:
        dialog.Destroy()
        _settle(wx)


# ── The helpers ──────────────────────────────────────────────────


@needs_display
def test_a_wrapping_label_wraps_again_when_its_window_narrows(wx_app):
    import wx

    from ficary.gui_layout import WrappingText

    frame = wx.Frame(None, size=(700, 300))
    panel = wx.Panel(frame)
    text = "Pick a file, paste its source URL, and press Promote. The file moves to the library index's tracked list so Check for Updates can pick it up."
    label = WrappingText(panel, label=text)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(label, 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        frame.Show()
        _settle(wx)
        wide_lines = label.GetLabel().count("\n")
        frame.SetSize(320, 300)
        _settle(wx)
        assert label.GetLabel().count("\n") > wide_lines
        assert label.GetSize().width <= panel.GetClientSize().width
        assert label.unwrapped_label() == text
        assert " ".join(label.GetLabel().split()) == text, "wrapping only adds line breaks"
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_wrapping_label_wraps_text_set_later(wx_app):
    import wx

    from ficary.gui_layout import WrappingText

    frame = wx.Frame(None, size=(320, 300))
    panel = wx.Panel(frame)
    label = WrappingText(panel, label="")
    try:
        frame.Show()
        _settle(wx)
        later = "Edge TTS: Microsoft Edge Neural Voices via edge-tts. 0 voices available. The voice list couldn't be fetched."
        label.SetLabel(later)
        assert "\n" in label.GetLabel()
        assert " ".join(label.GetLabel().split()) == later
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_bigger_than_the_screen_is_shrunk_onto_it(wx_app):
    import wx

    from ficary.gui_layout import fit_on_screen

    frame = wx.Frame(None, pos=(300, 200), size=(900, 820))
    try:
        fit_on_screen(frame, area=wx.Rect(0, 0, 1366, 728))
        assert frame.GetRect() == wx.Rect(300, 0, 900, 728)
        fit_on_screen(frame, area=wx.Rect(0, 0, 1024, 728))
        assert frame.GetRect() == wx.Rect(124, 0, 900, 728)
    finally:
        frame.Destroy()


@needs_display
def test_a_window_that_fits_is_left_where_it_is(wx_app):
    import wx

    from ficary.gui_layout import fit_on_screen

    frame = wx.Frame(None, pos=(100, 50), size=(700, 500))
    try:
        fit_on_screen(frame, area=wx.Rect(0, 0, 1366, 728))
        assert frame.GetRect() == wx.Rect(100, 50, 700, 500)
    finally:
        frame.Destroy()


@needs_display
def test_the_library_window_lays_itself_out_without_waiting_to_be_resized(main_window):
    """Windows sends a window no size event when it is shown, so it has to lay itself out once built. The Library window's only size event came with its status bar, before its controls had a sizer, so on Windows every one of them sat on top of the others in the top left-hand corner (Linux lays a window out when it is shown, which hid it)."""
    from ficary.library.gui import LibraryFrame

    window = LibraryFrame(main_window, main_window.prefs)
    try:
        panel = window.scan_btn.GetParent()
        places = {child.GetPosition().Get() for child in panel.GetChildren()}
        assert len(places) > 1, "every control is still where it was made, on top of the others"
    finally:
        window.Destroy()

