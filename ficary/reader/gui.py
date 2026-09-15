"""The in-app reader window (Phase 1: screen-reader-native).

A story opens in a read-only, fully accessible text control so the user's own
NVDA / VoiceOver / Orca reads it (say-all) — the mode the AudioGames thread
most asked for. Chapter navigation, reading-position restore, bookmarks, and
low-vision themes are all keyboard-first. The app-voice transport (live TTS)
is added in Phase 2; this frame already emits the reader-open/close lifecycle
so the audio layer can hook in without touching this file.
"""
from __future__ import annotations

import logging
import threading
import time

import wx

from ..audio.engine import CHANNEL_AMBIENT, get_engine
from ..audio.events import Event, ReaderEvent
from ..soundscape import library as _sc_library
from ..soundscape.session import SoundscapeSession
from ..prefs import (
    KEY_READER_AUTOADVANCE,
    KEY_READER_FONT_PT,
    KEY_READER_LANGUAGE,
    KEY_READER_THEME,
    KEY_READER_TTS_MODE,
    KEY_READER_VOICE,
    KEY_SPEECH_RATE,
    KEY_TTS_PROVIDERS,
)
from . import theme as _theme
from .live_tts import LiveTTSController
from .sleep_timer import MAX_MINUTES, MIN_MINUTES, SleepTimer
from .state import ReaderStateDB
from .source import StorySource

logger = logging.getLogger(__name__)

_EXCERPT_RADIUS = 40  # chars either side of the caret saved with a bookmark
_POSITION_SAVE_INTERVAL_S = 5  # debounce for listening-position autosave


