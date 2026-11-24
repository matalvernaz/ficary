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


# What must show whole: text and the controls that carry it.
_SHOWN_WHOLE = (wx.StaticText, wx.Button, wx.CheckBox, wx.RadioButton, wx.Choice, wx.ComboBox, wx.SpinCtrl)
_SHORTENED = wx.ST_ELLIPSIZE_START | wx.ST_ELLIPSIZE_MIDDLE | wx.ST_ELLIPSIZE_END

# What can be smaller than it would like, since it scrolls, but must still
# show enough to be used: with larger text, Add saved-search watch squeezed
# its Channels list to nothing and the story picker its Find box to a sliver.
_SCROLLS = (wx.ListBox, wx.ListCtrl, wx.TreeCtrl, wx.TextCtrl, wx.ScrolledWindow)
_LINES_AT_LEAST = 3
_CHARACTERS_AT_LEAST = 8

# Room added beyond the shortfall, so a control isn't drawn hard against the edge.
_SPARE = 8

# A shortfall this small is rounding, not a control cut off (the window
# checks allow the same): at fractional scaling it grew windows a few
# pixels a round.
_TOLERANCE = 2


def _room_asked(control) -> wx.Size:
    """The room a control needs: a label or button the size of its text, even where it was given a fixed size (a fixed 120 pixels cut "AI settings..." off inside its button once the text was larger); a drop-down the width it asks for, since one given a fixed width shows a long item cut short and its list shows it whole."""
    if isinstance(control, (wx.Choice, wx.ComboBox)):
        return control.GetEffectiveMinSize()
    return control.GetBestSize()


def _least_room(control) -> wx.Size:
    """The least room a list, text box or scrolled area can have and still be used: eight characters wide, and three lines tall (a one-line box, one). Never more than the control asks for itself, so one made small on purpose, which stays that size however big the window grows, isn't counted."""
    width = _CHARACTERS_AT_LEAST * control.GetCharWidth()
    one_line = isinstance(control, wx.TextCtrl) and not control.IsMultiLine()
    height = (1 if one_line else _LINES_AT_LEAST) * control.GetCharHeight()
    own = control.GetEffectiveMinSize()
    return wx.Size(min(width, own.width), min(height, own.height))


def _place_in(window, control) -> wx.Point:
    """Where ``control`` sits in the client area of the top-level ``window``, laid out or not."""
    x = y = 0
    current = control
    while current is not None and current is not window:
        position = current.GetPosition()
        x += position.x
        y += position.y
        parent = current.GetParent()
        if parent is not None and parent is not window:
            # A child's position is within its parent's client area, which
            # starts inside the parent's border.
            offset = parent.GetClientAreaOrigin()
            x += offset.x
            y += offset.y
        current = parent
    return wx.Point(x, y)


def _shortfall(window) -> tuple[int, int]:
    """How much wider and taller ``window`` needs to be for every label, button and drop-down in it to show whole, and every list, text box and scrolled area to show enough to be used: none squeezed below that, none past the right-hand or bottom edge. Inside an area that scrolls up and down, only the height is free: something too wide for it is cut off all the same."""
    client = window.GetClientSize()
    need_width = need_height = 0

    def look(parent, scrolled):
        nonlocal need_width, need_height
        for child in parent.GetChildren():
            if isinstance(child, wx.TopLevelWindow) or not child.IsShown():
                continue
            if isinstance(child, _SCROLLS):
                least = _least_room(child)
                size = child.GetSize()
                if size.width < least.width:
                    need_width = max(need_width, least.width - size.width)
                if scrolled is None:
                    if size.height < least.height:
                        need_height = max(need_height, least.height - size.height)
                    place = _place_in(window, child)
                    need_width = max(need_width, place.x + least.width - client.width)
                    need_height = max(need_height, place.y + least.height - client.height)
                elif scrolled.GetClientSize().width >= NARROWEST_WRAP:
                    place = _place_in(scrolled, child)
                    need_width = max(need_width, place.x + least.width - scrolled.GetClientSize().width)
            if isinstance(child, wx.ScrolledWindow):
                look(child, child)
                continue
            if isinstance(child, _SHOWN_WHOLE):
                shortened = isinstance(child, wx.StaticText) and child.GetWindowStyleFlag() & _SHORTENED
                asked = _room_asked(child)
                size = child.GetSize()
                if not shortened and size.width < asked.width:
                    need_width = max(need_width, asked.width - size.width)
                shown_width = max(size.width, 0 if shortened else asked.width)
                if scrolled is None:
                    place = _place_in(window, child)
                    need_width = max(need_width, place.x + shown_width - client.width)
                    need_height = max(need_height, place.y + size.height - client.height)
                elif scrolled.GetClientSize().width >= NARROWEST_WRAP:
                    # (Narrower, the area has not been given its size yet.)
                    place = _place_in(scrolled, child)
                    need_width = max(need_width, place.x + shown_width - scrolled.GetClientSize().width)
            look(child, scrolled)

    look(window, None)
    return (
        need_width if need_width > _TOLERANCE else 0,
        need_height if need_height > _TOLERANCE else 0,
    )


