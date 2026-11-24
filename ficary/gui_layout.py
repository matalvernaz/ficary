"""Layout helpers that keep every window readable by someone looking at it: labels that wrap to the window they sit in, and windows that fit on the screen.

A screen reader reads a cut-off label or a control past the window's edge in full either way, so these faults only show to sighted helpers, and only in a screenshot. The visual walk (tools/visual_walk.py) found instructions cut off mid-sentence in five dialogs on Windows, and a main window taller than a 768-pixel laptop screen.
"""

from __future__ import annotations

import wx

# Room left at a label's right-hand edge for the sizer border around it.
DEFAULT_MARGIN = 16

# Narrower than this, a window is still being laid out: wrapping to it would put one word on each line.
NARROWEST_WRAP = 120


class WrappingText(wx.StaticText):
    """A label that wraps to the width of the window it sits in, and wraps again when that width changes or its text is set.

    wx.StaticText.Wrap wraps once, at a fixed width, so a label wrapped for one size of window is cut off in a narrower one, and a label set later (a status line) isn't wrapped at all unless every caller remembers to. The text as given is kept, so each wrap starts from it rather than from the last wrap's line breaks.
    """

    def __init__(self, parent, label="", margin=DEFAULT_MARGIN, **kwargs):
        super().__init__(parent, label=label, **kwargs)
        self._unwrapped = label
        self._margin = margin
        self._wrapped_width = None
        self._wrapping = False
        parent.Bind(wx.EVT_SIZE, self._on_parent_size)
        self._wrap()

    def SetLabel(self, label):
        # wx's own Wrap sets the label too; that call must not start another wrap.
        if self._wrapping:
            super().SetLabel(label)
            return
        self._unwrapped = label
        self._wrapped_width = None
        self._wrap()

    def unwrapped_label(self) -> str:
        """The text as it was given, without the line breaks wrapping added."""
        return self._unwrapped

    def rewrap(self) -> None:
        """Wrap again at the current width, after something that changes how wide the text is drawn, such as a bold font."""
        self._wrapped_width = None
        self._wrap()

    def _width_to_wrap_to(self) -> int:
        """The width the label has: its window's, or before that window is laid out, its top-level window's."""
        width = self.GetParent().GetClientSize().width - self._margin
        if width >= NARROWEST_WRAP:
            return width
        return self.GetTopLevelParent().GetClientSize().width - 2 * self._margin

    def _wrap(self) -> None:
        width = self._width_to_wrap_to()
        if width < NARROWEST_WRAP or width == self._wrapped_width:
            return
        self._wrapped_width = width
        self._wrapping = True
        try:
            super().SetLabel(self._unwrapped)
            self.Wrap(width)
        finally:
            self._wrapping = False
        # A scrolled area works out how far it scrolls from its contents' sizes, which the new wrap has changed.
        parent = self.GetParent()
        if isinstance(parent, wx.ScrolledWindow):
            wx.CallAfter(_fit_inside, parent)

    def _on_parent_size(self, event) -> None:
        event.Skip()
        if self:
            self._wrap()


def _fit_inside(window) -> None:
    """Recompute a scrolled area's scrolling, unless it has been closed in the meantime."""
    if window:
        window.FitInside()


def fit_on_screen(window, area=None) -> None:
    """Shrink ``window`` to fit the screen it opens on, less the taskbar, and move it wholly onto that screen. ``area`` stands in for the screen's usable area (for tests).

    Windows doesn't shrink a window that is opened bigger than the screen: Ficary's main window, 820 pixels tall, ran under the taskbar of a 768-pixel laptop screen, status bar and all.
    """
    if area is None:
        index = wx.Display.GetFromWindow(window)
        display = wx.Display(index if index != wx.NOT_FOUND else 0)
        area = display.GetClientArea()
    rect = window.GetRect()
    width = min(rect.width, area.width)
    height = min(rect.height, area.height)
    # Kept where it was if it fits there; otherwise moved just far enough to be wholly on the screen.
    left = min(max(rect.x, area.x), area.x + area.width - width)
    top = min(max(rect.y, area.y), area.y + area.height - height)
    if (left, top, width, height) != (rect.x, rect.y, rect.width, rect.height):
        window.SetSize(left, top, width, height)
