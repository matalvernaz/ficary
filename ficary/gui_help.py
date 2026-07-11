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
