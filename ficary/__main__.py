"""``python -m ficary`` — thin shim over the shared entry point.

- With arguments: runs the CLI  (ficary https://...)
- Without arguments: launches the GUI  (double-click the exe)

The dispatch itself lives in :mod:`ficary.entrypoint` so the console
script, ``python -m ficary`` and the frozen build all take the same
path. Keep this file a shim: it runs on import, which is exactly what
``python -m`` wants and exactly what a console-script target must not do.
"""

from ficary.entrypoint import main

main()
