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

import logging
import os
import sys


def _prepare_frozen_path() -> None:
    """Let a frozen build import the modules sitting beside its exe."""
    if getattr(sys, "frozen", False):
        sys.path.insert(0, os.path.dirname(sys.executable))


# Win32 ``GetStdHandle``/``SetStdHandle`` identifiers, keyed by the C
# runtime descriptor each one backs.
_WIN32_STD_HANDLE_IDS = {0: -10, 1: -11, 2: -12}


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

    Detaching leaves the process's standard streams pointing at a
    console it no longer has. Every later ``print`` — and the CLI
    helpers the GUI shares report through ``print`` — then raised
    ``OSError: [WinError 6] The handle is invalid``. In user logs that
    read as a failed auto-index after every download, and as a
    download failing a second time inside its own error handler. Any
    subprocess started with only some of its streams redirected
    inherited the same dead handles and failed the same way. So after
    ``FreeConsole`` the streams are re-pointed somewhere that accepts
    writes; see :func:`_quiet_std_streams`.

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
    _quiet_std_streams()


def _quiet_std_streams() -> None:
    """Re-point the standard streams at the null device and route
    Python-level writes into the log.

    Three layers need fixing, because three kinds of writer use them:

    * The C runtime descriptors 0-2, used by C libraries and inherited
      by child processes: the null device is ``dup2``'d over them.
    * The Win32 standard handles, which ``subprocess`` consults when a
      caller redirects only some of a child's streams: pointed at the
      same null device.
    * ``sys.stdout``/``sys.stderr``, used by ``print``: replaced with a
      :class:`~ficary.logging_utils.LoggingStream` each, so CLI-shared
      code that prints a diagnostic lands it in the log file and the
      status pane instead of losing it. stdout goes in at DEBUG (it is
      the CLI's ordinary narration), stderr at WARNING.

    Portable on purpose — only the handle step is Windows-only — so a
    test can exercise it on any platform.
    """
    from ficary.logging_utils import LoggingStream

    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    try:
        null_fd = os.open(os.devnull, os.O_RDWR)
    except OSError:
        null_fd = None
    if null_fd is not None:
        for fd in (0, 1, 2):
            try:
                os.dup2(null_fd, fd)
            except OSError:
                continue
            if sys.platform == "win32":
                _point_win32_std_handle_at(fd)
        try:
            os.close(null_fd)
        except OSError:
            pass
    sys.stdout = LoggingStream("ficary.stdout", logging.DEBUG, fd=1)
    sys.stderr = LoggingStream("ficary.stderr", logging.WARNING, fd=2)


def _point_win32_std_handle_at(fd: int) -> None:
    """Make the Win32 standard handle for ``fd`` the descriptor's own
    (freshly null-backed) OS handle."""
    try:
        import ctypes
        import msvcrt

        set_std_handle = ctypes.windll.kernel32.SetStdHandle
        set_std_handle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        set_std_handle.restype = ctypes.c_int
        set_std_handle(
            _WIN32_STD_HANDLE_IDS[fd] & 0xFFFFFFFF,
            ctypes.c_void_p(msvcrt.get_osfhandle(fd)),
        )
    except (OSError, AttributeError, KeyError, ValueError):
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
