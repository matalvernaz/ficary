"""The desktop build detaches from its console and must then survive
every kind of write to the standard streams.

Run in a subprocess: the helper under test re-points descriptors 0-2,
which would otherwise fight pytest's own capture.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_quiet_std_streams_makes_every_std_write_safe(tmp_path):
    log_file = tmp_path / "app.log"
    code = textwrap.dedent(f"""
        import logging, os, subprocess, sys
        from ficary.entrypoint import _quiet_std_streams

        logging.basicConfig(
            filename={str(log_file)!r}, level=logging.DEBUG,
            format="%(name)s %(levelname)s %(message)s",
        )
        _quiet_std_streams()

        # print() used to raise OSError here once the console was gone.
        print("to stdout")
        print("to stderr", file=sys.stderr)
        sys.stdout.write("unterminated")
        sys.stdout.flush()
        # C-level writers and inherited descriptors land on the null device.
        os.write(1, b"raw write to fd 1")
        assert not sys.stdout.isatty()
        assert sys.stdout.encoding == "utf-8"
        assert sys.stdout.fileno() == 1 and sys.stderr.fileno() == 2
        # A child that inherits only some of our streams still starts.
        out = subprocess.run(
            [sys.executable, "-c", "print('child ok')"],
            stdout=subprocess.PIPE, check=True,
        ).stdout
        assert out.strip() == b"child ok", out
        logging.shutdown()
    """)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # Nothing escapes to the real descriptors any more.
    assert proc.stdout == ""
    assert proc.stderr == ""
    # ...and the Python-level writes became log records instead of
    # vanishing, at the levels the GUI's panes expect.
    log = log_file.read_text(encoding="utf-8")
    assert "ficary.stdout DEBUG to stdout" in log
    assert "ficary.stderr WARNING to stderr" in log
    assert "ficary.stdout DEBUG unterminated" in log
    assert "raw write" not in log