class ReaderFrame(wx.Frame):
    """Reads one :class:`StorySource`. One instance per open story."""

    def __init__(self, main_frame, prefs, source: StorySource):
        super().__init__(main_frame, title=f"Reader — {source.title}",
                         size=(900, 640))
        self._main = main_frame
        self.prefs = prefs
        self.source = source
        self._alive = True
        self._state = ReaderStateDB()
        self._current_chapter = 1
        self._current_rc = None
        self._text_prefix_len = 0
        self._paused = False
        self._engine = get_engine()
        self._live = None
        self._resolving_voice = False
        self._last_voice = ""
        # Every playback request carries this token. Stop, a chapter
        # change, a mode switch and close all bump it, so a voice
        # lookup that was still on the network when the user cancelled
        # can't come back and start speaking.
        self._playback_gen = 0
        # Listening position within the current chapter's text (not the
        # control, which also holds the heading prefix). Kept in step
        # with the spoken chunk so Play resumes where the voice stopped.
        self._listen_offset = 0
        self._last_pos_save = 0.0
        self._soundscape_session = None
        self._sleep_timer = SleepTimer(on_expire=lambda: wx.CallAfter(self._on_sleep_expire))

        self._build_menu()
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._populate_chapter_list()
        self._restore_position()
        self._soundscape_session = self._make_soundscape_session()
        self._engine.emit(Event(ReaderEvent.READER_OPENED, story_key=self.source.story_key))
        self.Centre()

    # ── UI construction ───────────────────────────────────────────
    def _build_menu(self) -> None:
        bar = wx.MenuBar()
        menu = wx.Menu()
        self._mi_prev = menu.Append(wx.ID_ANY, "&Previous chapter\tCtrl+Left")
        self._mi_next = menu.Append(wx.ID_ANY, "&Next chapter\tCtrl+Right")
        menu.AppendSeparator()
        self._mi_add_bm = menu.Append(wx.ID_ANY, "&Add bookmark\tCtrl+B")
        self._mi_list_bm = menu.Append(wx.ID_ANY, "&Bookmarks...\tCtrl+Shift+B")
        menu.AppendSeparator()
        self._mi_bigger = menu.Append(wx.ID_ANY, "&Larger text\tCtrl+=")
        self._mi_smaller = menu.Append(wx.ID_ANY, "&Smaller text\tCtrl+-")
        self._mi_theme = menu.Append(wx.ID_ANY, "Cycle &theme\tCtrl+T")
        menu.AppendSeparator()
        self._mi_play = menu.Append(wx.ID_ANY, "Play/Pause (app &voice)\tCtrl+P")
        self._mi_stop = menu.Append(wx.ID_ANY, "Stop &reading\tCtrl+.")
        self._mi_soundscape = menu.Append(wx.ID_ANY, "S&oundscape for this story...\tCtrl+Shift+A")
        self._mi_sleep = menu.Append(wx.ID_ANY, "Set sleep t&imer...\tCtrl+Shift+T")
        self._mi_sleep_status = menu.Append(wx.ID_ANY, "Sleep timer stat&us\tCtrl+Shift+S")
        menu.AppendSeparator()
        self._mi_close = menu.Append(wx.ID_CLOSE, "&Close\tCtrl+W")
        bar.Append(menu, "&Reader")
        self.SetMenuBar(bar)

        self.Bind(wx.EVT_MENU, lambda e: self._step_chapter(-1), self._mi_prev)
        self.Bind(wx.EVT_MENU, lambda e: self._step_chapter(1), self._mi_next)
        self.Bind(wx.EVT_MENU, self._on_add_bookmark, self._mi_add_bm)
        self.Bind(wx.EVT_MENU, self._on_list_bookmarks, self._mi_list_bm)
        self.Bind(wx.EVT_MENU, lambda e: self._bump_font(1), self._mi_bigger)
        self.Bind(wx.EVT_MENU, lambda e: self._bump_font(-1), self._mi_smaller)
        self.Bind(wx.EVT_MENU, self._on_cycle_theme, self._mi_theme)
        self.Bind(wx.EVT_MENU, self._on_play_pause, self._mi_play)
        self.Bind(wx.EVT_MENU, self._on_stop_tts, self._mi_stop)
        self.Bind(wx.EVT_MENU, self._on_pick_soundscape, self._mi_soundscape)
        self.Bind(wx.EVT_MENU, self._on_set_sleep, self._mi_sleep)
        self.Bind(wx.EVT_MENU, self._on_sleep_status, self._mi_sleep_status)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), self._mi_close)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="&Chapters"), 0, wx.ALL, 4)
        self.chapter_list = wx.ListCtrl(
            panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(240, -1))
        self.chapter_list.InsertColumn(0, "Chapter", width=230)
        self.chapter_list.SetName("Chapters")
        # ACTIVATED (Enter/double-click) only. Binding SELECTED as well made
        # every arrow keypress load a chapter and yank focus to the text
        # control (the list was unbrowsable by keyboard), and _load_chapter's
        # own programmatic Select() re-entered the handler with caret=0,
        # clobbering every restored reading position.
        self.chapter_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_chapter_selected)
        left.Add(self.chapter_list, 1, wx.EXPAND | wx.ALL, 4)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(panel, label="Chapter &text"), 0, wx.ALL, 4)
        self.text = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.text.SetName("Chapter text")
        right.Add(self.text, 1, wx.EXPAND | wx.ALL, 4)

        nav = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_prev = wx.Button(panel, label="&Previous")
        self.btn_next = wx.Button(panel, label="&Next")
        self.btn_prev.Bind(wx.EVT_BUTTON, lambda e: self._step_chapter(-1))
        self.btn_next.Bind(wx.EVT_BUTTON, lambda e: self._step_chapter(1))
        nav.Add(self.btn_prev, 0, wx.ALL, 4)
        nav.Add(self.btn_next, 0, wx.ALL, 4)
        nav.Add(wx.StaticText(panel, label="&Jump to"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.jump = wx.Choice(panel, choices=[])
        self.jump.SetName("Jump to chapter")
        self.jump.Bind(wx.EVT_CHOICE, self._on_jump)
        nav.Add(self.jump, 0, wx.ALL, 4)
        right.Add(nav, 0, wx.ALL, 4)

        transport = wx.BoxSizer(wx.HORIZONTAL)
        transport.Add(wx.StaticText(panel, label="&Reading mode"),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        self.mode = wx.Choice(panel, choices=["Screen reader", "App voice"])
        self.mode.SetName("Reading mode")
        self.mode.SetSelection(1 if self.prefs.get(KEY_READER_TTS_MODE) == "appvoice" else 0)
        self.mode.Bind(wx.EVT_CHOICE, self._on_mode)
        transport.Add(self.mode, 0, wx.ALL, 4)
        self.btn_play = wx.Button(panel, label="&Play / Pause")
        self.btn_stop = wx.Button(panel, label="S&top")
        self.btn_play.Bind(wx.EVT_BUTTON, self._on_play_pause)
        self.btn_stop.Bind(wx.EVT_BUTTON, self._on_stop_tts)
        transport.Add(self.btn_play, 0, wx.ALL, 4)
        transport.Add(self.btn_stop, 0, wx.ALL, 4)
        # Name the service that will receive the story text. "App voice"
        # alone doesn't say whether that is a local model or a cloud
        # endpoint, and that is the difference the provider setting is
        # there to control.
        self.voice_status = wx.StaticText(panel, label="Voice: not started")
        self.voice_status.SetName("Voice status")
        transport.Add(self.voice_status, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 4)
        right.Add(transport, 0, wx.ALL, 4)

        root.Add(left, 0, wx.EXPAND)
        root.Add(right, 1, wx.EXPAND)
        panel.SetSizer(root)

    # ── population + position ─────────────────────────────────────
    def _populate_chapter_list(self) -> None:
        count = self.source.chapter_count()
        labels = [f"Chapter {n}" for n in range(1, count + 1)]
        for i, label in enumerate(labels):
            self.chapter_list.InsertItem(i, label)
        self.jump.Set(labels)

    def _restore_position(self) -> None:
        pos = self._state.load_position(self.source.story_key)
        chapter, offset = pos if pos else (1, 0)
        chapter = max(1, min(chapter, self.source.chapter_count()))
        self._load_chapter(chapter, caret=offset)

    def _save_position(self) -> None:
        if self._current_rc is None:
            # No chapter ever loaded (e.g. the restore-target chapter failed
            # to read) — saving now would overwrite the stored position
            # with (1, 0).
            return
        try:
            offset = self._content_position()
        except RuntimeError:
            return
        self._state.save_position(
            self.source.story_key, self._current_chapter, offset,
            title=self.source.title)

    def _content_position(self) -> int:
        """The single position worth persisting for this chapter.

        While the app voice is reading, the spoken chunk is where the
        user actually is — the caret hasn't moved since they pressed
        Play. Otherwise the caret is authoritative, which is what
        screen-reader mode relies on.
        """
        if self._live is not None and self._listen_offset:
            return self._text_prefix_len + self._listen_offset
        return self.text.GetInsertionPoint()

    # ── chapter loading ───────────────────────────────────────────
    def _load_chapter(self, number: int, caret: int = 0) -> None:
        # A chapter change retires any pending playback request, and the
        # outgoing controller is released whether or not it was still
        # speaking — a finished one still owns its temp audio.
        self._new_playback_gen()
        if getattr(self, "_live", None) is not None:
            self._dispose_live()
            self._paused = False
        try:
            rc = self.source.load_chapter(number)
        except Exception as exc:  # ReaderSourceError or a corrupt chapter
            wx.MessageBox(f"Couldn't open chapter {number}: {exc}",
                          "Reader", wx.OK | wx.ICON_ERROR, self)
            return
        self._current_chapter = number
        self._current_rc = rc
        self._text_prefix_len = len(rc.heading) + 2
        self.text.SetValue(f"{rc.heading}\n\n{rc.text}")
        _theme.apply_to_textctrl(
            self.text,
            self.prefs.get(KEY_READER_THEME),
            self.prefs.get(KEY_READER_FONT_PT),
        )
        caret = max(0, min(caret, self.text.GetLastPosition()))
        self.text.SetInsertionPoint(caret)
        self.text.ShowPosition(caret)
        # One position, two views: the control's caret includes the
        # heading prefix, the listening offset indexes the chapter text.
        self._listen_offset = max(0, caret - self._text_prefix_len)
        if self.chapter_list.GetItemCount() >= number:
            self.chapter_list.Select(number - 1)
            self.chapter_list.EnsureVisible(number - 1)
        self.jump.SetSelection(number - 1)
        self.btn_prev.Enable(number > 1)
        self.btn_next.Enable(number < self.source.chapter_count())
        self._save_position()
        self.text.SetFocus()

    def _step_chapter(self, delta: int) -> None:
        target = self._current_chapter + delta
        if 1 <= target <= self.source.chapter_count():
            self._load_chapter(target)

    def _on_chapter_selected(self, event) -> None:
        self._load_chapter(event.GetIndex() + 1)

    def _on_jump(self, event) -> None:
        self._load_chapter(self.jump.GetSelection() + 1)

    # ── bookmarks ─────────────────────────────────────────────────
    def _on_add_bookmark(self, event) -> None:
        offset = self.text.GetInsertionPoint()
        body = self.text.GetValue()
        lo = max(0, offset - _EXCERPT_RADIUS)
        excerpt = body[lo:offset + _EXCERPT_RADIUS].replace("\n", " ").strip()
        default = f"Chapter {self._current_chapter}"
        with wx.TextEntryDialog(self, "Bookmark name:", "Add bookmark", default) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue().strip() or default
        self._state.add_bookmark(
            self.source.story_key, name, self._current_chapter, offset, excerpt)

    def _on_list_bookmarks(self, event) -> None:
        marks = self._state.list_bookmarks(self.source.story_key)
        if not marks:
            wx.MessageBox("No bookmarks yet.", "Bookmarks", wx.OK | wx.ICON_INFORMATION, self)
            return
        with _BookmarksDialog(self, marks) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                for bid in dlg.deleted:
                    self._state.delete_bookmark(bid)
                return
            for bid in dlg.deleted:
                self._state.delete_bookmark(bid)
            chosen = dlg.chosen
        if chosen is not None:
            self._load_chapter(chosen.chapter_number, caret=chosen.char_offset)

    # ── font + theme ──────────────────────────────────────────────
    def _bump_font(self, delta: int) -> None:
        pt = _theme.clamp_font_pt(self.prefs.get(KEY_READER_FONT_PT)) + delta
        self.prefs.set(KEY_READER_FONT_PT, _theme.clamp_font_pt(pt))
        _theme.apply_to_textctrl(self.text, self.prefs.get(KEY_READER_THEME),
                                 self.prefs.get(KEY_READER_FONT_PT))

    def _on_cycle_theme(self, event) -> None:
        nxt = _theme.next_theme(self.prefs.get(KEY_READER_THEME))
        self.prefs.set(KEY_READER_THEME, nxt)
        _theme.apply_to_textctrl(self.text, nxt, self.prefs.get(KEY_READER_FONT_PT))

    # ── app-voice (live TTS) ──────────────────────────────────────
    def _new_playback_gen(self) -> int:
        """Invalidate every in-flight playback request and return the
        new token. Called by Stop, chapter loads, mode switches and
        close — anything that means "whatever was starting, don't"."""
        self._playback_gen += 1
        return self._playback_gen

    def _playback_current(self, gen: int) -> bool:
        return self._alive and gen == self._playback_gen

    def _dispose_live(self) -> None:
        """Stop and release the current controller, active or not.

        A controller that reached the end of its chapter is no longer
        active, but it still owns a temp directory full of synthesised
        MP3s. Replacing it without stopping it first leaked one of those
        directories per chapter for the life of the process.
        """
        live = getattr(self, "_live", None)
        self._live = None
        if live is None:
            return
        try:
            live.stop()
        except Exception:
            logger.debug("Reader TTS controller stop failed", exc_info=True)

    def _on_mode(self, event) -> None:
        appvoice = self.mode.GetSelection() == 1
        self.prefs.set(KEY_READER_TTS_MODE, "appvoice" if appvoice else "screenreader")
        # Invalidate either way: switching *to* app voice mid-resolution
        # shouldn't inherit a request made before the user changed mind.
        self._new_playback_gen()
        if not appvoice:
            self._on_stop_tts(None)

    def _on_play_pause(self, event) -> None:
        if self.mode.GetSelection() != 1:
            wx.MessageBox(
                "Switch reading mode to 'App voice' to have Ficary read aloud. "
                "In 'Screen reader' mode your own screen reader reads the text.",
                "Reader", wx.OK | wx.ICON_INFORMATION, self)
            return
        if self._live is not None and self._live.is_active():
            if self._paused:
                self._live.resume()
                self._paused = False
            else:
                self._live.pause()
                self._paused = True
            return
        if not self._engine.available:
            wx.MessageBox(
                "Audio playback isn't available — PyOpenAL or an audio "
                "device is missing. Install the playback feature to use "
                "app-voice reading; screen-reader mode works without it.",
                "Reader", wx.OK | wx.ICON_INFORMATION, self)
            return
        if self._resolving_voice:
            return
        # Voice resolution can hit the network (edge-tts fetches its voice
        # catalog on first use) — run it off the GUI thread so a slow or
        # offline first Play doesn't freeze the window.
        self._resolving_voice = True
        gen = self._new_playback_gen()
        threading.Thread(
            target=self._resolve_voice_and_play, args=(gen,), daemon=True,
            name="ficary-reader-voice",
        ).start()

    def _resolve_voice_and_play(self, gen: int) -> None:
        voice = self._default_voice()
        wx.CallAfter(self._begin_playback, voice, gen)

    def _begin_playback(self, voice: str, gen: int | None = None) -> None:
        self._resolving_voice = False
        if gen is not None and not self._playback_current(gen):
            # Stop, a chapter change or a mode switch happened while the
            # voice catalog was being fetched. Honour that, don't speak.
            return
        if not self._alive:
            return
        try:
            if self.mode.GetSelection() != 1:
                # Reading mode moved to the screen reader while the voice
                # was resolving. Starting now would talk over it.
                return
        except RuntimeError:
            return
        if not voice:
            wx.MessageBox(
                "No TTS voice is available. Install the audio feature "
                "(edge-tts or Piper) to use app-voice reading.",
                "Reader", wx.OK | wx.ICON_INFORMATION, self)
            return
        self._paused = False
        self._last_voice = voice
        if gen is None:
            gen = self._new_playback_gen()
        resume_at = self._resume_offset()
        # Release the previous chapter's controller (and its temp audio)
        # before the replacement takes its place.
        self._dispose_live()
        controller = LiveTTSController(
            self._engine, voice=voice,
            rate=str(self.prefs.get(KEY_SPEECH_RATE) or "0"),
            on_highlight=lambda c: wx.CallAfter(self._highlight_chunk, c, gen),
            on_complete=lambda failed: wx.CallAfter(
                self._on_tts_complete, failed, gen),
            story_key=self.source.story_key,
        )
        self._live = controller
        self._update_voice_status()
        text = self._current_rc.text if self._current_rc else ""
        # Resume where the listener (or the caret) left off instead of
        # restarting the chapter every time Play is pressed.
        self._listen_offset = resume_at
        controller.start(
            text, self._current_chapter, start_offset=resume_at,
        )

    def _resume_offset(self) -> int:
        """Where the next Play should start reading from.

        The listening position is the default, but a caret the user has
        moved since the last spoken chunk is an explicit "read from
        here" and wins. Stop syncs the caret to the listening position
        so the two only differ when the user actually navigated.
        """
        try:
            caret = max(0, self.text.GetInsertionPoint() - self._text_prefix_len)
        except RuntimeError:
            return self._listen_offset
        return caret if caret != self._listen_offset else self._listen_offset

    def _on_tts_complete(self, failed_chunks: int, gen: int | None = None) -> None:
        """Natural end-of-chapter: announce any skipped sections, then
        auto-advance when the pref asks for it."""
        if gen is not None and not self._playback_current(gen):
            return
        if not self._alive:
            return
        if failed_chunks:
            wx.MessageBox(
                f"{failed_chunks} section(s) of this chapter couldn't be "
                "converted to speech and were skipped.",
                "Reader", wx.OK | wx.ICON_WARNING, self)
        if not self.prefs.get_bool(KEY_READER_AUTOADVANCE):
            return
        if self._current_chapter >= self.source.chapter_count():
            return
        if self._live is None or self._paused:
            return
        voice = self._last_voice
        self._load_chapter(self._current_chapter + 1)
        # A fresh chapter starts at its beginning, not at the offset the
        # previous chapter's listening position left behind.
        self._listen_offset = 0
        if voice:
            # Reuse the resolved voice — no need to re-hit the catalog
            # between chapters.
            self._begin_playback(voice)

    def _on_stop_tts(self, event) -> None:
        self._new_playback_gen()
        # Remember where the voice got to so Play resumes rather than
        # restarting the chapter.
        self._save_position()
        self._dispose_live()
        self._paused = False
        self._clear_highlight()
        # Leave the caret where the voice stopped: that is where the
        # screen reader should pick up, and it keeps "resume" and "read
        # from the caret" pointing at the same place.
        if self._listen_offset:
            try:
                pos = min(
                    self._text_prefix_len + self._listen_offset,
                    self.text.GetLastPosition(),
                )
                self.text.SetInsertionPoint(pos)
                self.text.ShowPosition(pos)
            except RuntimeError:
                pass

    def _highlight_chunk(self, chunk, gen: int | None = None) -> None:
        if gen is not None and not self._playback_current(gen):
            # A CallAfter queued just before Stop (or from a superseded
            # worker) must not repaint a highlight nothing is reading.
            return
        if not self._alive or self._live is None:
            return
        # The spoken chunk *is* the listening position. Saving it here
        # (debounced) means a crash or a forced quit loses seconds of
        # audio rather than the whole chapter.
        self._listen_offset = chunk.start
        now = time.monotonic()
        if now - self._last_pos_save >= _POSITION_SAVE_INTERVAL_S:
            self._last_pos_save = now
            self._save_position()
        pal = _theme.palette(self.prefs.get(KEY_READER_THEME))
        self._clear_highlight()
        start = self._text_prefix_len + chunk.start
        end = self._text_prefix_len + chunk.end
        self.text.SetStyle(start, end,
                           wx.TextAttr(wx.Colour(pal["hl_fg"]), wx.Colour(pal["hl_bg"])))
        self.text.ShowPosition(start)

    def _clear_highlight(self) -> None:
        _theme.apply_to_textctrl(self.text, self.prefs.get(KEY_READER_THEME),
                                 self.prefs.get(KEY_READER_FONT_PT))

    def _default_voice(self) -> str:
        """Pick the voice the reader should speak with.

        An explicit saved choice wins. Otherwise the catalog is narrowed
        to the providers the user left enabled and then to the reading
        language, so "offline only" really means offline and an English
        story isn't read by whichever locale happens to sort first in
        the provider's catalog.
        """
        try:
            from ..tts_providers import all_voices, voice_by_id

            saved = (self.prefs.get(KEY_READER_VOICE) or "").strip()
            providers = self._enabled_providers()
            if saved:
                info = voice_by_id(saved)
                if info is not None and (
                    not providers or info.provider in providers
                ):
                    return saved
            voices = all_voices(providers=providers)
            if providers:
                # Filter the result as well as the request: the catalog
                # is the thing that decides which service receives the
                # story text, so the restriction is enforced on what
                # comes back, not only on what was asked for.
                voices = [v for v in voices if v.provider in providers]
            if not voices:
                if providers:
                    logger.info(
                        "No reader voice available from the enabled "
                        "provider(s): %s", ", ".join(providers),
                    )
                return ""
            lang = self._preferred_language()
            if lang:
                matching = [v for v in voices if v.language.lower() == lang]
                if matching:
                    voices = matching
            return getattr(voices[0], "id", "") or getattr(voices[0], "name", "")
        except Exception:
            logger.debug("Reader voice selection failed", exc_info=True)
            return ""

    def _preferred_language(self) -> str:
        """Two-letter reading language: the explicit preference, else the
        system locale. Empty when neither is known, which means "don't
        filter" rather than "guess English"."""
        lang = (self.prefs.get(KEY_READER_LANGUAGE) or "").strip().lower()
        if lang:
            return lang.split("-", 1)[0]
        try:
            import locale

            tag = locale.getlocale()[0] or ""
        except Exception:
            tag = ""
        tag = tag.replace("-", "_").split("_", 1)[0].strip().lower()
        return tag if tag.isalpha() and len(tag) == 2 else ""

    def _enabled_providers(self) -> list[str] | None:
        """The TTS providers the user allows, or None for "any installed".

        Same preference the audiobook generator reads, so a reader that
        is told to stay local doesn't quietly reach a cloud voice the
        renderer would have refused.
        """
        raw = (self.prefs.get(KEY_TTS_PROVIDERS) or "").strip()
        if not raw:
            return None
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
        return names or None

    def _update_voice_status(self) -> None:
        status = getattr(self, "voice_status", None)
        if status is None:
            return
        voice = self._last_voice
        if not voice:
            label = "Voice: not started"
        else:
            from ..tts_providers import parse_voice_id

            provider, short = parse_voice_id(voice)
            label = f"Voice: {short} ({provider})"
        try:
            status.SetLabel(label)
        except RuntimeError:
            pass

    def voice_provider_label(self) -> str:
        """Human-readable "which service will receive this text"."""
        from ..tts_providers import parse_voice_id

        voice = self._last_voice or self._default_voice()
        if not voice:
            return "none"
        provider, _ = parse_voice_id(voice)
        return provider

    # ── soundscape ────────────────────────────────────────────────
    def _make_soundscape_session(self):
        slug = self._state.get_soundscape(self.source.story_key)
        sc = _sc_library.load(slug) if slug else None
        return SoundscapeSession(self._engine, sc)

    def _on_pick_soundscape(self, event) -> None:
        slugs = _sc_library.list_slugs()
        choices = ["(none)"] + slugs
        with wx.SingleChoiceDialog(self, "Ambient soundscape for this story:",
                                   "Soundscape", choices) as dlg:
            current = self._state.get_soundscape(self.source.story_key)
            if current in slugs:
                dlg.SetSelection(slugs.index(current) + 1)
            if dlg.ShowModal() != wx.ID_OK:
                return
            sel = dlg.GetSelection()
        slug = None if sel == 0 else slugs[sel - 1]
        self._state.set_soundscape(self.source.story_key, slug)
        sc = _sc_library.load(slug) if slug else None
        if self._soundscape_session is not None:
            self._soundscape_session.set_soundscape(sc)

    # ── sleep timer ───────────────────────────────────────────────
    def _on_set_sleep(self, event) -> None:
        with wx.NumberEntryDialog(
            self, "Stop reading after how many minutes?", "Minutes",
            "Sleep timer", 30, MIN_MINUTES, MAX_MINUTES) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            minutes = dlg.GetValue()
        used = self._sleep_timer.start(minutes)
        wx.MessageBox(f"Sleep timer set for {used} minutes.", "Sleep timer",
                      wx.OK | wx.ICON_INFORMATION, self)

    def _on_sleep_status(self, event) -> None:
        if self._sleep_timer.active:
            mins = (self._sleep_timer.remaining_seconds() + 59) // 60
            msg = f"Sleep timer active: about {mins} minute(s) remaining."
        else:
            msg = "No sleep timer set."
        wx.MessageBox(msg, "Sleep timer", wx.OK | wx.ICON_INFORMATION, self)

    def _on_sleep_expire(self) -> None:
        if not self._alive:
            return
        self._on_stop_tts(None)
        if self._soundscape_session is not None:
            self._soundscape_session.fade_out()
        wx.MessageBox("Sleep timer expired. Reading stopped.", "Sleep timer",
                      wx.OK | wx.ICON_INFORMATION, self)

    # ── lifecycle ─────────────────────────────────────────────────
    def _on_close(self, event) -> None:
        self._save_position()
        self._alive = False
        self._new_playback_gen()
        self._sleep_timer.cancel()
        self._dispose_live()
        self._engine.emit(Event(ReaderEvent.READER_CLOSED, story_key=self.source.story_key))
        if self._soundscape_session is not None:
            try:
                self._soundscape_session.close()
            except Exception:
                pass
        self._save_position()
        try:
            self._state.close()
        except Exception:
            pass
        if self._main is not None:
            self._main._notify_reader_frame_closed()
        event.Skip()


class _BookmarksDialog(wx.Dialog):
    """List bookmarks: Enter/Jump to go, Delete to remove, Close to dismiss."""

    def __init__(self, parent, marks):
        super().__init__(parent, title="Bookmarks", size=(460, 340))
        self._marks = list(marks)
        self.chosen = None
        self.deleted: list[int] = []

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="&Bookmarks"), 0, wx.ALL, 6)
        self.listbox = wx.ListBox(self, choices=self._labels())
        self.listbox.SetName("Bookmarks")
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._jump())
        if self._marks:
            self.listbox.SetSelection(0)
        sizer.Add(self.listbox, 1, wx.EXPAND | wx.ALL, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)
        btn_jump = wx.Button(self, wx.ID_OK, "&Jump")
        btn_del = wx.Button(self, label="&Delete")
        btn_close = wx.Button(self, wx.ID_CANCEL, "&Close")
        btn_del.Bind(wx.EVT_BUTTON, self._on_delete)
        row.Add(btn_jump, 0, wx.ALL, 4)
        row.Add(btn_del, 0, wx.ALL, 4)
        row.Add(btn_close, 0, wx.ALL, 4)
        sizer.Add(row, 0, wx.ALIGN_RIGHT)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_jump_btn, id=wx.ID_OK)

    def _labels(self) -> list[str]:
        return [f"{m.name} — ch. {m.chapter_number}"
                + (f": {m.excerpt}" if m.excerpt else "") for m in self._marks]

    @property
    def _selected(self):
        i = self.listbox.GetSelection()
        return self._marks[i] if 0 <= i < len(self._marks) else None

    def _jump(self) -> None:
        self.chosen = self._selected
        self.EndModal(wx.ID_OK)

    def _on_jump_btn(self, event) -> None:
        self.chosen = self._selected
        event.Skip()

    def _on_delete(self, event) -> None:
        i = self.listbox.GetSelection()
        if not (0 <= i < len(self._marks)):
            return
        self.deleted.append(self._marks[i].id)
        del self._marks[i]
        self.listbox.Set(self._labels())
        if self._marks:
            self.listbox.SetSelection(min(i, len(self._marks) - 1))
