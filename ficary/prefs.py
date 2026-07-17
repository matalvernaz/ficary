"""Persistent GUI preferences.

Frozen Windows builds store preferences as ``settings.ini`` next to
ficary.exe (portable — no registry dependency, moves with the folder).
Non-frozen installs use ``wx.Config`` with its platform default
(dotfile on POSIX, registry on Windows) so pip-installed ficary
behaves the same as it always has. Either way the accessor methods
below stay identical.
"""

import re as _re


def llm_provider_pref_keys(provider: str) -> tuple[str, str, str]:
    """Compute the per-provider ``(model, api_key, endpoint)`` pref
    keys used by the LLM settings dialog to keep each provider's
    credentials separate.

    The active provider's values are still mirrored into the legacy
    ``llm_model`` / ``llm_api_key`` / ``llm_endpoint`` keys that the
    rest of the app reads — these per-provider keys are an *archive*
    so a user with both an OpenAI and an Anthropic key doesn't lose
    one when switching the dropdown.

    Provider names are slugified (``openai-compatible`` →
    ``openai_compatible``) so wx.Config sees stable, separator-free
    key names regardless of how the provider id is spelled. Pure
    function on a string so it's testable without wx.
    """
    slug = _re.sub(r"[^a-z0-9]+", "_", provider.lower()).strip("_")
    if not slug:
        slug = "default"
    return (
        f"llm_{slug}_model",
        f"llm_{slug}_api_key",
        f"llm_{slug}_endpoint",
    )

KEY_NAME_TEMPLATE = "name_template"
KEY_FORMAT = "format"
KEY_OUTPUT_DIR = "output_dir"
KEY_CHECK_UPDATES = "check_updates"
KEY_SKIPPED_VERSION = "skipped_update_version"
# Unix timestamp (seconds) until which the update prompt is suppressed
# even when a new version is available. Set when the user clicks
# "Remind Me Later" — without it, the prompt re-fires at every
# launch, which trains users into reflex-skipping releases.
KEY_UPDATE_SNOOZED_UNTIL = "update_snoozed_until"

