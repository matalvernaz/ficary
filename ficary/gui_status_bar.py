"""A status bar that says what Ficary is doing now, for a screen reader's "read the status bar" key.

NVDA reads a window's status bar with NVDA+End, or NVDA+Shift+End with its laptop keyboard layout, where NVDA+End moves the review cursor to the end of the line instead (NVDA 2026.3 user guide). JAWS reads it with Insert+Page Down. Neither moves focus. The bar shows ficary.activity: what each running job is doing, or how the last one went, or "Ready." before anything has run.
"""

from __future__ import annotations

import wx

from . import activity
from .gui_help import is_own_destroy

IDLE_TEXT = "Ready."
"""What the bar says before any job has run, and after a job that ended without a result."""

HINT = (
    "The status bar at the bottom of this window says which story and "
    "chapter it's on: NVDA+End reads it (NVDA+Shift+End with NVDA's "
    "laptop keyboard layout), or Insert+Page Down in JAWS."
)
"""Said once per window when its first download or update starts. A line per chapter in the status log used to show a download was alive; that is in the status bar now."""

REFRESH_INTERVAL_MS = 250
"""How often the bar looks for a new sentence. Four times a second keeps up with chapters arriving, and the bar is only redrawn when the sentence has changed."""

# wx's default status bar style shortens text that doesn't fit with "...", and on Windows it gives the shortened text to the control itself, so a screen reader would hear a story's name cut off part-way. Without those flags the whole sentence goes to the control and only the drawing is clipped.
STYLE = wx.STB_SIZEGRIP | wx.FULL_REPAINT_ON_RESIZE


class ActivityStatusBar(wx.StatusBar):
    """A one-field status bar that keeps itself up to date from ficary.activity."""

    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=STYLE)
        self._shown: str | None = None
        self.refresh()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(REFRESH_INTERVAL_MS)
        # A timer still running when its window is deleted fires into freed memory, so it stops when the bar goes, whoever destroys it.
        self._own_window_id = self.GetId()
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def refresh(self) -> None:
        """Show the current sentence now, if it has changed since the last look."""
        text = activity.current() or IDLE_TEXT
        if text == self._shown:
            return
        self.SetStatusText(text)
        self._shown = text

    def _on_timer(self, event) -> None:
        self.refresh()

    def _on_destroy(self, event) -> None:
        if is_own_destroy(self._own_window_id, event):
            self._timer.Stop()
        event.Skip()


def add_activity_status_bar(frame: wx.Frame) -> ActivityStatusBar:
    """Give ``frame`` the status bar and return it."""
    bar = ActivityStatusBar(frame)
    frame.SetStatusBar(bar)
    return bar
