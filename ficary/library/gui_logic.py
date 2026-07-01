"""Pure helpers consumed by the GUI dialogs.

Kept separate from ``library/gui.py`` so tests can cover the display
rules without pulling in wxPython, which is an optional dependency.
"""

from __future__ import annotations

from pathlib import Path

from .reorganizer import MoveOp


def relative_to_root(p: Path, root: Path) -> str:
    """Display-friendly form: relative to the library root when under
    it, absolute otherwise."""
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def format_move_label(op: MoveOp, root: Path, checked: bool = False) -> str:
    """The string shown in a ReorganizePreviewDialog row.

    No ``[x]/[ ]`` prefix: NVDA reads CheckListBox checkbox state
    natively on current wxPython, so the prefix double-announced check
    state (matching the pattern dropped from ``gui_dialogs.py``'s
    StoryPickerDialog). ``checked`` is retained as a no-op kwarg so
    callers that still pass it don't break.
    """
    del checked  # accepted for back-compat with callers that still pass it
    source = relative_to_root(op.source, root)
    target = relative_to_root(op.target, root)
    arrow = "renamed to" if op.is_rename else "→"
    return f"{source}  {arrow}  {target}"
