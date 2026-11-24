"""Opens every Ficary window and dialog with a made-up library, at its normal size, and saves a screenshot of each, plus the list of its controls in Tab order. For looking at what a sighted helper sees, on Windows (run by the Visual walk workflow) as well as on Linux, and for checking that a layout change leaves what a screen reader hears alone.

Usage: python tools/visual_walk.py <output folder>

No network: every connection attempt fails at once, so nothing is fetched and nothing waits. Settings and the library index go under the output folder, never into the real ones.
"""

import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))


def _no_network(*args, **kwargs):
    raise OSError("no network during the visual walk")


# Block the network before anything Ficary imports can open a connection.
socket.socket.connect = _no_network
socket.create_connection = _no_network

output = Path(sys.argv[1]).resolve()
shots = output / "shots"
controls = output / "controls"
shots.mkdir(parents=True, exist_ok=True)
controls.mkdir(parents=True, exist_ok=True)

from ficary import portable  # noqa: E402

# Settings, logs and the library index live in the output folder.
portable._cached_root = output / "data"
portable._cached_root.mkdir(parents=True, exist_ok=True)

import wx  # noqa: E402

from ficary import gui  # noqa: E402
from ficary import prefs as prefs_module  # noqa: E402
from ficary.exporters import export_epub  # noqa: E402
from ficary.library.scanner import scan  # noqa: E402
from ficary.models import Chapter, Story  # noqa: E402

# A made-up library of three stories, one of them finished.
library_root = output / "Fanfic"
library_root.mkdir(parents=True, exist_ok=True)
books = []
made_up_stories = [
    ("The Long Road Home", "Harry Potter", "In-Progress", 12),
    ("Lanterns at Low Tide", "Original", "Complete", 5),
    ("Stars Over the Harbour", "Star Wars", "In-Progress", 30),
]
for number, (title, fandom, status, chapter_count) in enumerate(made_up_stories, 1):
    story_id = 1000 + number
    story = Story(
        id=story_id, title=title, author="Someone Writes",
        summary="A made-up story for looking at the windows.",
        url=f"https://www.fanfiction.net/s/{story_id}/1/",
    )
    story.metadata = {"status": status, "category": fandom, "rating": "T", "genre": "Drama"}
    paragraph = "The wind came off the ridge and the riders kept low. " * 30
    story.chapters = [
        Chapter(number=chapter, title=f"Chapter {chapter} - Somewhere new", html=f"<p>Chapter {chapter} of {title}. {paragraph}</p>")
        for chapter in range(1, chapter_count + 1)
    ]
    books.append(Path(export_epub(story, str(library_root))))
scan(library_root)

# The main window asks questions and checks the library at start; neither belongs in a walk.
gui.MainFrame._library_questions_at_start = lambda self: None
gui.MainFrame._start_story_check_at_launch = lambda self: None
gui.MainFrame._get_clipboard = lambda self: ""

app = wx.App(False)
main_window = gui.MainFrame()
main_window.prefs.set(prefs_module.KEY_LIBRARY_PATH, str(library_root))
main_window.Show()
report = [f"display: {wx.GetDisplaySize().width}x{wx.GetDisplaySize().height}, platform: {wx.PlatformInfo[1]}"]


