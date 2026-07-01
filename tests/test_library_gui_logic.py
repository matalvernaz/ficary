"""Tests for wx-free helpers used by the library GUI dialogs."""

from __future__ import annotations

from pathlib import Path

from ficary.library.gui_logic import format_move_label, relative_to_root
from ficary.library.reorganizer import MoveOp


def _op(source: str, target: str, url: str = "https://x/y/1/") -> MoveOp:
    return MoveOp(source=Path(source), target=Path(target), source_url=url)


def test_relative_to_root_under_root():
    root = Path("/lib")
    p = Path("/lib/Harry Potter/Fic.epub")
    assert relative_to_root(p, root) == "Harry Potter/Fic.epub"


def test_relative_to_root_outside_root_returns_absolute():
    root = Path("/lib")
    p = Path("/elsewhere/Fic.epub")
    assert relative_to_root(p, root) == "/elsewhere/Fic.epub"


def test_format_move_label_no_checked_prefix():
    """The deprecated ``[x] / [ ] `` prefix workaround is gone — NVDA
    reads CheckListBox state natively on current wxPython. The
    ``checked`` kwarg is retained as a no-op for back-compat with
    callers that still pass it."""
    root = Path("/lib")
    op = _op("/lib/Fic.epub", "/lib/Harry Potter/Fic.epub")
    checked = format_move_label(op, root, checked=True)
    unchecked = format_move_label(op, root, checked=False)
    assert not checked.startswith("[x] ")
    assert not checked.startswith("[ ] ")
    # Same label regardless of checked state, by design.
    assert checked == unchecked


def test_format_move_label_uses_arrow_for_relocation():
    root = Path("/lib")
    op = _op("/lib/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=True)
    assert "→" in label
    assert "renamed to" not in label


def test_format_move_label_uses_renamed_phrasing_for_rename():
    # Same parent directory — it's a pure rename, not a move
    root = Path("/lib")
    op = _op("/lib/Harry Potter/A.epub", "/lib/Harry Potter/B.epub")
    label = format_move_label(op, root, checked=True)
    assert "renamed to" in label
    assert "→" not in label


def test_format_move_label_uses_paths_relative_to_root():
    root = Path("/lib")
    op = _op("/lib/misplaced/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=True)
    assert "misplaced/Fic.epub" in label
    assert "Harry Potter/Fic.epub" in label
    # Full absolute paths shouldn't leak through when under root
    assert "/lib/misplaced" not in label
    assert "/lib/Harry Potter" not in label


def test_format_move_label_stable_for_identical_inputs():
    root = Path("/lib")
    op = _op("/lib/a.epub", "/lib/b/a.epub")
    assert format_move_label(op, root, True) == format_move_label(op, root, True)
