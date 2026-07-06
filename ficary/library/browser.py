"""Library browser — a screen-reader-navigable list of every story in the
indexed library, with actions to read, update, re-export, remove, or locate
one.

Reads the JSON :class:`~ficary.library.index.LibraryIndex` (populated by Scan
Library), so the list reflects the last scan; a Rescan button refreshes it.
Adult rows — a story in the separate adult-library root, or from an adult-site
adapter — are hidden until the user ticks "Show adult", keeping that content
off-screen by default.

Accessibility: the story list is a ``wx.ListCtrl`` in report mode with column
headers, and a read-only Summary pane below it updates on every arrow-key
focus change so a keyboard-only user hears the full details (path, URL,
library) without leaving the list. Enter (or double-click) opens the selected
story in the reader.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import wx

from .. import prefs as _prefs

logger = logging.getLogger(__name__)

# (heading, initial width). Title gets the lion's share; Library is the
# short root label / "Adult" bucket marker.
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Title", 320),
    ("Author", 160),
    ("Fandom", 150),
    ("Format", 70),
    ("Library", 100),
)

_REEXPORT_FORMATS = ("epub", "html", "txt")


@dataclass
class _Row:
    """One story as shown in the browser. ``abs_path`` is the file on disk;
    ``is_adult`` drives the hidden-by-default filter; ``library_label`` is the
    short per-root column value ("Adult" for the separate adult root)."""

    url: str
    title: str
    author: str
    fandom: str
    fmt: str
    root: Path
    abs_path: str
    is_adult: bool
    library_label: str


class LibraryBrowserFrame(wx.Frame):
    """Browse the indexed library and act on individual stories."""

    def __init__(self, main_frame: wx.Window, prefs):
        super().__init__(
            main_frame,
            title="Library Browser",
            size=(900, 620),
        )
        self._main = main_frame
        self._prefs = prefs
        self._rows: list[_Row] = []       # everything loaded from the index
        self._visible: list[_Row] = []    # current filtered view (parallel to list rows)
        self._adult_hidden = 0            # count hidden by the adult filter, for the status line
        self._build_ui()
        self._install_escape_accel()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._reload()

    # ── UI ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Search + adult toggle row.
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(
            wx.StaticText(panel, label="&Search:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.search_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetName("Search stories")
        self.search_ctrl.SetHint("title, author, or fandom")
        self.search_ctrl.Bind(wx.EVT_TEXT, self._on_search)
        top.Add(self.search_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.adult_chk = wx.CheckBox(panel, label="Show &adult")
        self.adult_chk.SetName("Show adult stories")
        self.adult_chk.SetValue(False)
        self.adult_chk.Bind(wx.EVT_CHECKBOX, self._on_toggle_adult)
        top.Add(self.adult_chk, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        # Story list.
        self.list_ctrl = wx.ListCtrl(
            panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
        )
        self.list_ctrl.SetName("Library stories")
        for col, (heading, width) in enumerate(_COLUMNS):
            self.list_ctrl.InsertColumn(col, heading, width=width)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        # FOCUSED fires on arrow-key movement even when selection state
        # doesn't visibly change, so the summary tracks the NVDA cursor.
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_select)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_deselect)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        sizer.Add(self.list_ctrl, 3, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.count_ctrl = wx.StaticText(panel, label="")
        sizer.Add(self.count_ctrl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Read-only detail pane — mirrors the picker-dialog convention so
        # keyboard users get the full record without leaving the list.
        sizer.Add(
            wx.StaticText(panel, label="&Details:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 8,
        )
        self.summary_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130),
        )
        self.summary_ctrl.SetName("Story details")
        sizer.Add(self.summary_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Action buttons.
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = wx.Button(panel, label="&Open in Reader")
        self.open_btn.Bind(wx.EVT_BUTTON, self._on_open)
        btns.Add(self.open_btn, 0, wx.RIGHT, 6)

        self.update_btn = wx.Button(panel, label="Check for &Updates")
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_update)
        btns.Add(self.update_btn, 0, wx.RIGHT, 6)

        self.reexport_btn = wx.Button(panel, label="Re-&export...")
        self.reexport_btn.Bind(wx.EVT_BUTTON, self._on_reexport)
        btns.Add(self.reexport_btn, 0, wx.RIGHT, 6)

        self.path_btn = wx.Button(panel, label="Copy &Path")
        self.path_btn.Bind(wx.EVT_BUTTON, self._on_copy_path)
        btns.Add(self.path_btn, 0, wx.RIGHT, 6)

        self.delete_btn = wx.Button(panel, label="&Delete...")
        self.delete_btn.Bind(wx.EVT_BUTTON, self._on_delete)
        btns.Add(self.delete_btn, 0, wx.RIGHT, 6)

        self.rescan_btn = wx.Button(panel, label="Re&scan")
        self.rescan_btn.Bind(wx.EVT_BUTTON, self._on_rescan)
        btns.Add(self.rescan_btn, 0, wx.RIGHT, 6)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btns.Add(close_btn, 0)
        sizer.Add(btns, 0, wx.ALL, 8)

        panel.SetSizer(sizer)
        self._enable_actions(False)

    def _install_escape_accel(self) -> None:
        close_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=int(close_id))
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, int(close_id)),
        ]))

    # ── Data loading / filtering ───────────────────────────────

    def _reload(self) -> None:
        """(Re)load rows from the on-disk index and refresh the view."""
        from .index import LibraryIndex
        from .template import ADULT_FICTION_ADAPTERS

        self._rows = []
        try:
            idx = LibraryIndex.load()
        except Exception as exc:  # index missing/corrupt — surface, don't crash
            logger.debug("library index load failed", exc_info=True)
            self._set_summary(f"Could not load the library index: {exc}")
            self._apply_filter()
            return

        adult_root = self._configured_adult_root()
        for root_str in idx.library_roots():
            root = Path(root_str)
            try:
                root_resolved = root.expanduser().resolve()
            except OSError:
                root_resolved = root
            root_is_adult = (
                adult_root is not None and root_resolved == adult_root
            )
            for url, entry in idx.stories_in(root):
                adapter = entry.get("adapter") or ""
                fandoms = entry.get("fandoms") or []
                rel = entry.get("relpath") or ""
                self._rows.append(_Row(
                    url=url,
                    title=entry.get("title") or "(untitled)",
                    author=entry.get("author") or "",
                    fandom=", ".join(fandoms),
                    fmt=entry.get("format") or "",
                    root=root,
                    abs_path=str(root / rel) if rel else "",
                    is_adult=root_is_adult or adapter in ADULT_FICTION_ADAPTERS,
                    library_label="Adult" if root_is_adult else (root.name or root_str),
                ))
        self._rows.sort(key=lambda r: (r.title.lower(), r.author.lower()))
        self._apply_filter()

    def _configured_adult_root(self) -> Optional[Path]:
        raw = (self._prefs.get(_prefs.KEY_LIBRARY_ADULT_PATH, "") or "").strip()
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve()
        except OSError:
            return None

    def _apply_filter(self) -> None:
        query = (self.search_ctrl.GetValue() or "").strip().lower()
        show_adult = self.adult_chk.GetValue()

        def matches(row: _Row) -> bool:
            if row.is_adult and not show_adult:
                return False
            if not query:
                return True
            return (
                query in row.title.lower()
                or query in row.author.lower()
                or query in row.fandom.lower()
            )

        self._adult_hidden = sum(
            1 for r in self._rows if r.is_adult and not show_adult
        )
        self._visible = [r for r in self._rows if matches(r)]

        self.list_ctrl.DeleteAllItems()
        for i, row in enumerate(self._visible):
            self.list_ctrl.InsertItem(i, row.title)
            self.list_ctrl.SetItem(i, 1, row.author)
            self.list_ctrl.SetItem(i, 2, row.fandom)
            self.list_ctrl.SetItem(i, 3, row.fmt)
            self.list_ctrl.SetItem(i, 4, row.library_label)

        self._update_count()
        if self._visible:
            self.list_ctrl.Select(0)
            self.list_ctrl.Focus(0)
            self._update_summary(self._visible[0])
            self._enable_actions(True)
        else:
            self._enable_actions(False)
            if not self._rows:
                self._set_summary(
                    "The library index is empty. Set a library folder in the "
                    "Library window and run Scan Library, then Rescan here."
                )
            else:
                self._set_summary("No stories match the current filter.")

    def _update_count(self) -> None:
        total = len(self._rows)
        shown = len(self._visible)
        msg = f"Showing {shown} of {total} stor{'y' if total == 1 else 'ies'}"
        if self._adult_hidden:
            msg += f" ({self._adult_hidden} adult hidden — tick Show adult to include)"
        self.count_ctrl.SetLabel(msg)

    # ── Selection / summary ────────────────────────────────────

    def _selected_row(self) -> Optional[_Row]:
        i = self.list_ctrl.GetFirstSelected()
        if 0 <= i < len(self._visible):
            return self._visible[i]
        return None

    def _on_select(self, event: wx.Event) -> None:
        i = event.GetIndex()
        if 0 <= i < len(self._visible):
            self._update_summary(self._visible[i])
            self._enable_actions(True)
        event.Skip()

    def _on_deselect(self, event: wx.Event) -> None:
        if self.list_ctrl.GetSelectedItemCount() == 0:
            self._enable_actions(False)
        event.Skip()

    def _on_activate(self, event: wx.Event) -> None:
        # Enter / double-click on a row reads it — the natural default.
        self._on_open(event)

    def _update_summary(self, row: _Row) -> None:
        self._set_summary("\n".join([
            f"Title: {row.title}",
            f"Author: {row.author or '(unknown)'}",
            f"Fandom: {row.fandom or '(none)'}",
            f"Format: {row.fmt or '(unknown)'}",
            f"Library: {row.library_label}"
            + ("  [adult]" if row.is_adult else ""),
            f"File: {row.abs_path or '(missing path)'}",
            f"Source: {row.url}",
        ]))

    def _set_summary(self, text: str) -> None:
        self.summary_ctrl.SetValue(text)

    def _enable_actions(self, enabled: bool) -> None:
        for btn in (
            self.open_btn, self.update_btn, self.reexport_btn,
            self.path_btn, self.delete_btn,
        ):
            btn.Enable(enabled)

    # ── Actions ────────────────────────────────────────────────

    def _require_file(self, row: _Row) -> Optional[Path]:
        """Return the story's existing Path, or warn and return None."""
        if not row.abs_path:
            wx.MessageBox(
                "This entry has no file path on record (index-only).",
                "Library Browser", wx.OK | wx.ICON_WARNING, self,
            )
            return None
        path = Path(row.abs_path)
        if not path.exists():
            wx.MessageBox(
                f"The file is no longer on disk:\n\n{path}\n\n"
                "Rescan to refresh the list.",
                "Library Browser", wx.OK | wx.ICON_WARNING, self,
            )
            return None
        return path

    def _on_open(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        path = self._require_file(row)
        if path is None:
            return
        # The reader understands EPUB/HTML; TXT has no chapter structure
        # to navigate, so it isn't a reader source.
        if path.suffix.lower() not in (".epub", ".html", ".htm"):
            wx.MessageBox(
                f"The reader opens EPUB and HTML stories; this is a "
                f"{path.suffix or 'file'}.",
                "Library Browser", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        self._main._open_reader_for_file(
            str(path), url=row.url, title=row.title, author=row.author,
        )

    def _on_update(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        path = self._require_file(row)
        if path is None:
            return
        if getattr(self._main, "_global_busy", False):
            wx.MessageBox(
                "The main window is busy with another job. Wait for it to "
                "finish, then try again.",
                "Library Browser", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        # Reuse the main window's single-file update pipeline so progress,
        # merge-in-place, and per-site queueing all behave identically to
        # the File → Update flow.
        try:
            self._main._begin_update_for_path(str(path))
        except Exception as exc:
            wx.MessageBox(
                f"Could not start the update:\n\n{exc}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            return
        wx.MessageBox(
            "Update started — watch the main window's status pane for "
            "progress.",
            "Library Browser", wx.OK | wx.ICON_INFORMATION, self,
        )

    def _on_copy_path(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None or not row.abs_path:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(row.abs_path))
            finally:
                wx.TheClipboard.Close()
            self.count_ctrl.SetLabel(f"Copied path: {row.abs_path}")

    def _on_delete(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        path = self._require_file(row)
        if path is None:
            return
        if wx.MessageBox(
            f"Delete this story file from disk?\n\n{path}\n\n"
            "This cannot be undone.",
            "Delete story",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self,
        ) != wx.YES:
            return
        try:
            path.unlink()
        except OSError as exc:
            wx.MessageBox(
                f"Could not delete the file:\n\n{exc}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            return
        # Drop it from the index too, so it doesn't reappear until a
        # rescan and so the count stays honest.
        try:
            from .index import LibraryIndex
            idx = LibraryIndex.load()
            if idx.remove(row.root, row.url):
                idx.save()
        except Exception:
            logger.debug("index remove after delete failed", exc_info=True)
        self.count_ctrl.SetLabel(f"Deleted: {path.name}")
        self._reload()

    def _on_reexport(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        path = self._require_file(row)
        if path is None:
            return
        with wx.SingleChoiceDialog(
            self, "Re-export this story to which format?", "Re-export",
            [f.upper() for f in _REEXPORT_FORMATS],
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            fmt = _REEXPORT_FORMATS[dlg.GetSelection()]
        self._run_reexport(row, path, fmt)

    def _run_reexport(self, row: _Row, path: Path, fmt: str) -> None:
        self.count_ctrl.SetLabel(f"Re-exporting {row.title} to {fmt.upper()}...")
        self._enable_actions(False)
        out_dir = str(path.parent)

        def worker():
            try:
                result = _reexport_file(path, fmt, out_dir)
            except Exception as exc:  # surface any read/export failure
                wx.CallAfter(self._reexport_done, None, str(exc))
                return
            wx.CallAfter(self._reexport_done, result, None)

        threading.Thread(target=worker, daemon=True).start()

    def _reexport_done(self, result: Optional[str], error: Optional[str]) -> None:
        self._enable_actions(True)
        if error:
            wx.MessageBox(
                f"Re-export failed:\n\n{error}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            self.count_ctrl.SetLabel("Re-export failed.")
            return
        self.count_ctrl.SetLabel(f"Re-exported to: {result}")

    def _on_rescan(self, event: wx.Event) -> None:
        # Rescan every configured root (main + separate adult), then reload.
        from .scanner import scan

        roots: list[Path] = []
        for key in (_prefs.KEY_LIBRARY_PATH, _prefs.KEY_LIBRARY_ADULT_PATH):
            raw = (self._prefs.get(key, "") or "").strip()
            if not raw:
                continue
            root = Path(raw).expanduser()
            if root.is_dir() and root not in roots:
                roots.append(root)
        if not roots:
            wx.MessageBox(
                "No library folder is configured. Set one in the Library "
                "window first.",
                "Library Browser", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        self.count_ctrl.SetLabel("Rescanning...")
        self.rescan_btn.Enable(False)

        def worker():
            try:
                for root in roots:
                    scan(root, recursive=True)
            except Exception as exc:
                wx.CallAfter(self._rescan_done, str(exc))
                return
            wx.CallAfter(self._rescan_done, None)

        threading.Thread(target=worker, daemon=True).start()

    def _rescan_done(self, error: Optional[str]) -> None:
        self.rescan_btn.Enable(True)
        if error:
            wx.MessageBox(
                f"Rescan failed:\n\n{error}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            return
        self._reload()

    def _on_search(self, event: wx.Event) -> None:
        self._apply_filter()

    def _on_toggle_adult(self, event: wx.Event) -> None:
        self._apply_filter()

    def _on_close(self, event: wx.Event) -> None:
        # Let the main window drop its reference if it tracks one.
        notify = getattr(self._main, "_notify_browser_frame_closed", None)
        if callable(notify):
            try:
                notify()
            except Exception:
                pass
        event.Skip()


def _reexport_file(path: Path, fmt: str, out_dir: str) -> str:
    """Reconstruct a Story from a downloaded file and re-export it as
    ``fmt``. Returns the written path. Runs off the UI thread."""
    from ..exporters import EXPORTERS
    from ..models import Story
    from ..updater import extract_metadata, read_chapters

    chapters = read_chapters(path)  # raises for TXT / unsupported
    md = extract_metadata(path)
    story = Story(
        id=0,
        title=md.title or path.stem,
        author=md.author or "Unknown Author",
        summary="",
        url=md.source_url or "",
        chapters=chapters,
        metadata={
            "rating": md.rating,
            "status": md.status,
            "fandoms": list(md.fandoms),
        },
    )
    exporter = EXPORTERS[fmt]
    return str(exporter(story, out_dir))