# How long "Remind Me Later" silences the update prompt for. Three
# days is short enough that a user who genuinely meant "later today"
# isn't blocked from updating for a week, but long enough that a user
# launching the app daily isn't asked again the next morning.
UPDATE_SNOOZE_S = 3 * 24 * 60 * 60
KEY_HR_AS_STARS = "hr_as_stars"
KEY_STRIP_NOTES = "strip_notes"
# HTML title-page layout: "modern" (styled heading + metadata table)
# or "classic" (flat legacy-downloader paragraph list). Mirrors
# exporters.HTML_STYLE_MODERN / HTML_STYLE_CLASSIC; kept as a literal so
# this lightweight module needn't import the heavy exporters package.
KEY_HTML_STYLE = "html_style"
# Pair with KEY_STRIP_NOTES: when on, ``strip_an_via_llm`` runs after
# the regex pass to catch author's notes that the heuristic missed.
# Off by default; the LLM round-trip costs latency (and tokens on
# paid providers).
KEY_LLM_STRIP_NOTES = "llm_strip_notes"
# FFN-only: prefer FicHub's shared cache over a direct chapter-by-chapter
# scrape. Off by default — FicHub's copy can lag the latest chapters, so
# it's opt-in and ignored for updates. See ficary/fichub.py.
KEY_FICHUB = "fichub"
# Combine a downloaded series (AO3 /series/, Literotica /series/se/) into a
# single file with every part as a chapter, instead of one file per part.
# Off by default so upgrading doesn't silently change the per-part behavior
# existing users get when they paste a series URL.
KEY_MERGE_SERIES = "merge_series"
# webnovel.com logged-in session cookie (a raw "Cookie:" header string).
# Optional — lets the user pull chapters their account has unlocked.
# Plain-text in the config file, same as the LLM/Pushover/Discord secrets.
KEY_WEBNOVEL_COOKIE = "webnovel_cookie"
# AO3 logged-in session cookie (raw "Cookie:" header). Optional — unlocks
# restricted works and private bookmarks. Plain-text, same as the others.
KEY_AO3_COOKIE = "ao3_cookie"
# Browser User-Agent to pin alongside KEY_AO3_COOKIE. Cloudflare binds a
# cf_clearance cookie to the UA that solved the challenge, so a pasted
# clearance cookie only validates when this matches. Optional.
KEY_AO3_USER_AGENT = "ao3_user_agent"
# Enable the Playwright Cloudflare solver (--cf-solve) for downloads
# started from the GUI. Off by default — it needs the cf-solve optional
# feature installed and opens a browser window on a blocked download.
KEY_CF_SOLVE = "cf_solve"
# ScribbleHub cookie (raw "Cookie:" header). Optional — carries the
# Cloudflare clearance cookie so fetches get past the challenge, and
# unlocks members-only / mature chapters when logged in.
KEY_SCRIBBLEHUB_COOKIE = "scribblehub_cookie"
# SubscribeStar cookie (raw "Cookie:" header). Required for any download —
# the creator feed is subscriber-only.
KEY_SUBSCRIBESTAR_COOKIE = "subscribestar_cookie"
KEY_SPEECH_RATE = "speech_rate"
KEY_ATTRIBUTION_BACKEND = "attribution_backend"
KEY_ATTRIBUTION_MODEL_SIZE = "attribution_model_size"
# LLM attribution settings — only consulted when
# KEY_ATTRIBUTION_BACKEND == "llm". Provider is one of
# "ollama", "openai", "anthropic", "openai-compatible".
# api_key is plain-text in the config file (the same store already
# holds Pushover/Discord secrets); endpoint blank means the provider's
# default (Ollama: localhost:11434, OpenAI: api.openai.com/v1, ...).
KEY_LLM_PROVIDER = "llm_provider"
KEY_LLM_MODEL = "llm_model"
KEY_LLM_API_KEY = "llm_api_key"
KEY_LLM_ENDPOINT = "llm_endpoint"
# Per-request timeout for LLM calls, in seconds. Sized for the slow end
# of self-hosted setups: a 14B model on CPU (or partial GPU offload)
# can spend 5+ minutes on a long chapter. 0 means "use the env var
# FICARY_LLM_TIMEOUT_S, then the built-in 300s default" — anyone who
# explicitly sets this in the GUI gets the value they typed regardless
# of the env. Bound is enforced in the dialog (60-3600).
KEY_LLM_REQUEST_TIMEOUT_S = "llm_request_timeout_s"
# Comma-separated list of enabled TTS provider names. Empty == fall
# back to "all installed providers" so a fresh install (just edge-tts)
# behaves like 2.1.x. The audiobook generator pulls the union of every
# listed provider's voice catalog into the per-character pool.
KEY_TTS_PROVIDERS = "tts_providers"
KEY_LOG_LEVEL = "log_level"
KEY_LOG_TO_FILE = "log_to_file"
# Prompt before closing the main window while a long-running job
# (download, audiobook build, search, etc.) is still active. The
# prompt's "Don't ask again" checkbox flips this pref off.
KEY_CONFIRM_CANCEL_ON_CLOSE = "confirm_cancel_on_close"
KEY_STORY_PICKER_SORT = "story_picker_sort"
# Library manager — auto-sort downloads into category subdirs and
# re-check existing files (including foreign ones from FanFicFare /
# FicHub) for updates.
KEY_LIBRARY_PATH = "library_path"
KEY_LIBRARY_PATH_TEMPLATE = "library_path_template"
KEY_LIBRARY_INDEX_PATH = "library_index_path"  # blank → program config dir
KEY_LIBRARY_MISC_FOLDER = "library_misc_folder"
# Folder name for original-fiction downloads (Royal Road). Distinct
# from the misc bucket because "no fandom on an original-fiction
# site" means "the work IS original", not "we couldn't classify".
KEY_LIBRARY_ORIGINAL_FOLDER = "library_original_folder"
# Folder name for adult-only / erotica downloads. Same reasoning as
# the original-fiction folder: a visible separate subtree keeps
# adult content out of the general fandom listing.
KEY_LIBRARY_ADULT_FOLDER = "library_adult_folder"
# Optional SEPARATE root path for adult-only / erotica downloads. When
# set, adult-adapter stories are written here — a wholly different
# location (e.g. an unsynced or encrypted folder) — instead of the
# <library>/<adult_folder> subtree. Blank (default) keeps the subfolder
# behaviour. The library index tracks it as its own root, so the browser
# lists and hides it independently of the main library.
KEY_LIBRARY_ADULT_PATH = "library_adult_path"
# Auto-mark WIP stories (status != Complete) as abandoned when
# their file mtime is older than this many days. Non-positive
# disables the auto-mark entirely. Marked stories are skipped by
# --update-library until the user revives them explicitly. Off
# (0) by default so an upgrade doesn't silently declare a pile
# of WIPs dead on the user's first scan.
KEY_LIBRARY_ABANDONED_AFTER_DAYS = "library_abandoned_after_days"
KEY_LIBRARY_AMBIGUOUS_PROMPT = "library_ambiguous_prompt"
KEY_LIBRARY_REORGANIZE_CONFIRM_EACH = "library_reorganize_confirm_each"
# Has the GUI already offered the user the one-time "your scanned
# library is the obvious default download folder, want to use it?"
# prompt. Flipped to True on the first answer (yes or no); we never
# re-ask so users who opt out aren't nagged on every launch.
KEY_LIBRARY_DEFAULT_PROMPTED = "library_default_prompted"
# Per-tab JSON blobs: {"query": "...", "filters": {key: value, ...}}
KEY_SEARCH_STATE_FFN = "search_state_ffn"
KEY_SEARCH_STATE_AO3 = "search_state_ao3"
KEY_SEARCH_STATE_ROYALROAD = "search_state_royalroad"
KEY_SEARCH_STATE_LITEROTICA = "search_state_literotica"
KEY_SEARCH_STATE_WATTPAD = "search_state_wattpad"
# Watchlist notification channels — see ficary.notifications for semantics.
# Pushover creds are a per-user + per-application pair; Discord is a single
# webhook URL; email uses the same SMTP config as --send-to-kindle and only
# needs the recipient address stored here.
KEY_PUSHOVER_TOKEN = "pushover_token"
KEY_PUSHOVER_USER = "pushover_user"
KEY_DISCORD_WEBHOOK = "discord_webhook"
KEY_NOTIFY_EMAIL = "notify_email"
# Watchlist background polling — GUI only; the CLI uses `--watch-run` on
# demand. `KEY_WATCH_POLL_INTERVAL_S` is clamped at load time to the
# floor defined in watchlist.MIN_POLL_INTERVAL_S so a corrupt config
# can't make the app hammer sites.
KEY_WATCH_AUTOPOLL = "watch_autopoll"
KEY_WATCH_POLL_INTERVAL_S = "watch_poll_interval_s"

