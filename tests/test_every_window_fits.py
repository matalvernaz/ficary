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

# Controls whose text must be shown whole, and drop-downs, which must at
# least get the width they ask for (one given a fixed width shows a long
# item cut short, and its list shows it whole).
_TEXT_CONTROL_KINDS = ("StaticText", "Button", "CheckBox", "RadioButton", "Choice", "ComboBox", "SpinCtrl")

# Controls that scroll, so can be smaller than they would like, but must
# still show enough to use: three lines (a one-line box, one) and eight
# characters, or what the control itself asks for if that is less.
_SCROLLING_KINDS = ("ListBox", "ListCtrl", "TreeCtrl", "TextCtrl", "ScrolledWindow")


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
                label = " ".join(child.GetLabel().split())[:60] or child.GetName()
                rect = child.GetScreenRect()
                shortened = isinstance(child, wx.StaticText) and child.GetWindowStyleFlag() & (wx.ST_ELLIPSIZE_START | wx.ST_ELLIPSIZE_MIDDLE | wx.ST_ELLIPSIZE_END)
                # Text and buttons need the room their text takes, even
                # given a fixed size; a drop-down, the width it asks for.
                asked = child.GetEffectiveMinSize() if isinstance(child, (wx.Choice, wx.ComboBox)) else child.GetBestSize()
                if not shortened and child.GetSize().width < asked.width - 2:
                    problems.append(f"{kind} squeezed: {label!r}")
                if not _scrolls(child, wx):
                    if rect.GetRight() > right_edge + 1:
                        problems.append(f"{kind} past the right-hand edge: {label!r}")
                    if rect.GetBottom() > bottom_edge + 1:
                        problems.append(f"{kind} below the bottom: {label!r}")
            scrolling = next((name for name in _SCROLLING_KINDS if isinstance(child, getattr(wx, name))), None)
            # (Inside an area that scrolls, its contents get the room they ask for.)
            if scrolling is not None and not _scrolls(child.GetParent(), wx):
                name = child.GetName()
                one_line = isinstance(child, wx.TextCtrl) and not child.IsMultiLine()
                asks = child.GetEffectiveMinSize()
                least_width = min(8 * child.GetCharWidth(), asks.width)
                least_height = min(child.GetCharHeight() * (1 if one_line else 3), asks.height)
                size = child.GetSize()
                corner = child.GetScreenPosition()
                if size.width < least_width - 2 or size.height < least_height - 2:
                    problems.append(f"{scrolling} squeezed to {size.width}x{size.height}: {name!r}")
                elif corner.x + least_width > right_edge + 2 or corner.y + least_height > bottom_edge + 2:
                    problems.append(f"{scrolling} past the edge: {name!r}")
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
@pytest.mark.parametrize("name", WINDOWS + ["main window"])
def test_fitting_a_window_again_leaves_it_as_it_was(main_window, name):
    """Windows are fitted again whenever some of their contents change (a status line, the voice): a shortfall that growing can't cure would make one creep larger every time."""
    import wx

    from ficary.gui_layout import grow_to_fit

    window = main_window if name == "main window" else _open(name, main_window)
    try:
        window.Show()
        window.Layout()
        _settle(wx)
        before = window.GetRect()
        assert grow_to_fit(window) is False
        _settle(wx)
        assert window.GetRect() == before
    finally:
        if window is main_window.add_story_frame:
            window.Hide()
        elif window is not main_window:
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
        # What the toolkit sends a window when one of its children gets
        # the focus. Under a test display with no window manager, whether
        # it arrives on its own depends on the screen size.
        last.GetEventHandler().ProcessEvent(wx.ChildFocusEvent(last))
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
def test_the_library_window_lays_itself_out_without_waiting_to_be_resized(main_window, monkeypatch):
    """Adding a status bar sizes a window's panel at once; on Windows that came before the Library window's controls had a sizer, nothing sized the panel again until the window was resized, and every control sat on top of the others in the top left-hand corner. Linux sizes the panel again when the window is shown, so this does what Windows does at the status bar."""
    from ficary.library import gui as library_gui
    from ficary.library.gui import LibraryFrame

    real_status_bar = library_gui.add_activity_status_bar

    def status_bar_as_on_windows(frame):
        bar = real_status_bar(frame)
        panel = frame.GetChildren()[0]
        panel.SetSize(frame.GetClientSize())
        return bar

    monkeypatch.setattr(library_gui, "add_activity_status_bar", status_bar_as_on_windows)
    window = LibraryFrame(main_window, main_window.prefs)
    try:
        panel = window.scan_btn.GetParent()
        places = {child.GetPosition().Get() for child in panel.GetChildren()}
        assert len(places) > 1, "every control is still where it was made, on top of the others"
    finally:
        window.Destroy()



