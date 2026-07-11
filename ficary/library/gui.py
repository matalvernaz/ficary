"""wxPython dialogs for the library manager.

Imported lazily from the main GUI so the rest of ``ficary.library``
stays wx-free for CLI use. Two dialogs:

* ``LibraryDialog`` — hub for library settings (path, template,
  misc folder) and the Scan / Reorganize entry points.
* ``ReorganizePreviewDialog`` — CheckListBox-based dry-run review,
  each row toggleable before applying.

NVDA reads CheckListBox check state natively on current wxPython, so
no row-prefix workaround is needed — the historical ``[x] / [ ]``
pattern was dropped to match the convention in ``gui_dialogs.py``.
Long operations (scan, apply) run on a worker thread and report back
through ``wx.CallAfter``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import wx

logger = logging.getLogger(__name__)

from .. import prefs as _prefs
from ..gui_help import set_help
from .gui_logic import format_move_label
from .index import LibraryIndex
from .refresh import build_refresh_queue, default_refresh_args
from .reorganizer import MoveOp, apply as apply_moves, plan
from .review import promote_untrackable, untrackable_for_root
from .scanner import scan
from .template import DEFAULT_MISC_FOLDER, DEFAULT_TEMPLATE


_TEMPLATE_HINT = (
    "Placeholders: {fandom} {title} {author} {ext} {rating} {status}. "
    "Forward slashes separate path components."
)


class LibraryFrame(wx.Frame):
    """Hub for library settings + scan/reorganize actions.

    Modeless so a scan or update run can be launched and the main
    window stays interactive — the user can start downloading a new
    story while the library check grinds on in the background.
    """

    def __init__(self, parent: wx.Window, prefs):
        super().__init__(
            parent,
            title="Library",
            size=(640, 460),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self._prefs = prefs
        # _alive guards worker-thread callbacks — they fire through
        # wx.CallAfter and can land after the window is destroyed
        # (user closed it mid-scan). EVT_CLOSE flips the flag before
        # wx tears down the widgets.
        self._alive = True
        # Cancel plumbing for the long Check-for-Updates run. The flag
        # stays None except while a worker is alive; the worker owns
        # the corresponding ``threading.Event`` and polls it between
        # probes and between downloads.
        self._update_cancel_event: threading.Event | None = None
        self._update_running = False
        self.Bind(wx.EVT_CLOSE, self._on_close_event)
        self._build_ui()
        self._load_prefs()
        # Escape closes the window (mirrors the old dialog affordance).
        self._install_escape_accel()

    # ── UI construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Scan a library of story files from any source and "
                    "keep it sorted by category."
                ),
            ),
            0, wx.ALL, 8,
        )

        # ── Library path ────────────────────────────────
        path_row = wx.BoxSizer(wx.HORIZONTAL)
        path_row.Add(
            wx.StaticText(panel, label="Library &folder:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.path_ctrl = wx.TextCtrl(panel)
        self.path_ctrl.SetName("Library folder")
        set_help(
            self.path_ctrl,
            "The main folder ficary scans and keeps sorted. New downloads "
            "are filed into fandom subfolders here.",
        )
        path_row.Add(self.path_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        browse_btn = wx.Button(panel, label="&Browse...")
        set_help(browse_btn, "Pick the library folder with a folder chooser.")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        path_row.Add(browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(path_row, 0, wx.EXPAND | wx.ALL, 8)

        # ── Adult library path (optional separate root) ──
        adult_row = wx.BoxSizer(wx.HORIZONTAL)
        adult_row.Add(
            wx.StaticText(panel, label="A&dult library folder (optional):"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.adult_path_ctrl = wx.TextCtrl(panel)
        self.adult_path_ctrl.SetName("Adult library folder")
        set_help(
            self.adult_path_ctrl,
            "Optional separate location for adult-only downloads "
            "(Literotica, AFF, etc.). When set, those stories are saved "
            "here instead of an Adult subfolder inside your main library, "
            "and the browser lists and hides them independently. Leave "
            "blank to keep them under <library>/Adult.",
        )
        adult_row.Add(
            self.adult_path_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        adult_browse_btn = wx.Button(panel, label="Bro&wse...")
        set_help(
            adult_browse_btn,
            "Pick the separate adult-library folder with a folder chooser.",
        )
        adult_browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_adult)
        adult_row.Add(adult_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(adult_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Template ────────────────────────────────────
        tmpl_row = wx.BoxSizer(wx.HORIZONTAL)
        tmpl_row.Add(
            wx.StaticText(panel, label="Path &template:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.template_ctrl = wx.TextCtrl(panel)
        self.template_ctrl.SetName("Path template")
        set_help(
            self.template_ctrl,
            "How files are named and foldered inside the library, using "
            "fields like {fandom}, {title}, {author}, and {ext}. Use "
            "Reset to restore the default.",
        )
        tmpl_row.Add(self.template_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        reset_btn = wx.Button(panel, label="&Reset")
        set_help(reset_btn, "Restore the default path template.")
        reset_btn.Bind(
            wx.EVT_BUTTON,
            lambda e: self.template_ctrl.SetValue(DEFAULT_TEMPLATE),
        )
        tmpl_row.Add(reset_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(tmpl_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(
            wx.StaticText(panel, label=_TEMPLATE_HINT),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8,
        )

        # ── Misc folder ─────────────────────────────────
        misc_row = wx.BoxSizer(wx.HORIZONTAL)
        misc_row.Add(
            wx.StaticText(panel, label="&Miscellaneous folder name:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.misc_ctrl = wx.TextCtrl(panel)
        self.misc_ctrl.SetName("Miscellaneous folder name")
        set_help(
            self.misc_ctrl,
            "Folder name for stories whose fandom can't be determined, or "
            "crossovers that span several fandoms. Defaults to Misc.",
        )
        misc_row.Add(self.misc_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(misc_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Abandoned-WIP threshold ────────────────────────
        abandoned_row = wx.BoxSizer(wx.HORIZONTAL)
        abandoned_row.Add(
            wx.StaticText(
                panel,
                label="Mark WIPs as &abandoned after (days; 0 = off):",
            ),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        # SpinCtrl rather than free-form TextCtrl because the value
        # is always an integer day count; 9999 is a generous upper
        # bound (~27 years — past the point where anyone would
        # meaningfully want to keep probing) and keeps the widget
        # from scrolling into nonsense territory.
        self.abandoned_after_ctrl = wx.SpinCtrl(
            panel, min=0, max=9999, initial=0,
        )
        self.abandoned_after_ctrl.SetName("Abandoned-after threshold in days")
        set_help(
            self.abandoned_after_ctrl,
            "When Scan Library runs, unfinished stories whose file hasn't "
            "changed in this many days are marked abandoned and skipped in "
            "later update checks. Set 0 to turn this off. Around 730 "
            "(two years) is a reasonable starting point.",
        )
        abandoned_row.Add(self.abandoned_after_ctrl, 0, wx.RIGHT, 6)

        self.manage_abandoned_btn = wx.Button(
            panel, label="Manage A&bandoned...",
        )
        set_help(
            self.manage_abandoned_btn,
            "Review the list of stories marked abandoned and revive any "
            "you want checked for updates again.",
        )
        self.manage_abandoned_btn.Bind(
            wx.EVT_BUTTON, self._on_manage_abandoned,
        )
        abandoned_row.Add(
            self.manage_abandoned_btn, 0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        sizer.Add(abandoned_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Status pane ─────────────────────────────────
        sizer.Add(
            wx.StaticText(panel, label="S&tatus:"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.status_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 120),
        )
        self.status_ctrl.SetName("Library status")
        set_help(
            self.status_ctrl,
            "Progress and results of scans and update checks appear here.",
        )
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # ── Action buttons ──────────────────────────────
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.scan_btn = wx.Button(panel, label="&Scan Library")
        set_help(
            self.scan_btn,
            "Read every story file in the library folder, record its "
            "details, and build the index the browser and update checks "
            "use. Run this once, and again after adding files by hand.",
        )
        self.scan_btn.Bind(wx.EVT_BUTTON, self._on_scan)
        btn_row.Add(self.scan_btn, 0, wx.RIGHT, 6)

        self.reorg_btn = wx.Button(panel, label="&Reorganize...")
        set_help(
            self.reorg_btn,
            "Move existing files to match the current path template and "
            "sorting rules. Shows the planned moves first before applying.",
        )
        self.reorg_btn.Bind(wx.EVT_BUTTON, self._on_reorganize)
        btn_row.Add(self.reorg_btn, 0, wx.RIGHT, 6)

        self.update_btn = wx.Button(panel, label="Check for &Updates")
        set_help(
            self.update_btn,
            "Check every indexed story for new chapters and merge any it "
            "finds. Finished and abandoned stories are skipped.",
        )
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_check_updates)
        btn_row.Add(self.update_btn, 0, wx.RIGHT, 6)

        # Update-mode modifiers live next to the button so keyboard
        # users can tab from Update → Force recheck → Fresh copies and
        # pick any combination (force + fresh, force alone, fresh alone,
        # or neither). Resets on dialog close — "fresh copies" is a
        # slow operation and a sticky toggle is a silent footgun.
        self.force_recheck_chk = wx.CheckBox(
            panel, label="&Force recheck (bypass TTL)",
        )
        self.force_recheck_chk.SetName("Force recheck — bypass TTL")
        set_help(
            self.force_recheck_chk,
            "Ignore the recent-check timer and the finished/abandoned "
            "skip — check every indexed story against its site even if it "
            "was just checked or marked finished.",
        )
        btn_row.Add(
            self.force_recheck_chk, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )

        self.refetch_all_chk = wx.CheckBox(
            panel, label="Fres&h copies (re-download all chapters)",
        )
        self.refetch_all_chk.SetName(
            "Fresh copies — re-download every chapter"
        )
        set_help(
            self.refetch_all_chk,
            "Re-download every chapter from the site instead of merging "
            "only new ones with what's on disk. Slower, but catches silent "
            "author edits to old chapters.",
        )
        btn_row.Add(
            self.refetch_all_chk, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )

        self.review_btn = wx.Button(panel, label="Review &Ambiguous...")
        set_help(
            self.review_btn,
            "Review stories the scanner couldn't confidently identify and "
            "correct their details by hand.",
        )
        self.review_btn.Bind(wx.EVT_BUTTON, self._on_review)
        btn_row.Add(self.review_btn, 0, wx.RIGHT, 6)

        self.browse_stories_btn = wx.Button(panel, label="&Open Story Browser...")
        set_help(
            self.browse_stories_btn,
            "Open the library browser: a searchable list of every story "
            "you can read, update, mark adult, abandon, or delete.",
        )
        self.browse_stories_btn.Bind(wx.EVT_BUTTON, self._on_open_browser)
        btn_row.Add(self.browse_stories_btn, 0, wx.RIGHT, 6)

        # Cancel button sits next to Check for Updates so the stop
        # action is adjacent to the start action. Disabled unless a
        # run is in flight.
        self.cancel_btn = wx.Button(panel, label="Ca&ncel Update")
        set_help(
            self.cancel_btn,
            "Stop the update check that's currently running. Enabled only "
            "while a run is in progress.",
        )
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel_update)
        self.cancel_btn.Disable()
        btn_row.Add(self.cancel_btn, 0, wx.RIGHT, 6)

        btn_row.AddStretchSpacer(1)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        set_help(close_btn, "Close the library window.")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

    # ── Preference plumbing ────────────────────────────────────

    def _load_prefs(self) -> None:
        self.path_ctrl.SetValue(self._prefs.get(_prefs.KEY_LIBRARY_PATH, "") or "")
        self.adult_path_ctrl.SetValue(
            self._prefs.get(_prefs.KEY_LIBRARY_ADULT_PATH, "") or ""
        )
        self.template_ctrl.SetValue(
            self._prefs.get(_prefs.KEY_LIBRARY_PATH_TEMPLATE) or DEFAULT_TEMPLATE
        )
        self.misc_ctrl.SetValue(
            self._prefs.get(_prefs.KEY_LIBRARY_MISC_FOLDER) or DEFAULT_MISC_FOLDER
        )
        try:
            raw = self._prefs.get(_prefs.KEY_LIBRARY_ABANDONED_AFTER_DAYS) or "0"
            self.abandoned_after_ctrl.SetValue(max(0, int(raw)))
        except (TypeError, ValueError):
            self.abandoned_after_ctrl.SetValue(0)

    def _save_prefs(self) -> None:
        self._prefs.set(_prefs.KEY_LIBRARY_PATH, self.path_ctrl.GetValue())
        self._prefs.set(
            _prefs.KEY_LIBRARY_ADULT_PATH, self.adult_path_ctrl.GetValue()
        )
        self._prefs.set(
            _prefs.KEY_LIBRARY_PATH_TEMPLATE, self.template_ctrl.GetValue()
        )
        self._prefs.set(
            _prefs.KEY_LIBRARY_MISC_FOLDER, self.misc_ctrl.GetValue()
        )
        self._prefs.set(
            _prefs.KEY_LIBRARY_ABANDONED_AFTER_DAYS,
            str(int(self.abandoned_after_ctrl.GetValue())),
        )

    def trigger_update_check(self) -> None:
        """Public entry point for the main window's Ctrl+U accelerator.

        Routes through the same handler as the Check-for-Updates button
        (reading the adjacent force/refetch checkboxes). No-ops quietly
        if a run is already in flight."""
        if self._update_running:
            return
        self._on_check_updates(None)

    def _current_path(self) -> Path | None:
        raw = (self.path_ctrl.GetValue() or "").strip()
        if not raw:
            wx.MessageBox(
                "Choose a library folder first.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return None
        root = Path(raw).expanduser()
        if not root.is_dir():
            wx.MessageBox(
                f"{root} is not a directory.",
                "Library", wx.OK | wx.ICON_ERROR, self,
            )
            return None
        return root

    # ── Event handlers ─────────────────────────────────────────

    def _on_browse(self, event: wx.Event) -> None:
        # No fallback to Path.home(): on frozen builds portable.setup_env
        # redirects HOME to the exe folder, so defaulting there opened the
        # picker inside the app's own directory. An empty defaultPath lets
        # wx open at a sensible OS location instead.
        dlg = wx.DirDialog(
            self, "Choose library folder",
            defaultPath=self.path_ctrl.GetValue() or "",
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.path_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_browse_adult(self, event: wx.Event) -> None:
        dlg = wx.DirDialog(
            self, "Choose separate adult-library folder",
            defaultPath=self.adult_path_ctrl.GetValue() or "",
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.adult_path_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_open_browser(self, event: wx.Event) -> None:
        """Open the library browser via the main window (single instance)."""
        self._save_prefs()
        opener = getattr(self.GetParent(), "_open_library_browser", None)
        if callable(opener):
            opener()

    def _append_status(self, line: str) -> None:
        self.status_ctrl.AppendText(line + "\n")

    def _set_busy(self, busy: bool) -> None:
        self.scan_btn.Enable(not busy)
        self.reorg_btn.Enable(not busy)
        self.update_btn.Enable(not busy)
        self.force_recheck_chk.Enable(not busy)
        self.refetch_all_chk.Enable(not busy)
        self.review_btn.Enable(not busy)
        # Cancel is only useful while an update run is live; scan and
        # reorganize are short-lived phase-2-only operations so they
        # don't wire into cancel_event at all.
        self.cancel_btn.Enable(busy and self._update_running)

    def _install_escape_accel(self) -> None:
        """Make Escape close the frame, same affordance the old dialog had."""
        close_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=int(close_id))
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, int(close_id)),
        ]))

    def _on_cancel_update(self, event: wx.Event) -> None:
        if self._update_cancel_event is None or not self._update_running:
            return
        self._update_cancel_event.set()
        self._append_status("Cancel requested — finishing current step...")
        self.cancel_btn.Disable()

    def _post_status(self, line: str) -> None:
        """Thread-safe status-pane append. Used as the progress callback
        for long-running worker-thread operations.

        Lines emitted on a per-site download-queue worker pick up a
        ``[<site>] `` prefix so two sites running concurrently during
        a library update-all Phase 3 don't interleave into an
        unreadable mash.
        """
        if not self._alive:
            return
        from ..download_queue import site_from_thread_name
        site = site_from_thread_name(threading.current_thread().name)
        if site and line and line.strip():
            line = f"[{site}] {line}"
        wx.CallAfter(self._append_status_if_alive, line)

    def _append_status_if_alive(self, line: str) -> None:
        if self._alive:
            self._append_status(line)

    def _on_scan(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        # Scan the separate adult-library root too when configured, so a
        # single Scan keeps the whole index (main + adult) fresh and the
        # browser sees both. A blank or not-yet-created adult folder is
        # skipped with a note rather than treated as an error.
        roots = [root]
        adult_raw = (self.adult_path_ctrl.GetValue() or "").strip()
        if adult_raw:
            adult_root = Path(adult_raw).expanduser()
            if adult_root.is_dir() and adult_root.resolve() != root.resolve():
                roots.append(adult_root)
            elif not adult_root.is_dir():
                self._append_status(
                    f"(Adult library folder {adult_root} is not a directory "
                    "yet — skipping.)"
                )
        for r in roots:
            self._append_status(f"Scanning {r}...")
        self._set_busy(True)

        def worker():
            results = []
            try:
                for r in roots:
                    results.append((r, scan(r, recursive=True)))
            except Exception as exc:
                wx.CallAfter(self._scan_failed, exc)
                return
            wx.CallAfter(self._scan_finished, results)

        threading.Thread(target=worker, daemon=True).start()

    def _report_scan_result(self, result, root: Path | None = None) -> None:
        prefix = f"{root}: " if root is not None else ""
        self._append_status(
            f"{prefix}Scanned {result.total_files} file(s): "
            f"{result.identified_via_url} tracked by URL, "
            f"{result.ambiguous} indexed-only, "
            f"{result.errors} error(s)."
        )
        if result.error_files:
            for path, msg in result.error_files[:5]:
                self._append_status(f"  error: {path.name}: {msg}")
            if len(result.error_files) > 5:
                self._append_status(
                    f"  ... and {len(result.error_files) - 5} more"
                )

    def _scan_finished(self, results) -> None:
        if not self._alive:
            return
        # ``results`` is a list of ``(root, ScanResult)``. Label per-root
        # only when more than one was scanned, so the common single-root
        # case reads exactly as before.
        multi = len(results) > 1
        for root, result in results:
            self._report_scan_result(result, root=root if multi else None)
        self._set_busy(False)
        self._notify_main_refresh()

    def _notify_main_refresh(self) -> None:
        """Reload the main window's embedded library list after a scan or
        update here changed the index on disk. Without this the embedded
        list keeps showing its startup snapshot — blank story_updated,
        missing newly-added stories — until the app restarts, which reads
        as "the update-date sort doesn't work" (the column is empty, so
        sorting it does nothing)."""
        refresh = getattr(self.GetParent(), "_refresh_library_panel", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("main library-panel refresh failed", exc_info=True)

    def _scan_failed(self, exc: Exception) -> None:
        if not self._alive:
            return
        self._append_status(f"Scan failed: {exc}")
        self._set_busy(False)

    def _on_manage_abandoned(self, event: wx.Event) -> None:
        """Open the abandoned-stories review dialog scoped to the
        library path the user has configured. Falls back to "all
        indexed libraries" if the field is empty, so the dialog is
        still usable as a cross-library audit view."""
        raw = (self.path_ctrl.GetValue() or "").strip()
        root = Path(raw).expanduser() if raw else None
        if root is not None and not root.is_dir():
            wx.MessageBox(
                f"{root} is not a directory — showing every indexed "
                "library instead.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            root = None
        dlg = AbandonedStoriesDialog(self, root)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _on_check_updates(
        self,
        event: wx.Event,
        *,
        force: bool | None = None,
        refetch_all: bool | None = None,
    ) -> None:
        """Start a library-update run.

        ``force`` and ``refetch_all`` default to reading the adjacent
        checkboxes so users tick the combination they want before
        pressing Update. The explicit kwargs are still honoured so
        programmatic callers (tests, future toolbar entries) can
        override the checkbox state without flipping UI widgets.
        """
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        if force is None:
            force = bool(self.force_recheck_chk.GetValue())
        if refetch_all is None:
            refetch_all = bool(self.refetch_all_chk.GetValue())

        roots = self._update_roots(root)

        # Describe exactly what combination is about to run so the
        # status log reflects the toggle state (easier to diagnose
        # "why is it slow" / "why did it re-probe").
        mode_bits: list[str] = []
        if force:
            mode_bits.append("ignoring recent-probe TTL")
        if refetch_all:
            mode_bits.append("re-downloading every chapter")
        scope = (
            f"{len(roots)} library roots"
            if len(roots) > 1 else str(root)
        )
        if mode_bits:
            self._append_status(f"Updating {scope} ({', '.join(mode_bits)})...")
        else:
            self._append_status(f"Checking {scope} for updates...")
        # Fresh cancel event per run — creating it up here so the
        # worker closure captures the exact instance the Cancel button
        # toggles.
        self._update_cancel_event = threading.Event()
        self._update_running = True
        self._set_busy(True)
        cancel_event = self._update_cancel_event

        def worker():
            try:
                from .refresh import DEFAULT_GUI_RECHECK_INTERVAL_S
                recheck_interval = (
                    0 if force else DEFAULT_GUI_RECHECK_INTERVAL_S
                )
                multi = len(roots) > 1
                for one_root in roots:
                    if cancel_event.is_set():
                        break
                    if multi:
                        self._post_status(f"\n— {one_root} —")
                    self._probe_one_root(
                        one_root,
                        force=force,
                        refetch_all=refetch_all,
                        recheck_interval=recheck_interval,
                        cancel_event=cancel_event,
                    )
            except Exception as exc:
                self._post_status(f"Update failed: {exc}")
            finally:
                wx.CallAfter(self._update_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _update_roots(self, root: Path) -> list[Path]:
        """Roots to probe for updates: the main library plus the separate
        adult-library root when configured. Probing only the main root
        silently skipped every adult download; mirrors _on_scan, which
        already scans both. Deduped, existing dirs only."""
        roots: list[Path] = [root]
        adult_raw = (self.adult_path_ctrl.GetValue() or "").strip()
        if adult_raw:
            adult_root = Path(adult_raw).expanduser()
            try:
                distinct = adult_root.resolve() != root.resolve()
            except OSError:
                distinct = str(adult_root) != str(root)
            if adult_root.is_dir() and distinct:
                roots.append(adult_root)
        return roots

    def _probe_one_root(
        self, root, *, force, refetch_all, recheck_interval, cancel_event,
    ) -> None:
        """Probe one library root for updates. Worker-thread body,
        factored out of _on_check_updates so the main + adult roots can
        each run it. Self-contained probe-stamp buffer per root."""
        from .. import cli
        from .index import LibraryIndex
        from .scanner import scan as rescan

        args = default_refresh_args(
            recheck_interval_s=recheck_interval,
            force_recheck=force,
            refetch_all=refetch_all,
            skip_complete=not force,
        )
        probe_queue, skipped = build_refresh_queue(
            root,
            skip_complete=not force,
            recheck_interval_s=recheck_interval,
            progress=self._post_status,
        )
        if not probe_queue and not skipped:
            self._post_status(
                f"No indexed stories for {root}. Run Scan Library first."
            )
            return

        # Surface the TTL decision up front so users who click Check for
        # Updates twice in a row can see whether the recent-probe skip is
        # firing (N stories in TTL) or whether no entries have a
        # ``last_probed`` stamp yet (fresh index) and everything queued.
        if recheck_interval > 0:
            ttl_hours = recheck_interval / 3600
            self._post_status(
                f"TTL {ttl_hours:.1f}h: {len(skipped)} recently-probed "
                f"story(ies) skipped, {len(probe_queue)} to probe. "
                f"(Click Force Full Recheck to ignore the TTL.)"
            )

        # Incremental ``last_probed`` stamping: buffer stamps and flush
        # every N probes plus once at the end, so closing the app
        # mid-probe doesn't lose all progress and force a full re-probe.
        STAMP_FLUSH_EVERY = 25
        stamp_lock = threading.Lock()
        pending_stamps: dict[str, int | None] = {}

        def _flush_stamps_locked():
            """Caller holds ``stamp_lock``. Reload the on-disk index,
            stamp pending URLs (with remote chapter counts so a later
            refresh can resume interrupted downloads), save, clear.
            Reloading per-flush merges into current disk state rather
            than overwriting with a stale in-memory copy."""
            if not pending_stamps:
                return
            try:
                idx = LibraryIndex.load()
                idx.mark_probed(root, dict(pending_stamps))
            except Exception as exc:
                logger.exception(
                    "probe-stamp flush failed (pending=%d)",
                    len(pending_stamps),
                )
                self._post_status(f"Warning: probe-stamp flush failed: {exc}")
            pending_stamps.clear()

        def on_probe_complete(url: str, remote_count: int | None = None) -> None:
            with stamp_lock:
                pending_stamps[url] = remote_count
                if len(pending_stamps) >= STAMP_FLUSH_EVERY:
                    _flush_stamps_locked()

        cli._run_update_queue(
            probe_queue, args, args.probe_workers,
            skipped_count=len(skipped),
            label="Library update",
            progress=self._post_status,
            on_probe_complete=on_probe_complete,
            cancel_event=cancel_event,
        )

        with stamp_lock:
            _flush_stamps_locked()

        try:
            rescan(root)
        except Exception as exc:
            self._post_status(
                f"Warning: post-update index refresh failed: {exc}"
            )

    def _update_finished(self) -> None:
        self._update_running = False
        self._update_cancel_event = None
        if not self._alive:
            return
        self._set_busy(False)
        self._notify_main_refresh()

    def _on_review(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        idx = LibraryIndex.load()
        untrackable = untrackable_for_root(idx, root)
        if not untrackable:
            wx.MessageBox(
                (
                    "No untrackable files in this library. "
                    "Run Scan Library first, or everything is already identified."
                ),
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        dlg = ReviewDialog(self, idx=idx, root=root, untrackable=untrackable)
        try:
            dlg.ShowModal()
            promoted = dlg.promoted_count
        finally:
            dlg.Destroy()
        if promoted:
            self._append_status(f"Review: promoted {promoted} file(s).")

    def _on_reorganize(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()

        template = self.template_ctrl.GetValue() or DEFAULT_TEMPLATE
        misc = self.misc_ctrl.GetValue() or DEFAULT_MISC_FOLDER

        try:
            moves = plan(root, template=template, misc_folder=misc)
        except Exception as exc:
            wx.MessageBox(
                f"Could not plan reorganize:\n\n{exc}",
                "Library", wx.OK | wx.ICON_ERROR, self,
            )
            return

        if not moves:
            self._append_status("Library is already organized — no moves needed.")
            wx.MessageBox(
                "This library is already organized — no moves needed.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return

        preview = ReorganizePreviewDialog(self, root=root, moves=moves)
        try:
            if preview.ShowModal() == wx.ID_OK:
                selected = preview.selected_indices()
                self._run_apply(root, moves, selected)
        finally:
            preview.Destroy()

    def _run_apply(
        self,
        root: Path,
        moves: list[MoveOp],
        selected_indices: set[int],
    ) -> None:
        self._append_status(
            f"Applying {len(selected_indices)} of {len(moves)} move(s)..."
        )
        # V2 — snapshot before the destructive op so a misdiagnosed
        # reorganize can be rolled back via --restore-index. Done on the
        # main thread before the worker fires so the snapshot is in
        # place even if the worker dies before the first move.
        try:
            from .backup import snapshot_before
            from .index import default_index_path
            snap = snapshot_before(
                f"GUI reorganize-apply on {root}", default_index_path(),
            )
            if snap is not None:
                self._append_status(f"Pre-apply index backup: {snap.name}")
        except Exception as exc:
            # Failing the backup shouldn't block the apply — but we log
            # it so a user diagnosing later knows there's no rollback
            # checkpoint for this run.
            self._append_status(f"Warning: pre-apply backup failed: {exc}")
        self._set_busy(True)

        def worker():
            try:
                result = apply_moves(
                    root, moves, selected_indices=selected_indices
                )
            except Exception as exc:
                wx.CallAfter(self._apply_failed, exc)
                return
            wx.CallAfter(self._apply_finished, result)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_finished(self, result) -> None:
        if not self._alive:
            return
        self._append_status(
            f"Applied {result.applied}, skipped {result.skipped}, "
            f"errors {result.errors}."
        )
        for msg in result.messages[:5]:
            self._append_status(f"  {msg}")
        if len(result.messages) > 5:
            self._append_status(f"  ... and {len(result.messages) - 5} more")
        self._set_busy(False)

    def _apply_failed(self, exc: Exception) -> None:
        if not self._alive:
            return
        self._append_status(f"Reorganize failed: {exc}")
        self._set_busy(False)

    def _on_close_event(self, event: wx.Event) -> None:
        # Mid-run close: confirm so the user isn't surprised when a
        # library update they kicked off evaporates. Veto the close if
        # they back out. If they say yes, flip the cancel flag so the
        # worker stops promptly; the close proceeds and the worker
        # threads finish out against a dead window (all their
        # callbacks are _alive-guarded).
        if self._update_running and self._update_cancel_event is not None:
            can_veto = event.CanVeto()
            choice = wx.MessageBox(
                "An update check is still running. Cancel it and close?",
                "Library — Update in progress",
                wx.YES_NO | wx.ICON_QUESTION, self,
            )
            if choice != wx.YES:
                if can_veto:
                    event.Veto()
                    return
            else:
                self._update_cancel_event.set()
        # Flip the alive flag before wx starts tearing down widgets so
        # any worker callback queued through wx.CallAfter sees a dead
        # window and bails instead of touching destroyed controls.
        self._alive = False
        self._save_prefs()
        # Let the main frame drop its reference so the menu item can
        # reopen cleanly next time.
        parent = self.GetParent()
        notify = getattr(parent, "_notify_library_frame_closed", None)
        if callable(notify):
            try:
                notify()
            except Exception:
                pass
        event.Skip()


# Backward-compat alias: older code (and tests) imported ``LibraryDialog``.
# The class is now a Frame but we keep the old name as a pointer so any
# lingering references keep resolving.
LibraryDialog = LibraryFrame


class ReorganizePreviewDialog(wx.Dialog):
    """Dry-run list of proposed moves with per-row checkboxes."""

    def __init__(self, parent: wx.Window, root: Path, moves: list[MoveOp]):
        super().__init__(
            parent,
            title="Reorganize Library — Preview",
            size=(820, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._root = Path(root).expanduser().resolve()
        self._moves = list(moves)
        self._build_ui()
        self._refresh_labels()
        self._set_all(True)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    f"{len(self._moves)} move(s) planned. "
                    "Tick the ones you want to apply, then press Apply "
                    "Selected. Use space to toggle the focused row."
                ),
            ),
            0, wx.ALL, 8,
        )

        top_row = wx.BoxSizer(wx.HORIZONTAL)
        select_all = wx.Button(panel, label="Select &All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_all(True))
        top_row.Add(select_all, 0, wx.RIGHT, 6)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_all(False))
        top_row.Add(select_none, 0)
        sizer.Add(top_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Planned moves")
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        apply_btn = wx.Button(panel, id=wx.ID_OK, label="&Apply Selected")
        apply_btn.SetDefault()
        btn_row.Add(apply_btn, 0, wx.RIGHT, 6)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CANCEL)

    # ── Label formatting ──────────────────────────────────────

    def _refresh_labels(self) -> None:
        checks = [self.list_ctrl.IsChecked(i) for i in range(self.list_ctrl.GetCount())]
        self.list_ctrl.Clear()
        labels = [
            format_move_label(
                op, self._root,
                checked=(checks[i] if i < len(checks) else True),
            )
            for i, op in enumerate(self._moves)
        ]
        self.list_ctrl.SetItems(labels)
        for i, op in enumerate(self._moves):
            checked = checks[i] if i < len(checks) else True
            self.list_ctrl.Check(i, checked)

    def _on_item_toggled(self, event: wx.Event) -> None:
        idx = event.GetSelection()
        checked = self.list_ctrl.IsChecked(idx)
        self.list_ctrl.SetString(
            idx, format_move_label(self._moves[idx], self._root, checked),
        )

    def _set_all(self, checked: bool) -> None:
        for i in range(len(self._moves)):
            self.list_ctrl.Check(i, checked)
            self.list_ctrl.SetString(
                i, format_move_label(self._moves[i], self._root, checked),
            )

    # ── Public ────────────────────────────────────────────────

    def selected_indices(self) -> set[int]:
        return {
            i for i in range(self.list_ctrl.GetCount())
            if self.list_ctrl.IsChecked(i)
        }


class ReviewDialog(wx.Dialog):
    """Per-file URL-entry flow for untrackable library entries.

    One file in focus at a time. User pastes a source URL for the
    selected row and clicks Promote; that file moves into the library
    index's stories list with MEDIUM confidence. Index is saved on
    every successful promotion so a mid-review crash doesn't lose
    accepted entries.
    """

    def __init__(
        self,
        parent: wx.Window,
        *,
        idx: LibraryIndex,
        root: Path,
        untrackable: list[dict],
    ):
        super().__init__(
            parent,
            title="Review Ambiguous Files",
            size=(740, 540),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._idx = idx
        self._root = Path(root).expanduser().resolve()
        self._pending = list(untrackable)
        self.promoted_count = 0
        self._build_ui()
        self._refresh_list()
        self._select_first_pending()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Pick a file, paste its source URL, and press Promote. "
                    "The file moves to the library index's tracked list so "
                    "Check for Updates can pick it up."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Untrackable files")
        self.list_ctrl.InsertColumn(0, "File", width=280)
        self.list_ctrl.InsertColumn(1, "Title", width=200)
        self.list_ctrl.InsertColumn(2, "Author", width=140)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        url_row = wx.BoxSizer(wx.HORIZONTAL)
        url_row.Add(
            wx.StaticText(panel, label="Source &URL:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.url_ctrl = wx.TextCtrl(
            panel, style=wx.TE_PROCESS_ENTER,
        )
        self.url_ctrl.SetName("Source URL")
        self.url_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_promote)
        url_row.Add(self.url_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.promote_btn = wx.Button(panel, label="&Promote")
        self.promote_btn.Bind(wx.EVT_BUTTON, self._on_promote)
        url_row.Add(self.promote_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.skip_btn = wx.Button(panel, label="&Skip")
        self.skip_btn.Bind(wx.EVT_BUTTON, self._on_skip)
        url_row.Add(self.skip_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(url_row, 0, wx.EXPAND | wx.ALL, 8)

        self.status_ctrl = wx.StaticText(panel, label="")
        self.status_ctrl.SetName("Review status")
        sizer.Add(self.status_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)

    def _refresh_list(self) -> None:
        self.list_ctrl.DeleteAllItems()
        for i, entry in enumerate(self._pending):
            row = self.list_ctrl.InsertItem(
                i, entry.get("relpath") or "(unknown path)"
            )
            self.list_ctrl.SetItem(row, 1, entry.get("title") or "")
            self.list_ctrl.SetItem(row, 2, entry.get("author") or "")

    def _select_first_pending(self) -> None:
        if self._pending:
            self.list_ctrl.Select(0)
            self.list_ctrl.Focus(0)
            self.url_ctrl.SetFocus()

    def _selected_index(self) -> int:
        return self.list_ctrl.GetFirstSelected()

    def _on_select(self, event: wx.Event) -> None:
        # Clear the URL field when the selection changes so a user
        # doesn't accidentally promote file N with the URL they typed
        # for N-1.
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel("")

    def _on_promote(self, event: wx.Event) -> None:
        i = self._selected_index()
        if i < 0 or i >= len(self._pending):
            return
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self.status_ctrl.SetLabel("Type a URL first, or press Skip.")
            return
        entry = self._pending[i]
        result = promote_untrackable(
            self._idx, self._root, entry.get("relpath") or "", url, save=True,
        )
        if not result.ok:
            self.status_ctrl.SetLabel(f"Not promoted: {result.message}")
            return
        self.promoted_count += 1
        del self._pending[i]
        self._refresh_list()
        if self._pending:
            new_i = min(i, len(self._pending) - 1)
            self.list_ctrl.Select(new_i)
            self.list_ctrl.Focus(new_i)
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel(
            f"Promoted to {result.adapter}. "
            f"{len(self._pending)} file(s) remaining."
        )
        if not self._pending:
            wx.MessageBox(
                "No more untrackable files.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )

    def _on_skip(self, event: wx.Event) -> None:
        i = self._selected_index()
        if i < 0 or i >= len(self._pending):
            return
        if i + 1 < len(self._pending):
            self.list_ctrl.Select(i + 1)
            self.list_ctrl.Focus(i + 1)
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel("")


class AbandonedStoriesDialog(wx.Dialog):
    """Review, revive, or bulk-clear abandoned-WIP markings.

    Every row shows ``title — author  [marked YYYY-MM-DD]`` in a
    ``wx.ListCtrl`` so NVDA speaks the story identity plus the
    mark date as one unit. Revive operates on the selected row
    (single or multi-select); Revive All drops every flag in the
    current scope. Scope is the library root passed in by
    ``LibraryFrame._on_manage_abandoned``; ``None`` scopes to every
    indexed library (cross-library audit).
    """

    def __init__(self, parent: wx.Window, root: Path | None):
        super().__init__(
            parent, title="Abandoned stories",
            size=(720, 440),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._root = root
        self._rows: list = []
        self._build_ui()
        self._refresh_rows()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        scope_text = (
            f"Library: {self._root}"
            if self._root is not None
            else "Scope: every indexed library"
        )
        sizer.Add(
            wx.StaticText(panel, label=scope_text),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Abandoned stories")
        set_help(
            self.list_ctrl,
            "Stories currently marked as abandoned work-in-progress and "
            "skipped by update checks. Select one or more, then Revive to "
            "start checking them again.",
        )
        for i, (label, width) in enumerate([
            ("Title", 260), ("Author", 150),
            ("Marked", 110), ("Path", 400),
        ]):
            self.list_ctrl.InsertColumn(i, label, width=width)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.revive_btn = wx.Button(panel, label="&Revive selected")
        set_help(
            self.revive_btn,
            "Clear the abandoned mark on the selected stor(y/ies) so they "
            "are checked for updates again.",
        )
        self.revive_btn.Bind(wx.EVT_BUTTON, self._on_revive_selected)
        btn_row.Add(self.revive_btn, 0, wx.RIGHT, 6)

        self.revive_all_btn = wx.Button(panel, label="Revive &all")
        set_help(
            self.revive_all_btn,
            "Clear the abandoned mark on every story in the current scope "
            "(asks for confirmation first).",
        )
        self.revive_all_btn.Bind(wx.EVT_BUTTON, self._on_revive_all)
        btn_row.Add(self.revive_all_btn, 0, wx.RIGHT, 6)

        btn_row.AddStretchSpacer(1)
        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        set_help(close_btn, "Close this dialog.")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        self.SetEscapeId(wx.ID_CLOSE)
        panel.SetSizer(sizer)

    def _refresh_rows(self) -> None:
        from .abandoned import list_abandoned

        idx = LibraryIndex.load()
        roots_arg = [self._root] if self._root is not None else None
        self._rows = list_abandoned(idx, roots=roots_arg)
        self.list_ctrl.DeleteAllItems()
        for i, row in enumerate(self._rows):
            marked = row.abandoned_at[:10] if row.abandoned_at else ""
            self.list_ctrl.InsertItem(i, row.title or "(no title)")
            self.list_ctrl.SetItem(i, 1, row.author or "(no author)")
            self.list_ctrl.SetItem(i, 2, marked)
            self.list_ctrl.SetItem(i, 3, row.relpath or "")
        self._update_button_state()

    def _update_button_state(self) -> None:
        has_rows = bool(self._rows)
        self.revive_btn.Enable(has_rows)
        self.revive_all_btn.Enable(has_rows)

    def _selected_urls(self) -> list[str]:
        urls: list[str] = []
        i = -1
        while True:
            i = self.list_ctrl.GetNextSelected(i)
            if i < 0:
                break
            if 0 <= i < len(self._rows):
                urls.append(self._rows[i].url)
        return urls

    def _on_revive_selected(self, event: wx.Event) -> None:
        urls = self._selected_urls()
        if not urls:
            wx.MessageBox(
                "Select one or more rows first.",
                "Abandoned stories", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        self._do_revive(urls)

    def _on_revive_all(self, event: wx.Event) -> None:
        if not self._rows:
            return
        if wx.MessageBox(
            f"Revive all {len(self._rows)} abandoned stor"
            f"{'y' if len(self._rows) == 1 else 'ies'} in the current "
            "scope? They will be re-included in the next update check.",
            "Confirm revive all",
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        self._do_revive(None)

    def _do_revive(self, urls: list[str] | None) -> None:
        from .abandoned import revive_abandoned

        idx = LibraryIndex.load()
        roots_arg = [self._root] if self._root is not None else None
        # ``urls=None`` is only reachable via the "Revive all" button,
        # which prompts a YES/NO confirm before getting here — pass the
        # explicit opt-in so the safety gate fires only on programming
        # mistakes, not on the legitimate UI path.
        report = revive_abandoned(
            idx, urls=urls, roots=roots_arg, revive_all=(urls is None),
        )
        if report.revived:
            idx.save()
        self._refresh_rows()
        wx.MessageBox(
            report.summary(),
            "Abandoned stories",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )


