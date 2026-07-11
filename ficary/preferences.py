"""Unified Preferences dialog.

Before this dialog existed, preferences were scattered across the main
download form (format, name template, output dir, HR-as-stars, strip
notes, speech rate, attribution backend/size), the View menu (log
level, save log to file), the File menu (warn before closing), and
the Library dialog. A handful of keys — ``check_updates``, the
Pushover/Discord/email notification credentials, the watchlist poll
interval — had no GUI at all and required editing ``settings.ini``
by hand.

This dialog consolidates those knobs into one tabbed window reachable
from ``Edit → Preferences`` (Ctrl+,). Values mirrored on the main form
(format, filename template, output dir, scene-break marker, strip
notes, speech rate, attribution) are written back to both the
persistent pref *and* the live form control so a change takes effect
immediately without requiring a restart.

The Watchlist tab drives ``KEY_WATCH_AUTOPOLL`` and
``KEY_WATCH_POLL_INTERVAL_S``; flipping either triggers
:meth:`MainFrame.apply_preferences`, which calls
``WatchlistPoller.reconfigure()`` so the poll thread starts, stops, or
picks up the new interval without requiring an app restart.
"""

from __future__ import annotations

import logging

import wx

from . import attribution as _attribution_module
from . import prefs as _p


logger = logging.getLogger(__name__)


_FORMAT_CHOICES = ["epub", "html", "txt", "audio"]

# HTML title-page layouts for the "Default HTML layout" choice. Values
# mirror exporters.HTML_STYLES; the parallel labels are what the
# wx.Choice displays (index maps back to the value on save).
_HTML_STYLE_VALUES = ["modern", "classic"]
_HTML_STYLE_LABELS = [
    "Modern — heading and metadata table",
    "Classic — plain legacy-downloader layout",
]
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

# (seconds, display label) pairs for the watchlist poll-interval
# dropdown. All presets are >= watchlist.MIN_POLL_INTERVAL_S so the
# runtime clamp in WatchlistPoller never modifies what the dialog
# wrote — the 15-minute floor here is a UX choice (faster than
# 15 min is site-abusive for even modest watchlists), not the
# safety floor.
_WATCH_INTERVAL_PRESETS: list[tuple[int, str]] = [
    (15 * 60, "15 minutes"),
    (30 * 60, "30 minutes"),
    (60 * 60, "1 hour"),
    (2 * 60 * 60, "2 hours"),
    (4 * 60 * 60, "4 hours"),
    (8 * 60 * 60, "8 hours"),
    (12 * 60 * 60, "12 hours"),
    (24 * 60 * 60, "24 hours"),
]
_DEFAULT_WATCH_PRESET_INDEX = next(
    i for i, (secs, _label) in enumerate(_WATCH_INTERVAL_PRESETS)
    if secs == 60 * 60
)

_PUSHOVER_HELP = (
    "Pushover delivers watchlist alerts to your phone. Create an "
    "application at https://pushover.net/apps/build to get an API "
    "token; your user key is shown on your Pushover dashboard. Leave "
    "both blank to disable."
)
_DISCORD_HELP = (
    "Discord webhook URL — server channel settings → Integrations → "
    "Webhooks → New Webhook. Leave blank to disable."
)
_EMAIL_HELP = (
    "Recipient address for watchlist email alerts. Uses the same SMTP "
    "credentials as 'Send to Kindle' (configured via CLI "
    "--send-to-kindle). Leave blank to disable."
)