@needs_display
def test_the_main_and_add_story_windows_open_no_bigger_than_the_screen(wx_app, monkeypatch):
    """Both are fitted to the screen they open on. Here the screen is made 300 pixels tall, shorter than either."""
    import wx

    from ficary import gui, gui_layout

    real_fit = gui_layout.fit_on_screen
    monkeypatch.setattr(gui, "fit_on_screen", lambda window, area=None: real_fit(window, area=wx.Rect(0, 0, 1024, 300)))
    frame = gui.MainFrame()
    try:
        assert frame.GetSize().height <= 300
        assert frame.add_story_frame.GetSize().height <= 300
    finally:
        frame._clip_timer.Stop()
        frame.Destroy()
        _settle(wx)


@needs_display
def test_an_install_that_changes_a_features_labels_lays_them_out_again(main_window, monkeypatch):
    """A status or button label that grows (Install to Reinstall, a status to "(unsupported on this build)") was drawn at its old width, cut off."""
    import wx

    from ficary import gui_dialogs

    dialog = gui_dialogs.OptionalFeaturesDialog(main_window)
    try:
        dialog.Show()
        _settle(wx)
        feature = list(dialog._buttons)[0]
        monkeypatch.setattr(dialog, "_status_text", lambda feature: "(unsupported on this build, and a good deal longer than before)")
        monkeypatch.setattr(dialog, "_button_label", lambda feature: "&Reinstall this feature with a much longer label than it had...")
        dialog._refresh_feature_row(feature)
        _settle(wx)
        assert misfits(dialog, wx) == []
    finally:
        dialog.Destroy()
        _settle(wx)


