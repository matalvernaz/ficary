"""PyInstaller hook for prism (PyPI ``prismatoid``), the screen-reader speech library.

prism keeps its cffi bridge and native library in ``prism/_native/``, a plain folder that
``prism/_native.py`` adds to ``prism.__path__`` at import time. Nothing PyInstaller does by
default reaches the bridge: the module graph is static, so it never sees a module under a path
added at run time; ``collect_data_files`` leaves extensions out; and ``collect_dynamic_libs``
looks for ``*.dll``, ``*.dylib`` and ``lib*.so``, which ``_prism_cffi.pyd`` and
``_prism_cffi.abi3.so`` both miss.

Without this the frozen app raises ``ModuleNotFoundError: prism._prism_cffi`` on
``import prism``, and announcements go quiet with only a debug line to say why. The release
builds pass ``--additional-hooks-dir tools/pyinstaller_hooks`` so PyInstaller finds it, and
``ficary --check-announcements`` run inside each build proves it worked.

The same hook as GenericMUD's, where this was first worked out.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

# _cffi_backend is loaded from C when the bridge starts, so no Python source imports it and the
# module graph misses it the same way.
hiddenimports = ["_cffi_backend"]

binaries = collect_dynamic_libs(
    "prism", search_patterns=["*.dll", "*.dylib", "*.so", "*.pyd"]
)