class PreferencesDialog(wx.Dialog):
    """Tabbed preferences dialog. Opens non-modally friendly (standard
    modal dialog with OK/Cancel). The owning MainFrame is responsible
    for syncing live UI controls after OK — see ``apply_to_main_frame``.
    """

    def __init__(self, parent, prefs, main_frame=None):
        super().__init__(
            parent, title="Preferences",
            size=(640, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.prefs = prefs
        self.main_frame = main_frame

        self._build_ui()
        self._load_values()
        self.Centre()

    # ── UI construction ─────────────────────────────────────────

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(panel)
        self.notebook.AddPage(self._build_general_tab(), "&General")
        self.notebook.AddPage(self._build_downloads_tab(), "&Downloads")
        self.notebook.AddPage(self._build_audiobook_tab(), "&Audiobook")
        self.notebook.AddPage(self._build_audiobookshelf_tab(), "Audio&bookshelf")
        self.notebook.AddPage(self._build_notifications_tab(), "&Notifications")
        self.notebook.AddPage(self._build_watchlist_tab(), "&Watchlist")
        self.notebook.AddPage(self._build_logging_tab(), "&Logging")
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "&OK")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        btn_row.AddButton(ok_btn)
        btn_row.AddButton(cancel_btn)
        btn_row.Realize()
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _labeled_row(self, parent, label, factory, *, help_text=None):
        """Create ``label`` and then the control, returning
        ``(row_sizer, ctrl)``.

        The StaticText MUST be constructed before the control: MSAA on
        Windows infers a control's accessible name from the nearest
        *preceding* StaticText sibling in creation order. The previous
        helper took an already-created control and made its label
        afterwards, so NVDA read every field with the prior row's
        label (or a help paragraph), and the first field on each tab
        with no label at all. ``MoveAfterInTabOrder`` only rewires the
        tab chain, not the sibling order MSAA walks, so it never fixed
        this. ``factory`` is called with ``parent`` and returns the
        control; ``help_text`` becomes the control's SetHelpText.
        """
        static = wx.StaticText(parent, label=label)
        ctrl = factory(parent)
        if help_text:
            ctrl.SetHelpText(help_text)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(static, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        return row, ctrl

    def _add_help_text(self, sizer, parent, text):
        """Wrapped small-print explanatory text below a field group."""
        st = wx.StaticText(parent, label=text)
        st.Wrap(560)
        font = st.GetFont()
        font.SetPointSize(max(8, font.GetPointSize() - 1))
        st.SetFont(font)
        sizer.Add(st, 0, wx.EXPAND | wx.ALL, 4)

    # ── Tabs ────────────────────────────────────────────────────

    def _build_general_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Output directory
        dir_row = wx.BoxSizer(wx.HORIZONTAL)
        dir_row.Add(
            wx.StaticText(panel, label="Default &output folder:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.output_dir_ctrl = wx.TextCtrl(panel)
        self.output_dir_ctrl.SetName("Default output folder")
        dir_row.Add(self.output_dir_ctrl, 1, wx.RIGHT, 4)
        browse_btn = wx.Button(panel, label="Bro&wse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_output)
        dir_row.Add(browse_btn, 0)
        sizer.Add(dir_row, 0, wx.EXPAND | wx.ALL, 6)

        # Filename template
        row, self.name_template_ctrl = self._labeled_row(
            panel, "Default &filename template:", wx.TextCtrl,
        )
        self.name_template_ctrl.SetName("Default filename template")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "Placeholders: {title}, {author}, {fandom}. Extension is "
            "appended automatically based on the chosen format.",
        )

        sizer.AddSpacer(8)

        self.check_updates_ctrl = wx.CheckBox(
            panel, label="Check for &updates automatically on launch",
        )
        self.check_updates_ctrl.SetName(
            "Check for updates automatically on launch"
        )
        sizer.Add(self.check_updates_ctrl, 0, wx.ALL, 6)

        self.confirm_close_ctrl = wx.CheckBox(
            panel, label="&Warn before closing during an active download",
        )
        self.confirm_close_ctrl.SetName(
            "Warn before closing during an active download"
        )
        sizer.Add(self.confirm_close_ctrl, 0, wx.ALL, 6)

        panel.SetSizer(sizer)
        return panel

    def _build_downloads_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        row, self.format_ctrl = self._labeled_row(
            panel, "Default &format:",
            lambda p: wx.Choice(p, choices=_FORMAT_CHOICES),
        )
        self.format_ctrl.SetName("Default format")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.html_style_ctrl = self._labeled_row(
            panel, "Default &HTML layout:",
            lambda p: wx.Choice(p, choices=_HTML_STYLE_LABELS),
            help_text=(
                "Applies to HTML output only. 'Classic' reproduces the "
                "plain title page and bare page title of legacy fanfic "
                "downloaders; chapter text is identical either way."
            ),
        )
        self.html_style_ctrl.SetName("Default HTML layout")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        sizer.AddSpacer(8)

        self.hr_stars_ctrl = wx.CheckBox(
            panel,
            label=(
                "Mark scene &breaks clearly by default "
                "(* * * in text, a silence pause in audiobooks)"
            ),
        )
        self.hr_stars_ctrl.SetName(
            "Mark scene breaks clearly by default — asterisks in text "
            "output, silence pause in audiobook output"
        )
        sizer.Add(self.hr_stars_ctrl, 0, wx.ALL, 6)

        self.strip_notes_ctrl = wx.CheckBox(
            panel, label="&Strip author's notes (A/N paragraphs) by default",
        )
        self.strip_notes_ctrl.SetName(
            "Strip author's notes by default"
        )
        sizer.Add(self.strip_notes_ctrl, 0, wx.ALL, 6)

        self.fichub_ctrl = wx.CheckBox(
            panel,
            label="Fast fanfiction.net download via Fic&Hub (may lag latest chapters)",
        )
        self.fichub_ctrl.SetName(
            "Fast fanfiction.net download via FicHub. Pulls the bulk of a "
            "fic in one request instead of the slow per-chapter crawl, then "
            "tops up any newer chapters. May lag the source by a few "
            "chapters; ignored for updates; falls back to a normal download "
            "if FicHub doesn't have the fic."
        )
        sizer.Add(self.fichub_ctrl, 0, wx.ALL, 6)

        self.merge_series_ctrl = wx.CheckBox(
            panel,
            label="Com&bine a series into one book (instead of one file per part)",
        )
        self.merge_series_ctrl.SetName(
            "Combine a series into one book instead of one file per part. "
            "Applies to AO3 and Literotica series URLs."
        )
        sizer.Add(self.merge_series_ctrl, 0, wx.ALL, 6)

        sizer.AddSpacer(8)

        # Optional per-site session cookies — set-once secrets, so they
        # live here rather than cluttering the main window. Password-
        # styled; stored plain-text in prefs like the other secrets.
        # Short accessible name matching the visible label; the long
        # explanation goes in SetHelpText, never the name.
        def _cookie(p):
            return wx.TextCtrl(p, style=wx.TE_PASSWORD)

        row, self.webnovel_cookie_ctrl = self._labeled_row(
            panel, "Webno&vel.com cookie:", _cookie,
            help_text=(
                "Paste a logged-in browser Cookie header to download "
                "chapters you have unlocked; leave blank for free chapters "
                "only. Stored locally; coins are never spent."
            ),
        )
        self.webnovel_cookie_ctrl.SetName("Webnovel.com cookie")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.ao3_cookie_ctrl = self._labeled_row(
            panel, "A&O3 cookie:", _cookie,
            help_text=(
                "Paste a logged-in browser Cookie header to download "
                "restricted works and your private bookmarks / "
                "marked-for-later; leave blank for anonymous access. "
                "Stored locally."
            ),
        )
        self.ao3_cookie_ctrl.SetName("AO3 cookie")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.scribblehub_cookie_ctrl = self._labeled_row(
            panel, "Scr&ibbleHub cookie:", _cookie,
            help_text=(
                "Paste a browser Cookie header to get past Cloudflare and, "
                "when logged in, download members-only and mature chapters; "
                "leave blank to rely on the Cloudflare solver. Stored "
                "locally."
            ),
        )
        self.scribblehub_cookie_ctrl.SetName("ScribbleHub cookie")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.subscribestar_cookie_ctrl = self._labeled_row(
            panel, "S&ubscribeStar cookie:", _cookie,
            help_text=(
                "Paste a logged-in browser Cookie header to download a "
                "creator's posts (the feed is subscriber-only, so this is "
                "required). Stored locally."
            ),
        )
        self.subscribestar_cookie_ctrl.SetName("SubscribeStar cookie")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        self._add_help_text(
            sizer, panel,
            "These set the defaults that load on launch. Format, filename, "
            "and Save-to still live on the main window; FicHub, Combine "
            "series, and the site cookies moved here since they're set "
            "once and left alone.",
        )

        panel.SetSizer(sizer)
        return panel

    def _build_audiobook_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        row, self.speech_rate_ctrl = self._labeled_row(
            panel, "Default speech &rate (%):",
            lambda p: wx.SpinCtrl(p, min=-50, max=100, initial=0, size=(90, -1)),
            help_text=(
                "Integer percent delta applied to every TTS call. "
                "-20 is 20% slower, +30 is 30% faster."
            ),
        )
        self.speech_rate_ctrl.SetName("Default speech rate percent")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "Integer percent delta applied to every TTS call. "
            "-20 is 20% slower, +30 is 30% faster.",
        )

        sizer.AddSpacer(8)

        self._attribution_choices = list(_attribution_module.available())
        display_labels = [
            _attribution_module.BACKENDS[b]["display"]
            for b in self._attribution_choices
        ]
        row, self.attribution_ctrl = self._labeled_row(
            panel, "Default &attribution backend:",
            lambda p: wx.Choice(p, choices=display_labels),
        )
        self.attribution_ctrl.SetName("Default attribution backend")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.attribution_size_ctrl = self._labeled_row(
            panel, "Default model &size (BookNLP only):", wx.TextCtrl,
        )
        self.attribution_size_ctrl.SetName(
            "Default attribution model size (blank = default)"
        )
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "Leave blank to use the backend's default. BookNLP accepts "
            "'small' or 'big'; other backends ignore this field. "
            "When a backend isn't installed, audiobook renders fall "
            "back to the builtin attributor automatically.",
        )

        panel.SetSizer(sizer)
        return panel

    def _build_audiobookshelf_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._add_help_text(
            sizer, panel,
            "Upload finished audiobooks (M4B) to an Audiobookshelf "
            "server. The token is a plain API key from your ABS user "
            "page — stored as-is, like the other credentials here. "
            "Fill in the server and token, then Fetch libraries to pick "
            "a target.",
        )

        row, self.abs_url_ctrl = self._labeled_row(
            panel, "Server &URL:", wx.TextCtrl,
        )
        self.abs_url_ctrl.SetName("Audiobookshelf server URL")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.abs_token_ctrl = self._labeled_row(
            panel, "API &token:",
            lambda p: wx.TextCtrl(p, style=wx.TE_PASSWORD),
        )
        self.abs_token_ctrl.SetName("Audiobookshelf API token")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        self.abs_fetch_btn = wx.Button(panel, label="&Fetch libraries")
        self.abs_fetch_btn.SetName("Fetch Audiobookshelf libraries")
        self.abs_fetch_btn.Bind(wx.EVT_BUTTON, self._on_abs_fetch_libraries)
        sizer.Add(self.abs_fetch_btn, 0, wx.ALL, 6)

        self._abs_library_ids: list[str] = []
        row, self.abs_library_ctrl = self._labeled_row(
            panel, "&Library:", lambda p: wx.Choice(p, choices=[]),
        )
        self.abs_library_ctrl.SetName("Audiobookshelf library")
        self.abs_library_ctrl.Bind(wx.EVT_CHOICE, self._on_abs_library_pick)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        self._abs_folder_ids: list[str] = []
        row, self.abs_folder_ctrl = self._labeled_row(
            panel, "F&older:", lambda p: wx.Choice(p, choices=[]),
        )
        self.abs_folder_ctrl.SetName("Audiobookshelf folder")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        # Cache of the last fetch so a library pick can repopulate folders
        # without another network call.
        self._abs_libraries: list[dict] = []
        self.abs_status = wx.StaticText(panel, label="")
        self.abs_status.SetName("Audiobookshelf status")
        sizer.Add(self.abs_status, 0, wx.EXPAND | wx.ALL, 6)

        panel.SetSizer(sizer)
        return panel

    def _on_abs_fetch_libraries(self, event):
        from .audiobookshelf import ABSConfigError, list_libraries
        # Persist url/token first so list_libraries reads the fresh values.
        self.prefs.set(_p.KEY_ABS_URL, self.abs_url_ctrl.GetValue().strip())
        self.prefs.set(_p.KEY_ABS_TOKEN, self.abs_token_ctrl.GetValue().strip())
        try:
            libraries = list_libraries(self.prefs)
        except ABSConfigError as exc:
            self.abs_status.SetLabel(str(exc))
            return
        except Exception as exc:
            self.abs_status.SetLabel(f"Couldn't reach the server: {exc}")
            return
        self._abs_libraries = libraries
        self._abs_library_ids = [lib["id"] for lib in libraries]
        self.abs_library_ctrl.Set([lib["name"] for lib in libraries] or ["(none)"])
        if libraries:
            self.abs_library_ctrl.SetSelection(0)
            self._on_abs_library_pick(None)
        self.abs_status.SetLabel(
            f"Found {len(libraries)} library(ies)."
        )

    def _on_abs_library_pick(self, event):
        idx = self.abs_library_ctrl.GetSelection()
        if not (0 <= idx < len(self._abs_libraries)):
            return
        folders = self._abs_libraries[idx].get("folders", [])
        self._abs_folder_ids = [f["id"] for f in folders]
        self.abs_folder_ctrl.Set(
            [f["fullPath"] or f["id"] for f in folders] or ["(default)"]
        )
        if folders:
            self.abs_folder_ctrl.SetSelection(0)

    def _build_notifications_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._add_help_text(
            sizer, panel,
            "Credentials used by the watchlist to alert you about new "
            "chapters, new works from followed authors, and new "
            "matches for saved searches. Leave a channel blank to "
            "disable it.",
        )

        # Labels carry the service name as a prefix so a screen
        # reader landing on "Pushover user key edit blank" already
        # knows which service it's configuring without having to
        # remember a separately-spoken section header. Section-
        # header StaticTexts are gone for the same reason: MSAA
        # treated the previous headers as label candidates for the
        # first following edit, which hijacked the real label.
        sizer.AddSpacer(6)

        row, self.pushover_token_ctrl = self._labeled_row(
            panel, "Pushover API &token:", wx.TextCtrl,
            help_text=_PUSHOVER_HELP,
        )
        self.pushover_token_ctrl.SetName("Pushover API token")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        row, self.pushover_user_ctrl = self._labeled_row(
            panel, "Pushover user &key:", wx.TextCtrl,
            help_text=_PUSHOVER_HELP,
        )
        self.pushover_user_ctrl.SetName("Pushover user key")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(sizer, panel, _PUSHOVER_HELP)

        sizer.AddSpacer(8)
        row, self.discord_webhook_ctrl = self._labeled_row(
            panel, "Discord &webhook URL:", wx.TextCtrl,
            help_text=_DISCORD_HELP,
        )
        self.discord_webhook_ctrl.SetName("Discord webhook URL")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(sizer, panel, _DISCORD_HELP)

        sizer.AddSpacer(8)
        row, self.notify_email_ctrl = self._labeled_row(
            panel, "Notification &email address:", wx.TextCtrl,
            help_text=_EMAIL_HELP,
        )
        self.notify_email_ctrl.SetName("Notification email address")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(sizer, panel, _EMAIL_HELP)

        panel.SetSizer(sizer)
        return panel

    def _build_watchlist_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._add_help_text(
            sizer, panel,
            "When enabled, ficary polls every watch on a schedule while "
            "the app is open and sends notifications for new chapters, "
            "new author works, and new search matches. Credentials come "
            "from the Notifications tab. Manage the list of watches "
            "from Watchlist → Manage watchlist (Ctrl+W).",
        )

        self.watch_autopoll_ctrl = wx.CheckBox(
            panel,
            label="&Automatically poll the watchlist while ficary is open",
        )
        self.watch_autopoll_ctrl.SetName(
            "Automatically poll the watchlist in the background"
        )
        sizer.Add(self.watch_autopoll_ctrl, 0, wx.ALL, 6)

        interval_labels = [label for _secs, label in _WATCH_INTERVAL_PRESETS]
        row, self.watch_interval_ctrl = self._labeled_row(
            panel, "Poll &interval:",
            lambda p: wx.Choice(p, choices=interval_labels),
        )
        self.watch_interval_ctrl.SetName("Poll interval")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "The interval is also capped internally at 5 minutes — "
            "faster polling risks tripping FFN's captcha on moderately "
            "sized watchlists.",
        )

        panel.SetSizer(sizer)
        return panel

    def _build_logging_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        row, self.log_level_ctrl = self._labeled_row(
            panel, "&Log level:",
            lambda p: wx.Choice(p, choices=_LOG_LEVELS),
        )
        self.log_level_ctrl.SetName("Log level")
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        self.log_to_file_ctrl = wx.CheckBox(
            panel, label="&Save log to file",
        )
        self.log_to_file_ctrl.SetName("Save log to file")
        sizer.Add(self.log_to_file_ctrl, 0, wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "Rotating file at <portable>/logs/ficary.log (1 MB × 3 "
            "backups). Use 'Open log folder' from the View menu to "
            "reveal it.",
        )

        panel.SetSizer(sizer)
        return panel

    # ── Load / save ────────────────────────────────────────────

    def _load_values(self):
        """Populate every control from the current prefs snapshot."""
        # General
        self.output_dir_ctrl.SetValue(self.prefs.get(_p.KEY_OUTPUT_DIR) or "")
        self.name_template_ctrl.SetValue(
            self.prefs.get(_p.KEY_NAME_TEMPLATE) or ""
        )
        self.check_updates_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_CHECK_UPDATES)
        )
        self.confirm_close_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE)
        )

        # Downloads
        fmt = (self.prefs.get(_p.KEY_FORMAT) or "epub").lower()
        if fmt in _FORMAT_CHOICES:
            self.format_ctrl.SetSelection(_FORMAT_CHOICES.index(fmt))
        else:
            self.format_ctrl.SetSelection(0)
        html_style = (self.prefs.get(_p.KEY_HTML_STYLE) or "modern").lower()
        if html_style in _HTML_STYLE_VALUES:
            self.html_style_ctrl.SetSelection(_HTML_STYLE_VALUES.index(html_style))
        else:
            self.html_style_ctrl.SetSelection(0)
        self.hr_stars_ctrl.SetValue(self.prefs.get_bool(_p.KEY_HR_AS_STARS))
        self.strip_notes_ctrl.SetValue(self.prefs.get_bool(_p.KEY_STRIP_NOTES))
        self.fichub_ctrl.SetValue(self.prefs.get_bool(_p.KEY_FICHUB))
        self.merge_series_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_MERGE_SERIES))
        self.webnovel_cookie_ctrl.SetValue(
            self.prefs.get(_p.KEY_WEBNOVEL_COOKIE) or "")
        self.ao3_cookie_ctrl.SetValue(self.prefs.get(_p.KEY_AO3_COOKIE) or "")
        self.scribblehub_cookie_ctrl.SetValue(
            self.prefs.get(_p.KEY_SCRIBBLEHUB_COOKIE) or "")
        self.subscribestar_cookie_ctrl.SetValue(
            self.prefs.get(_p.KEY_SUBSCRIBESTAR_COOKIE) or "")

        # Audiobook
        try:
            rate = int(self.prefs.get(_p.KEY_SPEECH_RATE) or "0")
        except (TypeError, ValueError):
            rate = 0
        self.speech_rate_ctrl.SetValue(max(-50, min(100, rate)))

        backend = (self.prefs.get(_p.KEY_ATTRIBUTION_BACKEND) or "builtin")
        if backend in self._attribution_choices:
            self.attribution_ctrl.SetSelection(
                self._attribution_choices.index(backend)
            )
        else:
            self.attribution_ctrl.SetSelection(0)
        self.attribution_size_ctrl.SetValue(
            self.prefs.get(_p.KEY_ATTRIBUTION_MODEL_SIZE) or ""
        )

        # Audiobookshelf
        self.abs_url_ctrl.SetValue(self.prefs.get(_p.KEY_ABS_URL) or "")
        self.abs_token_ctrl.SetValue(self.prefs.get(_p.KEY_ABS_TOKEN) or "")
        saved_lib = self.prefs.get(_p.KEY_ABS_LIBRARY_ID) or ""
        saved_folder = self.prefs.get(_p.KEY_ABS_FOLDER_ID) or ""
        if saved_lib:
            self._abs_library_ids = [saved_lib]
            self.abs_library_ctrl.Set([f"(saved: {saved_lib})"])
            self.abs_library_ctrl.SetSelection(0)
        if saved_folder:
            self._abs_folder_ids = [saved_folder]
            self.abs_folder_ctrl.Set([f"(saved: {saved_folder})"])
            self.abs_folder_ctrl.SetSelection(0)

        # Notifications
        self.pushover_token_ctrl.SetValue(
            self.prefs.get(_p.KEY_PUSHOVER_TOKEN) or ""
        )
        self.pushover_user_ctrl.SetValue(
            self.prefs.get(_p.KEY_PUSHOVER_USER) or ""
        )
        self.discord_webhook_ctrl.SetValue(
            self.prefs.get(_p.KEY_DISCORD_WEBHOOK) or ""
        )
        self.notify_email_ctrl.SetValue(
            self.prefs.get(_p.KEY_NOTIFY_EMAIL) or ""
        )

        # Watchlist
        self.watch_autopoll_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_WATCH_AUTOPOLL)
        )
        try:
            current_interval = int(
                self.prefs.get(_p.KEY_WATCH_POLL_INTERVAL_S)
                or _p.DEFAULT_WATCH_POLL_INTERVAL_S
            )
        except (TypeError, ValueError):
            current_interval = _p.DEFAULT_WATCH_POLL_INTERVAL_S
        preset_idx = next(
            (
                i for i, (secs, _label) in enumerate(_WATCH_INTERVAL_PRESETS)
                if secs == current_interval
            ),
            _DEFAULT_WATCH_PRESET_INDEX,
        )
        self.watch_interval_ctrl.SetSelection(preset_idx)

        # Logging
        level = (self.prefs.get(_p.KEY_LOG_LEVEL) or "INFO").upper()
        if level in _LOG_LEVELS:
            self.log_level_ctrl.SetSelection(_LOG_LEVELS.index(level))
        else:
            self.log_level_ctrl.SetSelection(_LOG_LEVELS.index("INFO"))
        self.log_to_file_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_LOG_TO_FILE)
        )

    def _on_browse_output(self, event):
        dlg = wx.DirDialog(
            self, "Choose default output folder",
            defaultPath=self.output_dir_ctrl.GetValue() or "",
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.output_dir_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_ok(self, event):
        self._save()
        event.Skip()

    def _save(self):
        """Write every control's value to prefs, then ask the owning
        frame to re-sync its live UI. A lot of these keys are mirrored
        on the main form; without the sync step, _save_prefs() on app
        close would overwrite the pref with the stale form value.
        """
        # General
        self.prefs.set(_p.KEY_OUTPUT_DIR, self.output_dir_ctrl.GetValue())
        self.prefs.set(_p.KEY_NAME_TEMPLATE, self.name_template_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_CHECK_UPDATES, self.check_updates_ctrl.GetValue(),
        )
        self.prefs.set_bool(
            _p.KEY_CONFIRM_CANCEL_ON_CLOSE, self.confirm_close_ctrl.GetValue(),
        )

        # Downloads
        fmt_idx = self.format_ctrl.GetSelection()
        if fmt_idx >= 0:
            self.prefs.set(_p.KEY_FORMAT, _FORMAT_CHOICES[fmt_idx])
        html_style_idx = self.html_style_ctrl.GetSelection()
        if html_style_idx >= 0:
            self.prefs.set(_p.KEY_HTML_STYLE, _HTML_STYLE_VALUES[html_style_idx])
        self.prefs.set_bool(_p.KEY_HR_AS_STARS, self.hr_stars_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_STRIP_NOTES, self.strip_notes_ctrl.GetValue(),
        )
        self.prefs.set_bool(_p.KEY_FICHUB, self.fichub_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_MERGE_SERIES, self.merge_series_ctrl.GetValue(),
        )
        self.prefs.set(
            _p.KEY_WEBNOVEL_COOKIE,
            self.webnovel_cookie_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_AO3_COOKIE, self.ao3_cookie_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_SCRIBBLEHUB_COOKIE,
            self.scribblehub_cookie_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_SUBSCRIBESTAR_COOKIE,
            self.subscribestar_cookie_ctrl.GetValue().strip(),
        )

        # Audiobook
        self.prefs.set(
            _p.KEY_SPEECH_RATE, str(self.speech_rate_ctrl.GetValue()),
        )
        b_idx = self.attribution_ctrl.GetSelection()
        if 0 <= b_idx < len(self._attribution_choices):
            self.prefs.set(
                _p.KEY_ATTRIBUTION_BACKEND, self._attribution_choices[b_idx],
            )
        self.prefs.set(
            _p.KEY_ATTRIBUTION_MODEL_SIZE,
            self.attribution_size_ctrl.GetValue().strip(),
        )

        # Audiobookshelf
        self.prefs.set(_p.KEY_ABS_URL, self.abs_url_ctrl.GetValue().strip())
        self.prefs.set(_p.KEY_ABS_TOKEN, self.abs_token_ctrl.GetValue().strip())
        lib_idx = self.abs_library_ctrl.GetSelection()
        if 0 <= lib_idx < len(self._abs_library_ids):
            self.prefs.set(_p.KEY_ABS_LIBRARY_ID, self._abs_library_ids[lib_idx])
        fold_idx = self.abs_folder_ctrl.GetSelection()
        if 0 <= fold_idx < len(self._abs_folder_ids):
            self.prefs.set(_p.KEY_ABS_FOLDER_ID, self._abs_folder_ids[fold_idx])

        # Notifications
        self.prefs.set(
            _p.KEY_PUSHOVER_TOKEN, self.pushover_token_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_PUSHOVER_USER, self.pushover_user_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_DISCORD_WEBHOOK,
            self.discord_webhook_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_NOTIFY_EMAIL, self.notify_email_ctrl.GetValue().strip(),
        )

        # Watchlist
        self.prefs.set_bool(
            _p.KEY_WATCH_AUTOPOLL, self.watch_autopoll_ctrl.GetValue(),
        )
        w_idx = self.watch_interval_ctrl.GetSelection()
        if 0 <= w_idx < len(_WATCH_INTERVAL_PRESETS):
            self.prefs.set(
                _p.KEY_WATCH_POLL_INTERVAL_S,
                str(_WATCH_INTERVAL_PRESETS[w_idx][0]),
            )

        # Logging
        lvl_idx = self.log_level_ctrl.GetSelection()
        if lvl_idx >= 0:
            self.prefs.set(_p.KEY_LOG_LEVEL, _LOG_LEVELS[lvl_idx])
        self.prefs.set_bool(
            _p.KEY_LOG_TO_FILE, self.log_to_file_ctrl.GetValue(),
        )

        if self.main_frame is not None:
            try:
                self.main_frame.apply_preferences()
            except Exception:
                logger.exception("main_frame.apply_preferences() failed")
