"""A status/log pane that stays cheap while a screen reader is attached.

Long jobs (a library update, a pip install) report progress one line
at a time from worker threads. The obvious implementation — one
``wx.CallAfter`` per line, each doing ``AppendText`` — is what made
the desktop lag under NVDA: a FicHub fast-path download reports every
one of its several hundred chapters in the same instant, a pip install
prints thousands of lines, and every one of those appends is a
separate round trip into the text control that the screen reader's
in-process hooks also have to observe. While the UI thread is busy
working through that queue, every focus move the user makes waits
behind it.

:class:`StatusLogCtrl` takes the shape the main window's status log
already uses and makes it reusable: lines are queued from any thread
with :meth:`post` (no wx calls, so no marshalling), a timer writes the
whole backlog in one ``AppendText`` per tick, and the control trims
itself so an hour-long run cannot grow a multi-megabyte edit control
that gets slower to append to with every line.
"""

from __future__ import annotations

import threading
from collections import deque

import wx

FLUSH_INTERVAL_MS = 100
"""How often queued lines are written into the control. Ten writes a
second reads as live to a person; several hundred a second only reads
as lag."""

DEFAULT_MAX_LINES = 5000
"""Line count at which the oldest lines are dropped. About one heavy
download session; the on-disk log keeps the full transcript."""

DEFAULT_TRIM_TO_LINES = 4000
"""How many lines survive a trim. Trimming to well below the ceiling
means one trim per thousand new lines rather than one per line."""


class StatusLogCtrl(wx.TextCtrl):
    """Read-only, non-wrapping, multi-line log whose appends are batched.

    ``post``/``post_line`` may be called from any thread. Everything
    else is the ordinary ``wx.TextCtrl`` surface, so a host that reads
    ``GetValue()`` or calls ``AppendText`` directly on the main thread
    keeps working; :meth:`flush` writes any queued text immediately for
    the callers (and tests) that need the control current now.
    """

    def __init__(
        self,
        parent,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        trim_to_lines: int = DEFAULT_TRIM_TO_LINES,
        style: int = 0,
        **kwargs,
    ):
        if trim_to_lines >= max_lines:
            raise ValueError("trim_to_lines must be below max_lines")
        super().__init__(
            parent,
            style=style | wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
            **kwargs,
        )
        self._max_lines = max_lines
        self._trim_to_lines = trim_to_lines
        self._pending: deque[str] = deque()
        self._lock = threading.Lock()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_flush_timer, self._timer)
        self._timer.Start(FLUSH_INTERVAL_MS)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    # ── Producer side (any thread) ─────────────────────────────

    def post(self, text: str) -> None:
        """Queue ``text`` verbatim for the next flush.

        Touches no wx state, so worker threads call it directly instead
        of marshalling one ``wx.CallAfter`` per line onto the UI thread.
        """
        with self._lock:
            self._pending.append(text)

    def post_line(self, line: str) -> None:
        """Queue ``line`` as one line of output (trailing whitespace
        dropped, newline added)."""
        self.post(line.rstrip() + "\n")

    def pending_count(self) -> int:
        """Number of queued, not yet written, posts."""
        with self._lock:
            return len(self._pending)

    # ── Consumer side (UI thread) ──────────────────────────────

    def flush(self) -> None:
        """Write every queued post into the control now, then trim."""
        with self._lock:
            if not self._pending:
                return
            chunk = "".join(self._pending)
            self._pending.clear()
        if not chunk:
            return
        self.AppendText(chunk)
        self._trim()

    def _trim(self) -> None:
        line_count = self.GetNumberOfLines()
        if line_count <= self._max_lines:
            return
        cut_line = line_count - self._trim_to_lines
        cut_pos = self.XYToPosition(0, cut_line)
        if cut_pos > 0:
            self.Remove(0, cut_pos)

    def _on_flush_timer(self, event) -> None:
        self.flush()

    def _on_destroy(self, event) -> None:
        # EVT_WINDOW_DESTROY also arrives for children; only our own
        # teardown should stop the timer.
        if event.GetEventObject() is self:
            self._timer.Stop()
        event.Skip()