def settle(seconds=0.6):
    """Let the window draw and lay itself out."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        wx.SafeYield()
        time.sleep(0.03)


def grab(window, path):
    """Save a picture of ``window``, its frame included where the platform draws one."""
    if wx.Platform == "__WXMSW__":
        # PrintWindow draws the window into a bitmap even where it runs past the edge of the screen.
        import ctypes

        size = window.GetSize()
        bitmap = wx.Bitmap(size.width, size.height)
        memory = wx.MemoryDC(bitmap)
        render_full_content = 2
        ctypes.windll.user32.PrintWindow(window.GetHandle(), memory.GetHDC(), render_full_content)
        memory.SelectObject(wx.NullBitmap)
        bitmap.SaveFile(str(path), wx.BITMAP_TYPE_PNG)
        return
    # Elsewhere ImageMagick takes it from the X display.
    subprocess.run(["import", "-window", str(window.GetHandle()), str(path)], check=False)


def describe(control):
    """One line for a control: its kind, its label and its name, as a screen reader is given them."""
    kind = type(control).__name__
    label = " ".join(control.GetLabel().split()) if hasattr(control, "GetLabel") else ""
    name = control.GetName()
    shown = "" if control.IsShown() else " (hidden)"
    enabled = "" if control.IsEnabled() else " (disabled)"
    return f"{kind}: label={label!r} name={name!r}{shown}{enabled}"


def tab_order(window, depth=0):
    """Every control under ``window``, in the order Tab reaches them: the order they were made in."""
    lines = []
    for child in window.GetChildren():
        if isinstance(child, wx.TopLevelWindow):
            continue
        lines.append("  " * depth + describe(child))
        lines.extend(tab_order(child, depth + 1))
    return lines


def snap(name, window):
    """Screenshot ``window`` and note its size."""
    window.Raise()
    settle()
    grab(window, shots / f"{name}.png")
    size = window.GetSize()
    report.append(f"{name}: {size.width}x{size.height}")


def find_notebook(window):
    """The first notebook inside ``window``, or None."""
    for child in window.GetChildren():
        if isinstance(child, wx.Notebook):
            return child
        found = find_notebook(child)
        if found is not None:
            return found
    return None


def walk(name, make, *, pages=False, keep=False, then=None):
    """Open a window, screenshot it (each page of its notebook with ``pages``), list its controls, and close it unless ``keep``. ``then`` is called with the window for any extra states to capture."""
    try:
        window = make()
        if not window.IsShown():
            window.Show()
        settle()
        (controls / f"{name}.txt").write_text("\n".join(tab_order(window)) + "\n", encoding="utf-8")
        notebook = find_notebook(window) if pages else None
        if notebook is not None:
            for page in range(notebook.GetPageCount()):
                notebook.SetSelection(page)
                settle(0.3)
                page_name = notebook.GetPageText(page).replace("&", "").replace(" ", "_")
                snap(f"{name}-{page + 1}-{page_name}", window)
        else:
            snap(name, window)
        if then is not None:
            then(window)
        if not keep:
            window.Destroy()
            settle(0.2)
        return window
    except Exception:
        report.append(f"{name}: FAILED\n" + traceback.format_exc(limit=4))
        return None


def audiobook_format(window):
    """The Add Story window with Format set to audio, which shows the voice settings."""
    choice = main_window.format_ctrl
    choice.SetSelection(choice.FindString("audio"))
    main_window._update_audio_panel_visibility()
    settle()
    snap("add-story-window-audio", window)
    choice.SetSelection(choice.FindString("epub"))
    main_window._update_audio_panel_visibility()


def scrolled_areas(window):
    """Every scrolled area inside ``window``."""
    found = []
    for child in window.GetChildren():
        if isinstance(child, wx.ScrolledWindow):
            found.append(child)
        found.extend(scrolled_areas(child))
    return found


def scrolled_to_the_end(window):
    """The window with every scrolled area in it scrolled to the bottom."""
    for area in scrolled_areas(window):
        area.Scroll(0, area.GetVirtualSize().height)
    settle()
    snap(f"{window.GetTitle().lower().replace(' ', '-')}-scrolled-to-the-end", window)


def last_button_focused(window):
    """Optional Features scrolled back to the top, then the last Install button given the focus, as Tab gives it: it should come into view."""
    for area in scrolled_areas(window):
        area.Scroll(0, 0)
    settle()
    last = list(window._buttons.values())[-1]
    last.SetFocus()
    settle()
    snap("optional-features-last-button-focused", window)


def optional_features_states(window):
    scrolled_to_the_end(window)
    last_button_focused(window)


# The main window, the Add Story window and the Library window.
walk("main-window", lambda: main_window, keep=True)
walk("add-story-window", lambda: main_window.add_story_frame, keep=True, then=audiobook_format)
main_window.add_story_frame.Hide()
main_window._on_library_menu(None)
settle()
walk("library-window", lambda: main_window._library_frame, keep=True)

# The library's own windows and dialogs.
from ficary.library.browser import LibraryBrowserFrame  # noqa: E402
from ficary.library.gui import AbandonedStoriesDialog, ReorganizePreviewDialog, ReviewDialog  # noqa: E402
from ficary.library.index import LibraryIndex  # noqa: E402
from ficary.library.reorganizer import MoveOp  # noqa: E402

library_window = main_window._library_frame
walk("library-browser", lambda: LibraryBrowserFrame(main_window, main_window.prefs))
walk("reorganize-preview", lambda: ReorganizePreviewDialog(library_window, root=library_root, moves=[
    MoveOp(source=books[0], target=library_root / "Harry Potter" / books[0].name, source_url="https://www.fanfiction.net/s/1001/1/"),
    MoveOp(source=books[1], target=library_root / "Original" / books[1].name, source_url="https://www.fanfiction.net/s/1002/1/"),
]))
walk("review-ambiguous", lambda: ReviewDialog(library_window, idx=LibraryIndex.load(), root=library_root, untrackable=[
    {"relpath": "Misc/Some Old Download.epub", "title": "Some Old Download", "author": "Unknown"},
    {"relpath": "Misc/Another One.html", "title": "Another One", "author": "Someone"},
]))
walk("abandoned-stories", lambda: AbandonedStoriesDialog(library_window, library_root))

# Preferences, one screenshot per page.
from ficary.preferences import PreferencesDialog  # noqa: E402

walk("preferences", lambda: PreferencesDialog(main_window, main_window.prefs, main_frame=main_window), pages=True)

# The download and audiobook dialogs.
from ficary import gui_dialogs  # noqa: E402

works = [
    {"title": "The Long Road Home", "author": "Someone Writes", "words": "36,000", "chapters": "12", "rating": "T", "status": "In-Progress", "updated": "2026-11-20", "url": "https://www.fanfiction.net/s/1001/1/", "section": "own"},
    {"title": "A Favourite of Theirs", "author": "Other Person", "words": "120,000", "chapters": "40", "rating": "M", "status": "Complete", "updated": "2025-03-02", "url": "https://www.fanfiction.net/s/1009/1/", "section": "favorites"},
]
walk("story-picker", lambda: gui_dialogs.StoryPickerDialog(main_window, "Stories by Someone Writes", works, prefs=main_window.prefs))
own_works = [dict(work, section="own") for work in works]
walk("story-picker-own-stories-only", lambda: gui_dialogs.StoryPickerDialog(main_window, "Stories by Someone Writes", own_works, prefs=main_window.prefs))
walk("multi-picker", lambda: gui_dialogs.MultiPickerDialog(main_window, "Genres", ["Adventure", "Drama", "Fantasy", "LitRPG", "Romance", "Sci-fi"], initial=["Drama"]))
walk("series-parts", lambda: gui_dialogs.SeriesPartsDialog(main_window, "A Made-up Series", [
    {"title": "Part One", "author": "Someone Writes", "words": "12,000", "rating": "T", "url": "https://example.org/1"},
    {"title": "Part Two", "author": "Someone Writes", "words": "15,500", "rating": "T", "url": "https://example.org/2"},
]))
walk("optional-features", lambda: gui_dialogs.OptionalFeaturesDialog(main_window), then=optional_features_states)
walk("tts-providers", lambda: gui_dialogs.TtsProvidersDialog(main_window, main_window.prefs))
walk("ai-settings", lambda: gui_dialogs.LlmSettingsDialog(main_window, main_window.prefs))
walk("add-from-url-list", lambda: gui_dialogs.AddFromUrlListDialog(main_window))
voice_map = SimpleNamespace(mapping={"Harry": "en-GB-RyanNeural", "Hermione": "en-GB-SoniaNeural"}, save=lambda: None)
walk("voice-preview", lambda: gui_dialogs.VoicePreviewDialog(main_window, [
    {"name": "Harry", "gender": "male", "voice": "en-GB-RyanNeural", "count": 42},
    {"name": "Hermione", "gender": "female", "voice": "en-GB-SoniaNeural", "count": 37},
], voice_map, "en-US-AriaNeural"))
walk("update-progress", lambda: gui._UpdateProgressDialog(main_window, "v9.9.9", threading.Event()))

# Each site's search window.
from ficary.gui_search import SearchFrame  # noqa: E402

for _accelerator, site_key, spec_function, _label in main_window._SEARCH_MENU_ITEMS:
    walk(f"search-{site_key}", lambda site_key=site_key, spec_function=spec_function: SearchFrame(main_window, site_key, spec_function()))

# The watchlist and its two Add dialogs.
from ficary import gui_watchlist  # noqa: E402

walk("watchlist", lambda: gui_watchlist.WatchlistFrame(main_window))
walk("add-url-watch", lambda: gui_watchlist.AddURLWatchDialog(main_window, gui_watchlist.WATCH_TYPE_STORY, channels=["Status pane", "Email"]))
walk("add-search-watch", lambda: gui_watchlist.AddSearchWatchDialog(main_window, channels=["Status pane", "Email"]))

# The reader and its bookmarks.
from ficary.reader.gui import ReaderFrame, _BookmarksDialog  # noqa: E402
from ficary.reader.source import StorySource  # noqa: E402

reader_source = StorySource.from_file(books[0], url="https://www.fanfiction.net/s/1001/1/", title="The Long Road Home", author="Someone Writes")
reader = walk("reader", lambda: ReaderFrame(main_window, main_window.prefs, reader_source), keep=True)
bookmarks = [
    SimpleNamespace(name="Where I stopped", chapter_number=3, excerpt="The wind came off the ridge"),
    SimpleNamespace(name="Good bit", chapter_number=7, excerpt=""),
]
walk("reader-bookmarks", lambda: _BookmarksDialog(reader or main_window, bookmarks))
if reader is not None:
    reader.Destroy()

# The soundscape editor.
from ficary.soundscape.editor import SoundscapeEditorDialog  # noqa: E402

walk("soundscape-editor", lambda: SoundscapeEditorDialog(main_window))

(output / "sizes.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
print("\n".join(report))
library_window.Destroy()
main_window._clip_timer.Stop()
main_window.Destroy()
settle(0.3)