# Audiobookshelf upload target for finished M4Bs (parallel to
# --send-to-kindle). The token is a plain API key from the ABS user
# page, stored as-is — same posture as the Pushover/SMTP/cookie
# secrets above.
KEY_ABS_URL = "abs_url"
KEY_ABS_TOKEN = "abs_token"
KEY_ABS_LIBRARY_ID = "abs_library_id"
KEY_ABS_FOLDER_ID = "abs_folder_id"
KEY_ABS_AUTO_SEND = "abs_auto_send"

# In-app reader. Font size + theme apply to the chapter text control;
# tts_mode is "screenreader" (let the user's screen reader read the text) or
# "appvoice" (the app speaks via edge/piper — Phase 2). autoadvance moves to
# the next chapter when app-voice reaches the end of one.
KEY_READER_FONT_PT = "reader_font_pt"
KEY_READER_THEME = "reader_theme"
KEY_READER_TTS_MODE = "reader_tts_mode"
KEY_READER_AUTOADVANCE = "reader_autoadvance"

# Default GUI polling interval for the watchlist background thread, in
# seconds. One hour balances freshness against site politeness — FFN's
# 6s/request floor means even a 50-watch list fits comfortably inside
# an hour, and every other supported site is faster.
DEFAULT_WATCH_POLL_INTERVAL_S = 60 * 60