@needs_display
def test_the_checker_finds_a_label_cut_off_and_a_button_past_the_edge(wx_app):
    """The checker every window test relies on: a label too long for its window, and a button placed past the right-hand edge."""
    import wx

    frame = wx.Frame(None, size=(240, 200))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(wx.StaticText(panel, label="A label far too long to fit in a window this narrow, cut off at its edge"), 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    wx.Button(panel, label="Past the edge", pos=(220, 120))
    try:
        frame.Show()
        _settle(wx)
        problems = misfits(frame, wx)
        assert any(problem.startswith("StaticText") for problem in problems)
        assert any(problem.startswith("Button past the right-hand edge") for problem in problems)
    finally:
        frame.Destroy()
        _settle(wx)


def test_a_voice_list_that_cant_be_fetched_is_said_plainly():
    """Offline, the TTS providers window showed the raw error, which named an SSL object at a memory address."""
    import socket
    import urllib.error

    from ficary.gui_dialogs import _voice_list_problem

    for failure in (ConnectionRefusedError("refused"), socket.gaierror("no such host"), urllib.error.URLError("down"), TimeoutError()):
        said = _voice_list_problem(failure)
        assert "no connection to the voice service" in said
        assert "Check the internet connection" in said
    aiohttp = pytest.importorskip("aiohttp")
    assert "no connection to the voice service" in _voice_list_problem(aiohttp.ClientConnectionError("Cannot connect to host speech.platform.bing.com:443 ssl:<ssl.SSLContext object at 0xffff68724dd0>"))
    other = _voice_list_problem(ValueError("the catalog's format changed"))
    assert "the catalog's format changed" in other
    assert "no connection" not in other


@needs_display
def test_the_add_story_window_is_not_a_long_strip(main_window):
    """Its option rows, the audiobook settings among them, wrap, so the window is an ordinary shape. With the audiobook settings on one line it opened 1,225 pixels wide and under 300 tall."""
    window = main_window.add_story_frame
    size = window.GetSize()
    # The shape, not the size: larger text makes the window larger.
    assert size.width < 3 * size.height, size


# ── Fitting a window to its contents (any text size) ────────────


@needs_display
def test_a_window_whose_contents_fit_keeps_its_size(wx_app):
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(500, 300))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(wx.Button(panel, label="Short"), 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        fit_to_contents(frame, (500, 300))
        assert frame.GetSize() == frame.FromDIP(wx.Size(500, 300))
    finally:
        frame.Destroy()


@needs_display
def test_a_list_and_a_text_box_squeezed_to_nothing_widen_and_lengthen_the_window(wx_app):
    """With larger text, Add saved-search watch squeezed its Channels list to nothing and the story picker its Find box to a sliver: they scroll, so they may be smaller than they would like, but not too small to use."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(300, 160))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.Add(wx.StaticText(panel, label="A label that takes up most of the row:"), 0, wx.RIGHT, 4)
    find = wx.TextCtrl(panel, name="Find")
    row.Add(find, 1)
    sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
    channels = wx.CheckListBox(panel, choices=["Status pane", "Email"], name="Channels")
    sizer.Add(channels, 1, wx.EXPAND | wx.ALL, 8)
    # Below the list, so the list isn't at the bottom edge: squeezed, it
    # still sits inside the window.
    for line in range(5):
        sizer.Add(wx.StaticText(panel, label=f"Line {line} of the instructions"), 0, wx.LEFT, 8)
    panel.SetSizer(sizer)
    try:
        frame.SetClientSize(frame.FromDIP(wx.Size(300, 160)))
        panel.Layout()
        assert find.GetSize().width < 8 * find.GetCharWidth(), "the test needs the box squeezed to begin with"
        assert channels.GetSize().height < 3 * channels.GetCharHeight(), "the test needs the list squeezed to begin with"
        fit_to_contents(frame, (300, 160))
        frame.Show()
        _settle(wx)
        assert misfits(frame, wx) == []
        assert find.GetSize().width >= 8 * find.GetCharWidth() - 2
        assert channels.GetSize().height >= 3 * channels.GetCharHeight() - 2
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_asked_to_be_bigger_than_the_screen_is_fitted_onto_it(wx_app):
    """Fitted at first even when nothing in it needs more room."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None)
    try:
        fit_to_contents(frame, (6000, 6000))
        area = wx.Display(0).GetClientArea()
        assert frame.GetSize().width <= area.width
        assert frame.GetSize().height <= area.height
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_shortfall_of_a_pixel_or_two_is_rounding_not_a_cut(wx_app):
    """At fractional text sizes, measurements round a pixel or two either way; each grew a window by a few pixels as if something were cut off."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(400, 200))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    button = wx.Button(panel, label="A button two pixels short")
    button.SetMinSize(wx.Size(button.GetBestSize().width - 2, -1))
    sizer.Add(button, 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        fit_to_contents(frame, (400, 200))
        assert frame.GetSize() == frame.FromDIP(wx.Size(400, 200))
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_list_made_small_on_purpose_is_left_small(wx_app):
    """A control kept small whatever the window's size can't be helped by growing the window, which would grow again every time it was fitted."""
    import wx

    from ficary.gui_layout import fit_to_contents, grow_to_fit

    frame = wx.Frame(None, size=(300, 200))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    small = wx.ListBox(panel, choices=["One", "Two", "Three", "Four"], name="Small on purpose")
    small.SetMinSize(wx.Size(40, 20))
    sizer.Add(small, 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        fit_to_contents(frame, (300, 200))
        frame.Show()
        _settle(wx)
        size = frame.GetSize()
        assert size == frame.FromDIP(wx.Size(300, 200))
        assert grow_to_fit(frame) is False
        assert frame.GetSize() == size
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_that_needs_no_more_room_is_left_where_it_was_put(wx_app):
    """Fitted again after its status changed, a window someone put partly off the screen stays there."""
    import wx

    from ficary.gui_layout import grow_to_fit

    frame = wx.Frame(None, size=(400, 300))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(wx.Button(panel, label="Short"), 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        frame.Show()
        _settle(wx)
        frame.SetPosition(wx.Point(-150, 40))
        _settle(wx)
        put = frame.GetPosition()
        assert put.x < 0, "the test needs the window partly off the screen"
        assert grow_to_fit(frame) is False
        _settle(wx)
        assert frame.GetPosition() == put
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_grows_by_what_would_be_cut_off_and_no_more(wx_app):
    """Sized to what everything in it would like, windows that already fitted grew; it grows only by what doesn't fit."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(300, 200))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = [wx.Button(panel, label=f"A button with a long label {n}") for n in range(3)]
    for button in buttons:
        row.Add(button, 0, wx.RIGHT, 6)
    sizer.Add(row, 0, wx.ALL, 8)
    sizer.Add(wx.ListCtrl(panel, style=wx.LC_REPORT), 1, wx.EXPAND | wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        fit_to_contents(frame, (300, 200))
        frame.Show()
        _settle(wx)
        assert misfits(frame, wx) == []
        needed = 8 + sum(button.GetBestSize().width + 6 for button in buttons) + 8
        border = frame.GetSize().width - frame.GetClientSize().width
        assert frame.GetSize().width <= needed + border + 3 * 8, "grown far past what the buttons need"
        assert frame.GetSize().height == frame.FromDIP(wx.Size(300, 200)).height, "the list can scroll; nothing needed more height"
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_with_pages_is_fitted_to_its_widest_page_once_shown(wx_app):
    """A notebook's pages have no size until the window is shown (on Linux), and only one shows at a time; the widest page is the one hidden here."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(300, 240))
    panel = wx.Panel(frame)
    notebook = wx.Notebook(panel)
    first = wx.Panel(notebook)
    first_sizer = wx.BoxSizer(wx.VERTICAL)
    first_sizer.Add(wx.CheckBox(first, label="Short"), 0, wx.ALL, 8)
    first.SetSizer(first_sizer)
    second = wx.Panel(notebook)
    second_sizer = wx.BoxSizer(wx.VERTICAL)
    wide = wx.CheckBox(second, label="A setting whose label runs on well past the width of this small window")
    second_sizer.Add(wide, 0, wx.ALL, 8)
    second.SetSizer(second_sizer)
    notebook.AddPage(first, "First")
    notebook.AddPage(second, "Second")
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(notebook, 1, wx.EXPAND | wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        fit_to_contents(frame, (300, 240))
        frame.Show()
        _settle(wx, 20)
        assert notebook.GetSelection() == 0, "the page showing is put back"
        notebook.SetSelection(1)
        _settle(wx)
        assert misfits(frame, wx) == []
        assert frame.GetSize().width <= wide.GetBestSize().width + 120, "grown far past the widest page"
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_measuring_a_windows_pages_puts_the_focus_back(wx_app, monkeypatch):
    """Each page of a notebook is shown in turn to measure it. Windows moves the focus out of a page it hides; left there, a screen reader would start somewhere else. (A test display with no window manager gives no window the focus, so the focus is played here.)"""
    import wx

    from ficary.gui_layout import grow_to_fit

    frame = wx.Frame(None, size=(400, 300))
    panel = wx.Panel(frame)
    notebook = wx.Notebook(panel)
    pages = []
    for title in ("First", "Second"):
        page = wx.Panel(notebook)
        page_sizer = wx.BoxSizer(wx.VERTICAL)
        page_sizer.Add(wx.TextCtrl(page, name=f"{title} field"), 0, wx.ALL, 8)
        page.SetSizer(page_sizer)
        notebook.AddPage(page, title)
        pages.append(page)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(notebook, 1, wx.EXPAND | wx.ALL, 8)
    panel.SetSizer(sizer)
    field = pages[0].GetChildren()[0]
    focus = {"on": field}
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: focus["on"]))
    real_change = wx.Notebook.ChangeSelection

    def change_and_move_the_focus(self, page):
        focus["on"] = self
        return real_change(self, page)

    monkeypatch.setattr(wx.Notebook, "ChangeSelection", change_and_move_the_focus)
    field.SetFocus = lambda: focus.update(on=field)
    try:
        frame.Show()
        _settle(wx)
        grow_to_fit(frame)
        assert focus["on"] is field
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_window_made_smaller_is_given_its_usual_size(wx_app):
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(200, 120))
    wx.Panel(frame)
    try:
        fit_to_contents(frame, (500, 300))
        assert frame.GetSize() == frame.FromDIP(wx.Size(500, 300))
    finally:
        frame.Destroy()


@needs_display
def test_a_long_folder_path_is_shortened_not_widening_the_window(main_window, tmp_path):
    """A path has no spaces to wrap at; it shortens in the middle, and a screen reader still reads it whole."""
    import wx

    from ficary.library.gui import AbandonedStoriesDialog

    deep = tmp_path.joinpath(*(f"a-rather-long-folder-name-{n}" for n in range(12)))
    deep.mkdir(parents=True)
    dialog = AbandonedStoriesDialog(main_window, deep)
    try:
        dialog.Show()
        _settle(wx)
        assert dialog.GetSize().width < 1000
        assert misfits(dialog, wx) == []
    finally:
        dialog.Destroy()
        _settle(wx)


@needs_display
def test_preferences_is_no_wider_than_its_widest_setting_needs(main_window):
    """Its pages are measured once it is shown; measured on a page not given its size yet, it grew to the width of the screen."""
    import wx

    from ficary.preferences import PreferencesDialog

    dialog = PreferencesDialog(main_window, main_window.prefs, main_frame=main_window)
    try:
        dialog.Show()
        _settle(wx, 20)
        widest = 0
        notebook = _notebook_in(dialog, wx)
        for page in range(notebook.GetPageCount()):
            for control in notebook.GetPage(page).GetChildren():
                if isinstance(control, wx.CheckBox):
                    widest = max(widest, control.GetBestSize().width)
        assert dialog.GetSize().width <= max(dialog.FromDIP(wx.Size(640, 0)).width, widest + 200)
    finally:
        dialog.Destroy()
        _settle(wx)


@needs_display
def test_controls_placed_past_an_edge_widen_and_lengthen_the_window(wx_app):
    """Windows doesn't squeeze what doesn't fit, as Linux does: it puts it past the window's edge. Placed there by hand here."""
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(300, 200))
    panel = wx.Panel(frame)
    right = wx.Button(panel, label="Right", pos=(340, 10))
    below = wx.Button(panel, label="Below", pos=(10, 260))
    # A list, a text box and a spin box need room too.
    wx.ListBox(panel, choices=["One", "Two"], pos=(700, 60), name="List placed right")
    wx.TextCtrl(panel, pos=(10, 320), name="Box placed below")
    wx.SpinCtrl(panel, pos=(380, 130), name="Spin box placed right")
    try:
        fit_to_contents(frame, (300, 200))
        client = frame.GetClientSize()
        assert client.width >= 340 + right.GetSize().width
        assert client.height >= 260 + below.GetSize().height
        frame.Show()
        _settle(wx)
        assert misfits(frame, wx) == []
    finally:
        frame.Destroy()
        _settle(wx)


@needs_display
def test_a_control_too_wide_for_an_area_that_scrolls_up_and_down_widens_the_window(wx_app):
    import wx

    from ficary.gui_layout import fit_to_contents

    frame = wx.Frame(None, size=(300, 200))
    panel = wx.Panel(frame)
    area = wx.ScrolledWindow(panel, style=wx.VSCROLL)
    area.SetScrollRate(0, 20)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(area, 1, wx.EXPAND)
    panel.SetSizer(sizer)
    wide = wx.Button(area, label="Too wide", pos=(330, 10))
    try:
        frame.Layout()
        fit_to_contents(frame, (300, 200))
        frame.Layout()
        assert area.GetClientSize().width >= 330 + wide.GetSize().width
    finally:
        frame.Destroy()


@needs_display
def test_a_button_given_a_fixed_width_too_small_for_its_label_is_found(wx_app):
    """A fixed 120 pixels cut "AI settings..." off inside its button once the text was larger; the button reported 120 as all it needed."""
    import wx

    frame = wx.Frame(None, size=(400, 200))
    panel = wx.Panel(frame)
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(wx.Button(panel, label="A label longer than its button", size=(60, -1)), 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    try:
        frame.Show()
        _settle(wx)
        assert any(problem.startswith("Button squeezed") for problem in misfits(frame, wx))
    finally:
        frame.Destroy()
        _settle(wx)


# ── Windows whose contents change after they open ────────────────
#
# Each window is fitted as it opens; one whose contents then grow (a
# backend chosen, a voice speaking) grows again to fit them.


@needs_display
def test_add_story_shows_whole_with_every_attribution_backend(main_window):
    """With BookNLP its Size drop-down appears, and with the AI backend its status names the model, which runs long; both were cut off."""
    import wx

    from ficary import prefs as prefs_module

    main_window.prefs.set(prefs_module.KEY_LLM_PROVIDER, "anthropic")
    main_window.prefs.set(prefs_module.KEY_LLM_MODEL, "claude-sonnet-4-5-20250929")
    window = main_window.add_story_frame
    choice = main_window.format_ctrl
    try:
        window.Show()
        choice.SetSelection(choice.FindString("audio"))
        main_window._update_audio_panel_visibility()
        for index, backend in enumerate(main_window._attribution_choices):
            main_window.attribution_ctrl.SetSelection(index)
            main_window._on_attribution_change(None)
            _settle(wx)
            assert misfits(window, wx) == [], backend
    finally:
        choice.SetSelection(choice.FindString("epub"))
        main_window._update_audio_panel_visibility()
        window.Hide()


@needs_display
def test_ai_settings_shows_whole_with_every_provider(main_window):
    """Ollama brings buttons of its own; "Download Ollama..." was cut off once the text was larger, in a dialog opened on another provider and measured without them."""
    import wx

    from ficary import gui_dialogs
    from ficary import prefs as prefs_module

    main_window.prefs.set(prefs_module.KEY_LLM_PROVIDER, "anthropic")
    dialog = gui_dialogs.LlmSettingsDialog(main_window, main_window.prefs)
    try:
        dialog.Show()
        for index in range(dialog.provider_ctrl.GetCount()):
            dialog.provider_ctrl.SetSelection(index)
            event = wx.CommandEvent(wx.EVT_CHOICE.typeId, dialog.provider_ctrl.GetId())
            event.SetEventObject(dialog.provider_ctrl)
            dialog.provider_ctrl.GetEventHandler().ProcessEvent(event)
            _settle(wx)
            assert misfits(dialog, wx) == [], dialog.provider_ctrl.GetString(index)
    finally:
        dialog.Destroy()
        _settle(wx)


@needs_display
def test_the_reader_shows_whole_once_a_voice_is_speaking(main_window):
    """"Voice: en-GB-RyanNeural (edge)" is longer than "Voice: not started", at the end of a row that was already full."""
    import wx

    from ficary.reader.gui import ReaderFrame
    from ficary.reader.source import StorySource

    reader = ReaderFrame(main_window, main_window.prefs, StorySource.from_file(main_window.made_up_books[0]))
    try:
        reader.Show()
        _settle(wx)
        reader._last_voice = "edge:en-GB-ThomasNeural-VeryLongVoiceName"
        reader._update_voice_status()
        _settle(wx)
        assert misfits(reader, wx) == []
    finally:
        reader.Destroy()
        _settle(wx)
