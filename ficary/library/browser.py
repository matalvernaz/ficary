"""Library browser — a screen-reader-navigable list of every story in the
indexed library, with actions to read, update, re-export, remove, or locate
one.

Reads the JSON :class:`~ficary.library.index.LibraryIndex` (populated by Scan
Library), so the list reflects the last scan; a Rescan button refreshes it.
Adult rows — a story in the separate adult-library root, or from an adult-site
adapter — are hidden until the user ticks "Show adult", keeping that content
off-screen by default. A per-story Mark Adult / Mark Not Adult button writes an
explicit ``adult`` override onto the index entry that wins over the derived
guess, so a mis-classified story (a false positive, or an explicit work the
site-based rule missed) can be corrected. A Mark Abandoned / Revive button
does the same for the abandoned-WIP flag, so a dead work-in-progress can be
retired from update checks right from the browser.

Accessibility: the story list is a ``wx.ListCtrl`` in report mode with column
headers, and a read-only Summary pane below it updates on every arrow-key
focus change so a keyboard-only user hears the full details (path, URL,
library) without leaving the list. Enter (or double-click) opens the selected
story in the reader.

Structure: all of the above lives in :class:`LibraryPanel`, a plain
``wx.Panel`` so it can be hosted standalone (:class:`LibraryBrowserFrame`,
the Library window) *or* embedded directly in the main window as the
library-first central view. Cross-window actions (open in the reader, start
an update) are delegated to a ``main_frame`` handle rather than done here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import wx

from .. import prefs as _prefs
from ..gui_help import set_help

logger = logging.getLogger(__name__)

# (heading, initial width). Title gets the lion's share; Library is the
# short root label / "Adult" bucket marker.
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Title", 300),
    ("Author", 150),
    ("Fandom", 140),
    ("Format", 70),
    ("Library", 90),
    ("Added", 100),
)

# Sort-by dropdown options: label → (row attribute, descending). Kept as
# an explicit control rather than column-header clicks alone because
# header clicks are mouse-only — invisible to a keyboard/screen-reader
# user. Header clicks still work and drive the same state.
_SORT_CHOICES: tuple[tuple[str, tuple[str, bool]], ...] = (
    ("Title", ("title", False)),
    ("Author", ("author", False)),
    ("Fandom", ("fandom", False)),
    ("Format", ("fmt", False)),
    ("Library", ("library_label", False)),
    ("Date added (newest first)", ("added_at", True)),
    ("Date added (oldest first)", ("added_at", False)),
)

# Column index → row attribute, for header-click sorting.
_COLUMN_SORT_ATTRS = ("title", "author", "fandom", "fmt", "library_label",
                      "added_at")

_REEXPORT_FORMATS = ("epub", "html", "txt")


def _added_display(added_at: str) -> str:
    """Human column value for an ISO ``added_at`` stamp: just the date
    part; empty for entries indexed before the field existed (their
    true add date is unknown — showing scan time would be a lie)."""
    return added_at[:10] if added_at else ""


@dataclass
class _Row:
    """One story as shown in the browser. ``abs_path`` is the file on disk;
    ``is_adult`` drives the hidden-by-default filter; ``library_label`` is the
    short per-root column value ("Adult" for the separate adult root).

    ``adult_overridden`` records whether ``is_adult`` came from an explicit
    per-story override rather than the site/folder-derived guess, so the
    details pane can say so. ``is_abandoned`` reflects the ``abandoned_at``
    index flag that drops a WIP from update probes."""

    url: str
    title: str
    author: str
    fandom: str
    fmt: str
    root: Path
    abs_path: str
    is_adult: bool
    library_label: str
    adult_overridden: bool = False
    is_abandoned: bool = False
    added_at: str = ""
    """ISO first-seen stamp from the index; empty on entries indexed
    before the field existed. Empty values sort to the end under
    "newest first" (and the start under oldest-first)."""


class LibraryPanel(wx.Panel):
    """Browse the indexed library and act on individual stories.

    Hosted standalone by :class:`LibraryBrowserFrame` and mountable
    directly in the main window. Cross-window actions go through
    ``main_frame`` (``_open_reader_for_file``, ``_begin_update_for_path``,
    ``_global_busy``). ``on_close``, when given, adds a Close button wired
    to it — the standalone frame passes its own ``Close``; the embedded
    main-window view passes nothing (there's nothing to close)."""

    def __init__(self, parent, main_frame, prefs, *, on_close=None):
        super().__init__(parent)
        self._main = main_frame
        self._prefs = prefs
        self._on_close = on_close         # callable → adds a Close button
        self._rows: list[_Row] = []       # everything loaded from the index
        self._visible: list[_Row] = []    # current filtered view (parallel to list rows)
        self._adult_hidden = 0            # count hidden by the adult filter, for the status line
        self._sort_attr = "title"         # current sort field (row attribute)
        self._sort_desc = False
        self._build_ui()
        self.reload()

    # ── UI ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Search + adult toggle row.
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(
            wx.StaticText(self, label="&Search:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.search_ctrl = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetName("Search stories")
        self.search_ctrl.SetHint("title, author, or fandom")
        set_help(
            self.search_ctrl,
            "Filter the list as you type — matches story title, author, "
            "or fandom. Clear the box to show everything again.",
        )
        self.search_ctrl.Bind(wx.EVT_TEXT, self._on_search)
        top.Add(self.search_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        top.Add(
            wx.StaticText(self, label="S&ort by:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.sort_ctrl = wx.Choice(
            self, choices=[label for label, _ in _SORT_CHOICES],
        )
        self.sort_ctrl.SetSelection(0)
        self.sort_ctrl.SetName("Sort stories by")
        set_help(
            self.sort_ctrl,
            "Order the story list — by title, author, fandom, format, "
            "library, or the date each story was added. Clicking a column "
            "header sorts by that column too (click again to reverse).",
        )
        self.sort_ctrl.Bind(wx.EVT_CHOICE, self._on_sort_choice)
        top.Add(self.sort_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.adult_chk = wx.CheckBox(self, label="Show &adult")
        self.adult_chk.SetName("Show adult stories")
        self.adult_chk.SetValue(False)
        set_help(
            self.adult_chk,
            "Adult stories are hidden by default. Tick to include them in "
            "the list; untick to hide them again.",
        )
        self.adult_chk.Bind(wx.EVT_CHECKBOX, self._on_toggle_adult)
        top.Add(self.adult_chk, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        # Story list.
        self.list_ctrl = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
        )
        self.list_ctrl.SetName("Library stories")
        set_help(
            self.list_ctrl,
            "Every story the last scan indexed. Arrow through the list to "
            "hear each story's details below; press Enter to open the "
            "selected story in the reader.",
        )
        for col, (heading, width) in enumerate(_COLUMNS):
            self.list_ctrl.InsertColumn(col, heading, width=width)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        # FOCUSED fires on arrow-key movement even when selection state
        # doesn't visibly change, so the summary tracks the NVDA cursor.
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_select)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_deselect)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        self.list_ctrl.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)
        sizer.Add(self.list_ctrl, 3, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.count_ctrl = wx.StaticText(self, label="")
        sizer.Add(self.count_ctrl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Read-only detail pane — mirrors the picker-dialog convention so
        # keyboard users get the full record without leaving the list.
        sizer.Add(
            wx.StaticText(self, label="&Details:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 8,
        )
        self.summary_ctrl = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130),
        )
        self.summary_ctrl.SetName("Story details")
        set_help(
            self.summary_ctrl,
            "Full details of the story highlighted in the list above — "
            "title, author, fandom, format, adult and abandoned status, "
            "file path, and source link.",
        )
        sizer.Add(self.summary_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Action buttons.
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = wx.Button(self, label="&Open in Reader")
        self.open_btn.Bind(wx.EVT_BUTTON, self._on_open)
        set_help(
            self.open_btn,
            "Open the selected story in the built-in reader "
            "(EPUB and HTML stories).",
        )
        btns.Add(self.open_btn, 0, wx.RIGHT, 6)

        self.update_btn = wx.Button(self, label="Check for &Updates")
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_update)
        set_help(
            self.update_btn,
            "Check the selected story's source site for new chapters and "
            "merge any into the existing file.",
        )
        btns.Add(self.update_btn, 0, wx.RIGHT, 6)

        self.reexport_btn = wx.Button(self, label="Re-&export...")
        self.reexport_btn.Bind(wx.EVT_BUTTON, self._on_reexport)
        set_help(
            self.reexport_btn,
            "Write the selected story out again in another format "
            "(EPUB, HTML, or plain text).",
        )
        btns.Add(self.reexport_btn, 0, wx.RIGHT, 6)

        self.path_btn = wx.Button(self, label="Copy &Path")
        self.path_btn.Bind(wx.EVT_BUTTON, self._on_copy_path)
        set_help(
            self.path_btn,
            "Copy the selected story's file path on disk to the clipboard.",
        )
        btns.Add(self.path_btn, 0, wx.RIGHT, 6)

        # Per-story management: adult classification and abandoned flag.
        # Labels are set per selection in _update_summary so the button
        # states the exact action it will take on the current story.
        self.adult_btn = wx.Button(self, label="Mark Ad&ult")
        self.adult_btn.Bind(wx.EVT_BUTTON, self._on_toggle_adult_flag)
        set_help(
            self.adult_btn,
            "Mark the selected story as adult, or clear that mark. This "
            "override wins over ficary's automatic guess, so you can fix a "
            "story that was filed wrong. Adult stories are hidden until "
            "Show adult is ticked.",
        )
        btns.Add(self.adult_btn, 0, wx.RIGHT, 6)

        self.abandon_btn = wx.Button(self, label="Mark A&bandoned")
        self.abandon_btn.Bind(wx.EVT_BUTTON, self._on_toggle_abandoned)
        set_help(
            self.abandon_btn,
            "Mark the selected work-in-progress as abandoned so update "
            "checks skip it, or revive it so it's checked again. Use this "
            "for a WIP you know the author has walked away from.",
        )
        btns.Add(self.abandon_btn, 0, wx.RIGHT, 6)

        self.delete_btn = wx.Button(self, label="&Delete...")
        self.delete_btn.Bind(wx.EVT_BUTTON, self._on_delete)
        set_help(
            self.delete_btn,
            "Delete the selected story's file from disk and drop it from "
            "the library index. Cannot be undone.",
        )
        btns.Add(self.delete_btn, 0, wx.RIGHT, 6)

        self.rescan_btn = wx.Button(self, label="Re&scan")
        self.rescan_btn.Bind(wx.EVT_BUTTON, self._on_rescan)
        set_help(
            self.rescan_btn,
            "Re-read every configured library folder from disk and refresh "
            "this list.",
        )
        btns.Add(self.rescan_btn, 0, wx.RIGHT, 6)

        # Close only exists when a host asked for it (the standalone
        # window). The embedded main-window view has nothing to close.
        if self._on_close is not None:
            close_btn = wx.Button(self, id=wx.ID_CLOSE, label="&Close")
            close_btn.Bind(wx.EVT_BUTTON, lambda e: self._on_close())
            set_help(close_btn, "Close the library browser (Escape).")
            btns.Add(close_btn, 0)
        sizer.Add(btns, 0, wx.ALL, 8)

        self.SetSizer(sizer)
        self._enable_actions(False)

    # ── Data loading / filtering ───────────────────────────────

    def reload(self) -> None:
        """(Re)load rows from the on-disk index and refresh the view.

        Public so a host can refresh the list after an external change —
        e.g. the main window re-reading it once a download has been
        auto-indexed."""
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
                # An explicit per-story ``adult`` override (True/False)
                # wins over the site/folder-derived guess; only fall back
                # to the guess when no override has been set.
                override = entry.get("adult")
                derived_adult = (
                    root_is_adult or adapter in ADULT_FICTION_ADAPTERS
                )
                is_adult = (
                    bool(override) if override is not None else derived_adult
                )
                self._rows.append(_Row(
                    url=url,
                    title=entry.get("title") or "(untitled)",
                    author=entry.get("author") or "",
                    fandom=", ".join(fandoms),
                    fmt=entry.get("format") or "",
                    root=root,
                    abs_path=str(root / rel) if rel else "",
                    is_adult=is_adult,
                    library_label="Adult" if root_is_adult else (root.name or root_str),
                    adult_overridden=override is not None,
                    is_abandoned=bool(entry.get("abandoned_at")),
                    added_at=str(entry.get("added_at") or ""),
                ))
        self._apply_sort()
        self._apply_filter()

    def _apply_sort(self) -> None:
        """Sort ``self._rows`` by the current field/direction. Strings
        compare case-insensitively; the ISO ``added_at`` stamps compare
        lexicographically (which is chronological); rows missing the
        stamp (indexed before it existed) sort after dated rows under
        "newest first". Title breaks ties so equal keys stay stable and
        predictable."""
        attr = self._sort_attr
        desc = self._sort_desc

        def key(row: _Row):
            value = getattr(row, attr, "") or ""
            return (str(value).lower(), row.title.lower(), row.author.lower())

        self._rows.sort(key=key, reverse=desc)

    def _sort_description(self) -> str:
        for label, (attr, desc) in _SORT_CHOICES:
            if attr == self._sort_attr and desc == self._sort_desc:
                return label
        heading = dict(zip(_COLUMN_SORT_ATTRS, (c[0] for c in _COLUMNS))).get(
            self._sort_attr, self._sort_attr,
        )
        return f"{heading} ({'descending' if self._sort_desc else 'ascending'})"

    def _resort(self) -> None:
        """Re-sort, refresh the view, and reflect the order in the count
        line so the active sort is visible (and readable) at a glance."""
        self._apply_sort()
        self._apply_filter()
        self.count_ctrl.SetLabel(
            f"{self.count_ctrl.GetLabel()} — sorted by {self._sort_description()}"
        )

    def _on_sort_choice(self, event: wx.Event) -> None:
        i = self.sort_ctrl.GetSelection()
        if not (0 <= i < len(_SORT_CHOICES)):
            return
        self._sort_attr, self._sort_desc = _SORT_CHOICES[i][1]
        self._resort()

    def _on_col_click(self, event: wx.Event) -> None:
        col = event.GetColumn()
        if not (0 <= col < len(_COLUMN_SORT_ATTRS)):
            return
        attr = _COLUMN_SORT_ATTRS[col]
        if attr == self._sort_attr:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_attr = attr
            # Dates default to newest-first — the useful order; text
            # columns default ascending.
            self._sort_desc = attr == "added_at"
        # Mirror into the dropdown when an option matches, so the
        # accessible control always reflects reality.
        for i, (_label, (a, d)) in enumerate(_SORT_CHOICES):
            if a == self._sort_attr and d == self._sort_desc:
                self.sort_ctrl.SetSelection(i)
                break
        self._resort()

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
            self.list_ctrl.SetItem(i, 5, _added_display(row.added_at))

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
        if row.is_adult:
            adult_state = (
                "yes (you set this)" if row.adult_overridden
                else "yes (detected)"
            )
        else:
            adult_state = (
                "no (you set this)" if row.adult_overridden
                else "no"
            )
        self._set_summary("\n".join([
            f"Title: {row.title}",
            f"Author: {row.author or '(unknown)'}",
            f"Fandom: {row.fandom or '(none)'}",
            f"Format: {row.fmt or '(unknown)'}",
            f"Library: {row.library_label}",
            f"Adult: {adult_state}",
            f"Abandoned: {'yes' if row.is_abandoned else 'no'}",
            f"Added: {_added_display(row.added_at) or '(unknown)'}",
            f"File: {row.abs_path or '(missing path)'}",
            f"Source: {row.url}",
        ]))
        # Each toggle button states the action it will take on this story.
        self.adult_btn.SetLabel(
            "Mark Not Ad&ult" if row.is_adult else "Mark Ad&ult"
        )
        self.abandon_btn.SetLabel(
            "&Revive Story" if row.is_abandoned else "Mark A&bandoned"
        )

    def _set_summary(self, text: str) -> None:
        self.summary_ctrl.SetValue(text)

    def _enable_actions(self, enabled: bool) -> None:
        for btn in (
            self.open_btn, self.update_btn, self.reexport_btn,
            self.path_btn, self.adult_btn, self.abandon_btn, self.delete_btn,
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

    def _entry_for_row(self, idx, row: _Row):
        """Return the mutable index entry dict for ``row``, or None.

        ``row.url`` is whatever key ``stories_in`` yielded. Try the
        canonicalising lookup first (the normal path), then fall back to
        an exact stored-key match so a row whose key isn't canonical —
        the same case ``LibraryIndex.remove`` guards against — can still
        be mutated instead of silently missed."""
        entry = idx.lookup_by_url(row.root, row.url)
        if entry is not None:
            return entry
        for url, candidate in idx.stories_in(row.root):
            if url == row.url:
                return candidate
        return None

    def _reselect_by_url(self, url: str) -> bool:
        """After a reload, reselect the row for ``url`` if it's still in
        the filtered view (a story hidden by the adult filter won't be),
        so the user's cursor doesn't jump. Returns whether it was found."""
        for i, row in enumerate(self._visible):
            if row.url == url:
                self.list_ctrl.Select(i)
                self.list_ctrl.Focus(i)
                self._update_summary(row)
                self._enable_actions(True)
                return True
        return False

    def _on_toggle_adult_flag(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        new_adult = not row.is_adult
        try:
            from .index import LibraryIndex
            idx = LibraryIndex.load()
            entry = self._entry_for_row(idx, row)
            if entry is None:
                wx.MessageBox(
                    "This story is no longer in the index. Rescan and try "
                    "again.",
                    "Library Browser", wx.OK | wx.ICON_WARNING, self,
                )
                return
            entry["adult"] = new_adult
            idx.save()
        except Exception as exc:
            wx.MessageBox(
                f"Could not update the story:\n\n{exc}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            return
        self.reload()
        found = self._reselect_by_url(row.url)
        msg = (
            f"Marked “{row.title}” as adult."
            if new_adult else
            f"Marked “{row.title}” as not adult."
        )
        if new_adult and not found and not self.adult_chk.GetValue():
            msg += " It's now hidden — tick Show adult to see it."
        self.count_ctrl.SetLabel(msg)

    def _on_toggle_abandoned(self, event: wx.Event) -> None:
        row = self._selected_row()
        if row is None:
            return
        reviving = row.is_abandoned
        try:
            from .index import LibraryIndex
            from .abandoned import mark_abandoned_urls, revive_abandoned
            idx = LibraryIndex.load()
            if reviving:
                report = revive_abandoned(idx, urls=[row.url], roots=[row.root])
                changed = bool(report.revived)
            else:
                report = mark_abandoned_urls(idx, [row.url], roots=[row.root])
                changed = bool(report.newly_marked)
            if changed:
                idx.save()
        except Exception as exc:
            wx.MessageBox(
                f"Could not update the story:\n\n{exc}",
                "Library Browser", wx.OK | wx.ICON_ERROR, self,
            )
            return
        self.reload()
        self._reselect_by_url(row.url)
        self.count_ctrl.SetLabel(
            f"Revived “{row.title}” — it will be checked for "
            "updates again."
            if reviving else
            f"Marked “{row.title}” abandoned — update checks "
            "will skip it until you revive it."
        )

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
        self.reload()

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
        self.reload()

    def _on_search(self, event: wx.Event) -> None:
        self._apply_filter()

    def _on_toggle_adult(self, event: wx.Event) -> None:
        self._apply_filter()


class LibraryBrowserFrame(wx.Frame):
    """Standalone Library window hosting a :class:`LibraryPanel`.

    Thin wrapper: the panel holds all the list logic and actions; this
    frame adds the window chrome (title, size), the Escape-to-close
    accelerator, and the close notification back to the main window.
    Attribute access falls through to the panel, so existing callers and
    tests that reach ``frame.list_ctrl`` / ``frame._rows`` / ``frame._on_*``
    keep working after the panel extraction."""

    def __init__(self, main_frame: wx.Window, prefs):
        super().__init__(
            main_frame, title="Library Browser", size=(900, 620),
        )
        self._main = main_frame
        self.panel = LibraryPanel(self, main_frame, prefs, on_close=self.Close)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self._install_escape_accel()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def __getattr__(self, name):
        # Only reached when normal attribute lookup fails — i.e. for the
        # panel's Python attributes, never for wx's own methods (those
        # resolve on the class). Lets frame.<x> mean panel.<x> without
        # enumerating every attribute the panel exposes.
        panel = self.__dict__.get("panel")
        if panel is not None:
            return getattr(panel, name)
        raise AttributeError(name)

    def _install_escape_accel(self) -> None:
        close_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=int(close_id))
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, int(close_id)),
        ]))

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
