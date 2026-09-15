"""The single launch path shared by every way Ficary can be started.

``ficary`` on the command line, ``python -m ficary``, and the frozen
desktop executable all land here, so they agree on one rule: arguments
mean the CLI, no arguments means the GUI. The console script used to
point straight at ``ficary.cli:main``, which always parses an operation,
so ``ficary`` with no arguments exited 2 with a usage error instead of
opening the window the README tells people to expect.

This module is import-safe — importing it does not launch anything —
which is what lets the console script and ``__main__`` both target it.
"""
from __future__ import annotations

import os
import sys


def _prepare_frozen_path() -> None:
    """Let a frozen build import the modules sitting beside its exe."""
    if getattr(sys, "frozen", False):
        sys.path.insert(0, os.path.dirname(sys.executable))


def _detach_windows_console_for_gui() -> None:
    """Drop the console window the OS attached to a GUI launch.

    PyInstaller builds ficary.exe with ``--console`` so the same
    binary can serve CLI users — running ``ficary https://...`` from
    cmd or PowerShell needs stdout/stderr to land in that terminal.
    The cost is that double-clicking the exe to open the GUI also
    spawns a black console window that hangs behind the wx frame
    for the whole session. ``FreeConsole`` detaches our process
    from that allocated console so it closes immediately, leaving
    only the GUI window visible.

    No-op outside frozen Windows builds. Safe when invoked from
    cmd: ``FreeConsole`` only releases *our* handle, the parent
    shell keeps its own console intact (and we don't need stdio
    in GUI mode anyway).
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except (OSError, AttributeError):
        # ctypes not available, or kernel32 missing the symbol — both
        # exceptional enough that swallowing is correct: a leftover
        # console window is far better than a startup crash.
        pass


def main(argv: list[str] | None = None) -> None:
    """Dispatch to the GUI or the CLI depending on the arguments."""
    _prepare_frozen_path()
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        from ficary.cli import main as cli_main

        cli_main(args)
        return
    _detach_windows_console_for_gui()
    try:
        from ficary.gui import main as gui_main
    except ImportError:
        print("GUI requires wxPython: pip install 'ficary[gui]'")
        print("Running CLI help instead:\n")
        from ficary.cli import main as cli_main

        cli_main(["--help"])
        return
    gui_main()