def _notebooks_in(window) -> list:
    found = []
    for child in window.GetChildren():
        if isinstance(child, wx.Notebook):
            found.append(child)
        found.extend(_notebooks_in(child))
    return found


def _page_borders(window) -> dict:
    """For each notebook in ``window``, how much smaller than the notebook its page area is (the tabs and the border), measured on the page showing, once it has its size."""
    borders = {}
    for notebook in _notebooks_in(window):
        page = notebook.GetCurrentPage()
        if page is None:
            continue
        notebook_size = notebook.GetSize()
        page_size = page.GetSize()
        borders[notebook] = (notebook_size.width - page_size.width, notebook_size.height - page_size.height)
    return borders


def _shortfall_on_every_page(window, borders) -> tuple[int, int]:
    """``_shortfall`` on each page of every notebook in ``window``, which shows one at a time; the page that was showing is put back. Each page is given the room the notebook has for it now, less ``borders``: the toolkit resizes only the showing page, and on Linux not straight away."""
    width, height = _shortfall(window)
    focused = wx.Window.FindFocus()
    for notebook, (border_width, border_height) in borders.items():
        if not notebook:
            continue
        showing = notebook.GetSelection()
        notebook_size = notebook.GetSize()
        room = wx.Size(notebook_size.width - border_width, notebook_size.height - border_height)
        for page in range(notebook.GetPageCount()):
            # ChangeSelection, unlike SetSelection, sends no page-changed event.
            notebook.ChangeSelection(page)
            notebook.GetPage(page).SetSize(room)
            notebook.GetPage(page).Layout()
            page_width, page_height = _shortfall(window)
            width = max(width, page_width)
            height = max(height, page_height)
        if showing != wx.NOT_FOUND:
            notebook.ChangeSelection(showing)
            notebook.GetPage(showing).SetSize(room)
            notebook.GetPage(showing).Layout()
    # Hiding a page while measuring can move the focus out of it; a
    # screen reader would then start somewhere else.
    if focused and wx.Window.FindFocus() is not focused:
        focused.SetFocus()
    return width, height


def _pages_unsized(window) -> bool:
    """Whether ``window`` has notebook pages not given their size yet: on Linux a page gets it only once the window is shown."""
    for notebook in _notebooks_in(window):
        page = notebook.GetCurrentPage()
        if page is not None and page.GetSize().width < NARROWEST_WRAP:
            return True
    return False


def grow_to_fit(window) -> bool:
    """Grow ``window`` by whatever in it would be cut off and, if it grew, fit it on the screen; returns whether it grew. Called again after a window's contents change (a longer status, a control shown): it was measured as it opened. A window that needn't grow is left where it is, even partly off the screen, where someone may have put it."""
    if not window:
        return False
    window.Layout()
    borders = _page_borders(window)
    grew = False
    # Laying out again after growing can wrap a label differently, so a
    # few rounds; each only ever grows the window.
    for _ in range(4):
        window.Layout()
        for child in window.GetChildren():
            if isinstance(child, wx.Panel) and not isinstance(child, wx.TopLevelWindow):
                child.Layout()
        extra_width, extra_height = _shortfall_on_every_page(window, borders)
        if not extra_width and not extra_height:
            break
        size = window.GetSize()
        window.SetSize(
            size.width + (extra_width + _SPARE if extra_width else 0),
            size.height + (extra_height + _SPARE if extra_height else 0),
        )
        grew = True
    if grew:
        fit_on_screen(window)
    return grew


def fit_to_contents(window, minimum) -> None:
    """Size the top-level ``window`` to ``minimum`` (width and height in pixels at 100% scaling, so larger on a screen scaled up), then larger wherever a label, button or drop-down in it would be cut off, and fit it on the screen.

    Sized in fixed pixels, Ficary's windows cut off buttons and lines of text, and squeezed lists to nothing, once the system's text was larger (Windows' larger text setting, or large text on Linux): eight of them at 150%. Matt chose to have that fixed (2026-11-24). Windows display scaling doesn't do this, as Ficary isn't DPI-aware: Windows draws its windows as at 100% and stretches them. A window grows only by what doesn't fit: lists, text boxes and scrolled areas can be smaller than they would like, so sizing to what everything would like made windows that already fitted larger.

    A window with notebook pages is measured when it is first shown, since on Linux its pages have no size before then.
    """
    smallest = window.FromDIP(wx.Size(*minimum))
    size = window.GetSize()
    width = max(size.width, smallest.width)
    height = max(size.height, smallest.height)
    if (width, height) != (size.width, size.height):
        window.SetSize(width, height)
    if not _pages_unsized(window):
        if not grow_to_fit(window):
            fit_on_screen(window)
        return

    def on_first_show(event):
        event.Skip()
        if event.IsShown():
            window.Unbind(wx.EVT_SHOW, handler=on_first_show)
            wx.CallAfter(grow_to_fit, window)

    window.Bind(wx.EVT_SHOW, on_first_show)
    fit_on_screen(window)
