"""Small helpers for attaching explanatory help to wx controls.

A control's *accessible name* (``SetName``) is what a screen reader
announces as the control's identity; its *help text* (``SetHelpText``)
is the longer description NVDA/JAWS read on request, and its *tooltip*
(``SetToolTip``) is the hover bubble sighted users get. The codebase
already names its controls; these helpers add the description layer so
both audiences get a one-line explanation of what a field does.
"""

from __future__ import annotations

from typing import Any


def set_help(ctrl: Any, text: str) -> None:
    """Attach ``text`` to ``ctrl`` as both a hover tooltip and the
    accessible help-text description.

    Both setters are wrapped defensively: a handful of composite
    controls (and the read-only fallbacks used in headless test runs)
    don't implement one or the other, and a missing description must
    never take down UI construction.
    """
    try:
        ctrl.SetToolTip(text)
    except Exception:
        pass
    try:
        ctrl.SetHelpText(text)
    except Exception:
        pass


def name_and_help(ctrl: Any, name: str, text: str) -> None:
    """Set the accessible name *and* the help/tooltip in one call, for
    controls that don't yet have a name. ``name`` is the short identity
    a screen reader speaks on focus; ``text`` is the longer description."""
    try:
        ctrl.SetName(name)
    except Exception:
        pass
    set_help(ctrl, text)


def is_own_destroy(window_id: int, event: Any) -> bool:
    """True when an EVT_WINDOW_DESTROY ``event`` is about the window whose
    id is ``window_id``, false when it is about one of its children.

    ``event.GetEventObject() is self`` looks like the natural test and
    never holds for a top-level window: while it is being torn down wx
    hands back a different Python wrapper (a plain TopLevelWindow), so
    the cleanup it guarded never ran. Compare the id saved when the
    window was made instead; the window itself may no longer answer
    GetId() by then.
    """
    try:
        return event.GetId() == window_id
    except RuntimeError:
        return False
