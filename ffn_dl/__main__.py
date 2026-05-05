"""Entry point for ffn-dl.

- With arguments: runs the CLI  (ffn-dl https://...)
- Without arguments: launches the GUI  (double-click the exe)
"""

import sys
import os

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))


def _detach_windows_console_for_gui() -> None:
    """Drop the console window the OS attached to a GUI launch.

    PyInstaller builds ffn-dl.exe with ``--console`` so the same
    binary can serve CLI users — running ``ffn-dl https://...`` from
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


def main():
    if len(sys.argv) > 1:
        from ffn_dl.cli import main as cli_main
        cli_main()
    else:
        _detach_windows_console_for_gui()
        try:
            from ffn_dl.gui import main as gui_main
            gui_main()
        except ImportError:
            print("GUI requires wxPython: pip install 'ffn-dl[gui]'")
            print("Running CLI help instead:\n")
            from ffn_dl.cli import main as cli_main
            cli_main(["--help"])


main()
