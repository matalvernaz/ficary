"""Two pieces of the library-first main window: "Import Existing Folder", and asking for a library folder the first time Ficary opens.

The maintainer chose both. Scan Library becomes Import Existing Folder, in the Library window and on the Library menu, and every message that told people to run Scan Library names it instead. The first time Ficary opens with no library folder it asks where the library should be, once; Cancel leaves the question to the first download, as before.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

wx = pytest.importorskip("wx")

from ficary import prefs as prefs_module  # noqa: E402
from ficary.library import scanner  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "ficary"


class TestTheName:
    def test_no_visible_text_still_says_scan_library(self):
        """Docstrings and comments may; labels and messages may not."""
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "Scan Library" in node.value:
                    parent = parents.get(node)
                    if isinstance(parent, ast.Expr):
                        continue
                    found.append(f"{path.name}:{node.lineno}")
        assert found == []

    def test_a_book_saved_outside_the_library_is_told_where_to_go(self, tmp_path):
        library = tmp_path / "library"
        library.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        book = elsewhere / "story.epub"
        book.write_bytes(b"zip")
        reason = scanner.library_skip_reason(book, library)
        assert "use Library > Import Existing Folder after moving the file" in reason

    def test_the_name_is_the_one_the_button_and_menu_use(self):
        assert scanner.IMPORT_EXISTING_FOLDER == "Import Existing Folder"

    def test_the_command_line_option_keeps_its_name(self):
        from ficary.cli import _build_parser

        args = _build_parser().parse_args(["--scan-library", "/tmp/library"])
        assert args.scan_library == "/tmp/library"


needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


def window_with_settings(monkeypatch, values: dict):
    """A real main window whose settings are a plain dict the test controls."""
    from ficary.gui import MainFrame

    frame = MainFrame()

    def get(key, default=None):
        return values.get(key, default)

    def set_value(key, value):
        values[key] = value

    def get_bool(key, default=False):
        value = values.get(key, default)
        return value in (True, "1", 1, "true", "True")

    def set_bool(key, value):
        values[key] = bool(value)

    monkeypatch.setattr(frame.prefs, "get", get)
    monkeypatch.setattr(frame.prefs, "set", set_value)
    monkeypatch.setattr(frame.prefs, "get_bool", get_bool)
    monkeypatch.setattr(frame.prefs, "set_bool", set_bool)
    frame.output_ctrl.SetValue("")
    return frame


class FolderPicker:
    """Stands in for the folder picker. ``answer`` is the folder chosen, or None for Cancel."""

    answer = None
    shown = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ShowModal(self):
        type(self).shown += 1
        return wx.ID_OK if type(self).answer else wx.ID_CANCEL

    def GetPath(self):
        return type(self).answer


@pytest.fixture
def picker(monkeypatch):
    FolderPicker.answer = None
    FolderPicker.shown = 0
    monkeypatch.setattr(wx, "DirDialog", FolderPicker)
    return FolderPicker


@needs_display
class TestTheMenuAndTheButton:
    def test_the_library_menu_has_it(self, wx_app, monkeypatch):
        frame = window_with_settings(monkeypatch, {})
        try:
            bar = frame.GetMenuBar()
            menu = bar.GetMenu(bar.FindMenu("Library"))
            labels = [item.GetItemLabel() for item in menu.GetMenuItems()]
            assert "&Import Existing Folder..." in labels
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_the_menu_item_opens_the_library_window_and_starts_the_import(self, wx_app, monkeypatch):
        frame = window_with_settings(monkeypatch, {})
        started = []
        try:
            from ficary.library.gui import LibraryFrame

            monkeypatch.setattr(LibraryFrame, "_on_scan", lambda self, event: started.append(True))
            item_id = frame.GetMenuBar().FindMenuItem("Library", "Import Existing Folder...")
            assert item_id != wx.NOT_FOUND
            frame.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item_id))
            assert frame._library_frame is not None
            assert started == [True]
        finally:
            if frame._library_frame is not None:
                frame._library_frame.Destroy()
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_the_library_windows_button_is_renamed_with_a_free_shortcut(self, wx_app, monkeypatch):
        frame = window_with_settings(monkeypatch, {})
        try:
            from ficary.library.gui import LibraryFrame

            library = LibraryFrame(frame, frame.prefs)
            try:
                assert library.scan_btn.GetLabel() == "&Import Existing Folder"
                letters = []
                for child in library.GetChildren()[0].GetChildren():
                    label = child.GetLabel() if hasattr(child, "GetLabel") else ""
                    if "&" in label:
                        letters.append(label[label.index("&") + 1].lower())
                assert letters.count("i") == 1
            finally:
                library.Destroy()
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_an_import_already_running_is_not_started_twice(self, wx_app, monkeypatch):
        frame = window_with_settings(monkeypatch, {})
        try:
            from ficary.library.gui import LibraryFrame

            library = LibraryFrame(frame, frame.prefs)
            started = []
            monkeypatch.setattr(library, "_on_scan", lambda event: started.append(True))
            try:
                library.scan_btn.Disable()
                library.trigger_import()
                assert started == []
                library.scan_btn.Enable()
                library.trigger_import()
                assert started == [True]
            finally:
                library.Destroy()
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()


@needs_display
class TestTheFirstStartQuestion:
    def test_it_asks_when_there_is_no_library_and_sets_the_answer(self, wx_app, monkeypatch, picker):
        values = {}
        frame = window_with_settings(monkeypatch, values)
        try:
            picker.answer = "/home/someone/Stories"
            frame.original_library_questions()
            assert picker.shown == 1
            assert values[prefs_module.KEY_LIBRARY_PATH] == "/home/someone/Stories"
            assert frame.output_ctrl.GetValue() == "/home/someone/Stories"
            assert values[prefs_module.KEY_LIBRARY_ASKED_AT_START] is True
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_it_is_asked_once_whatever_the_answer(self, wx_app, monkeypatch, picker):
        values = {}
        frame = window_with_settings(monkeypatch, values)
        try:
            frame.original_library_questions()
            frame.original_library_questions()
            assert picker.shown == 1
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_cancel_leaves_it_to_the_first_download(self, wx_app, monkeypatch, picker):
        values = {}
        frame = window_with_settings(monkeypatch, values)
        try:
            frame.original_library_questions()
            assert prefs_module.KEY_LIBRARY_PATH not in values
            with frame._log_lock:
                text = "".join(frame._log_queue)
            assert "Your first download will ask again." in text
            # And the first download does ask.
            picker.answer = "/home/someone/Stories"
            assert frame._require_save_target() is True
            assert picker.shown == 2
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_it_does_not_ask_when_a_library_is_set(self, wx_app, monkeypatch, picker):
        frame = window_with_settings(monkeypatch, {prefs_module.KEY_LIBRARY_PATH: "/home/someone/Stories"})
        try:
            frame.original_library_questions()
            assert picker.shown == 0
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_only_one_question_per_launch(self, wx_app, monkeypatch, picker):
        """When the offer to use an already scanned library comes up, the first-start question waits for another day."""
        values = {}
        frame = window_with_settings(monkeypatch, values)
        try:
            monkeypatch.setattr(frame, "_maybe_offer_library_as_default", lambda: True)
            frame.original_library_questions()
            assert picker.shown == 0
            assert prefs_module.KEY_LIBRARY_ASKED_AT_START not in values
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_the_offer_says_when_it_did_not_ask(self, wx_app, monkeypatch, picker):
        frame = window_with_settings(monkeypatch, {prefs_module.KEY_LIBRARY_DEFAULT_PROMPTED: True})
        try:
            assert frame._maybe_offer_library_as_default() is False
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()

    def test_the_window_asks_at_launch(self, wx_app, monkeypatch):
        """The launch schedules the questions; the test suite holds them back, so check the scheduling."""
        from ficary import gui

        scheduled = []
        real_call_after = gui.wx.CallAfter

        def call_after(function, *args, **kwargs):
            # The suite swaps the method for a stand-in, so compare with whatever the class holds now.
            if getattr(function, "__func__", None) is gui.MainFrame._library_questions_at_start:
                scheduled.append(True)
                return None
            return real_call_after(function, *args, **kwargs)

        monkeypatch.setattr(gui.wx, "CallAfter", call_after)
        frame = window_with_settings(monkeypatch, {})
        try:
            assert scheduled == [True]
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
