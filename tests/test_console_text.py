"""Console output survives characters the console's code page lacks.

Reported as ficary#1: on a Windows console using cp1252 the command line
died with ``UnicodeEncodeError`` the moment it printed a story title that
held an arrow, which ended a whole library update near its end, and search
results crashed the same way.
"""
from __future__ import annotations

import io
import sys

from ficary import console_text, entrypoint


def _cp1252_stream() -> io.TextIOWrapper:
    """A text stream that encodes the way a legacy Windows console does."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\n")


def _written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("cp1252")


def test_cli_launch_prints_an_arrow_title_on_a_cp1252_console(monkeypatch):
    stdout = _cp1252_stream()
    stderr = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    def fake_cli_main(args):
        # The shape of the line _run_update_queue was printing when it died.
        print("  [3/40] Updated: Harry → the Next Generation (12 → 13 chapters)")
        print("warning: 東京 → retry", file=sys.stderr)

    monkeypatch.setattr("ficary.cli.main", fake_cli_main)
    entrypoint.main(["--update-library", "somewhere"])

    assert _written(stdout) == (
        "  [3/40] Updated: Harry -> the Next Generation (12 -> 13 chapters)\n"
    )
    assert _written(stderr) == "warning: ?? -> retry\n"


def test_search_results_keep_what_cp1252_can_show(monkeypatch):
    from ficary import cli

    stdout = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    console_text.make_console_output_safe()

    cli._print_search_results([{
        "title": "Pokémon: Ash’s Journey → Kalos ★",
        "author": "Zoë",
        "fandom": "Pokémon",
        "words": "12,000",
        "chapters": 5,
        "rating": "T",
        "status": "Complete",
        "summary": "Szőke and 東京 — a summary…",
    }])

    output = _written(stdout)
    # cp1252 has é, ë, the curly apostrophe, the em dash and the ellipsis,
    # so those print as themselves; only the rest are stood in for.
    assert "Pokémon: Ash’s Journey -> Kalos *" in output
    assert "by Zoë | Pokémon" in output
    assert "Szoke and ?? — a summary…" in output


def test_a_utf8_stream_is_left_alone(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    console_text.make_console_output_safe()
    assert stream.errors == "strict"
    print("A → B")
    stream.flush()
    assert stream.buffer.getvalue().decode("utf-8") == "A → B\n"


def test_a_stream_without_reconfigure_is_left_alone(monkeypatch):
    # The desktop build swaps a logging stream in for stdout once it has let
    # go of its console; it has no reconfigure and nothing to fix.
    class WriteOnly:
        encoding = "cp1252"

        def __init__(self):
            self.lines = []

        def write(self, text):
            self.lines.append(text)

        def flush(self):
            pass

    stream = WriteOnly()
    monkeypatch.setattr(sys, "stdout", stream)
    console_text.make_console_output_safe()
    print("A → B")
    assert "".join(stream.lines) == "A → B\n"


def test_stand_ins():
    assert console_text._stand_in("→") == "->"
    assert console_text._stand_in("ő") == "o"
    assert console_text._stand_in("ﬁ") == "fi"
    assert console_text._stand_in("２") == "2"
    assert console_text._stand_in("⅓") == "1/3"
    assert console_text._stand_in("​") == ""
    assert console_text._stand_in("東") == "?"
    assert console_text._stand_in("😀") == "?"