DEFAULTS = {
    KEY_NAME_TEMPLATE: "{title} - {author}",
    KEY_FORMAT: "epub",
    KEY_CHECK_UPDATES: True,
    KEY_HR_AS_STARS: False,
    KEY_STRIP_NOTES: False,
    KEY_HTML_STYLE: "modern",  # exporters.HTML_STYLE_MODERN
    KEY_LLM_STRIP_NOTES: False,
    KEY_FICHUB: False,
    KEY_MERGE_SERIES: False,
    KEY_WEBNOVEL_COOKIE: "",
    KEY_AO3_COOKIE: "",
    KEY_SCRIBBLEHUB_COOKIE: "",
    KEY_SUBSCRIBESTAR_COOKIE: "",
    KEY_SPEECH_RATE: "0",
    KEY_ATTRIBUTION_BACKEND: "builtin",
    KEY_ATTRIBUTION_MODEL_SIZE: "",
    KEY_LLM_PROVIDER: "ollama",
    KEY_LLM_MODEL: "llama3.1:8b",
    KEY_LLM_API_KEY: "",
    KEY_LLM_ENDPOINT: "",
    KEY_LLM_REQUEST_TIMEOUT_S: 0,
    KEY_TTS_PROVIDERS: "",
    KEY_LOG_LEVEL: "INFO",
    KEY_LOG_TO_FILE: False,
    KEY_CONFIRM_CANCEL_ON_CLOSE: True,
    KEY_LIBRARY_PATH_TEMPLATE: "{fandom}/{title} - {author}.{ext}",
    KEY_LIBRARY_MISC_FOLDER: "Misc",
    KEY_LIBRARY_AMBIGUOUS_PROMPT: True,
    KEY_LIBRARY_REORGANIZE_CONFIRM_EACH: True,
    KEY_WATCH_AUTOPOLL: False,
    KEY_WATCH_POLL_INTERVAL_S: DEFAULT_WATCH_POLL_INTERVAL_S,
    KEY_READER_FONT_PT: 14,
    KEY_READER_THEME: "light",
    KEY_READER_TTS_MODE: "screenreader",
    KEY_READER_AUTOADVANCE: True,
    KEY_ABS_URL: "",
    KEY_ABS_TOKEN: "",
    KEY_ABS_LIBRARY_ID: "",
    KEY_ABS_FOLDER_ID: "",
    KEY_ABS_AUTO_SEND: False,
}


def _migrate_legacy_wx_config(cfg) -> None:
    """First run under the new name: copy prefs from the pre-rename
    ``wx.Config("ffn-dl")`` store so pip/dev users keep their settings.
    Only fires into an empty new store; best-effort."""
    try:
        import wx
        if cfg.GetNumberOfEntries() > 0:
            return
        old = wx.Config("ffn-dl")
        more, key, index = old.GetFirstEntry()
        copied = 0
        while more:
            cfg.Write(key, old.Read(key, ""))
            copied += 1
            more, key, index = old.GetNextEntry(index)
        if copied:
            cfg.Flush()
    except Exception:
        pass


class Prefs:
    """Thin wrapper over wx.Config with string and bool accessors."""

    def __init__(self):
        from . import portable

        # Portable frozen build: keep settings.ini next to the exe
        # (or in the writable-fallback dir). Pip-installed / dev mode
        # uses the platform default so users keep their existing prefs.
        #
        # CLI-only installs may not have wxPython — the tool still works
        # with a read-only fallback that returns DEFAULTS and quietly
        # swallows set()/set_bool() calls. The GUI install path always
        # has wx, so users never hit this branch in practice.
        self._cfg = None
        try:
            import wx
        except ImportError:
            return

        if portable.is_frozen():
            self._cfg = wx.FileConfig(
                appName="ficary",
                localFilename=str(portable.settings_file()),
                style=wx.CONFIG_USE_LOCAL_FILE,
            )
        else:
            self._cfg = wx.Config("ficary")
            _migrate_legacy_wx_config(self._cfg)

    def get(self, key: str, default=None):
        if self._cfg is None:
            return default if default is not None else DEFAULTS.get(key)
        val = self._cfg.Read(key, "")
        return val if val else (default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        if self._cfg is None:
            return
        self._cfg.Write(key, "" if value is None else str(value))
        self._cfg.Flush()

    def get_bool(self, key: str, default: bool = None) -> bool:
        if default is None:
            default = DEFAULTS.get(key, False)
        if self._cfg is None:
            return default
        return self._cfg.ReadBool(key, default)

    def set_bool(self, key: str, value: bool) -> None:
        if self._cfg is None:
            return
        self._cfg.WriteBool(key, bool(value))
        self._cfg.Flush()

    def flush(self) -> None:
        """Force any in-memory wx.Config buffer to disk/registry now.

        Every `set`/`set_bool` already flushes, but we call this
        explicitly before spawning a child process in the auto-update
        restart path so the child can't race ahead and read stale
        values that we just wrote.
        """
        if self._cfg is None:
            return
        try:
            self._cfg.Flush()
        except Exception:
            pass
