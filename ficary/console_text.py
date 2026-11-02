"""Console output that never crashes on a character the console cannot show.

A Windows console on a legacy code page such as cp1252 has no glyph for an
arrow, most symbols, or any CJK text, and Python's default for standard
output is to raise ``UnicodeEncodeError`` at the first one. A single story
title containing "→" used to end a whole ``--update-library`` run near its
end. The frozen build ignores ``PYTHONUTF8`` and ``PYTHONIOENCODING``, so
those are no way out for users either.

Forcing UTF-8 onto such a console would trade the crash for mojibake, so
each character the stream cannot encode is replaced by the closest
plain-text stand-in instead: "->" for an arrow, the base letter for an
accented one, "?" when there is nothing sensible.
"""
from __future__ import annotations

import codecs
import sys
import unicodedata

ERROR_HANDLER_NAME = "ficary-console"

# Plain-text stand-ins for characters that turn up in titles, summaries and
# status lines. They are only used for characters the stream's own encoding
# cannot represent, and every value is ASCII so it encodes under any code
# page.
_STAND_INS = {
    " ": " ",  # no-break space
    " ": " ",  # thin space
    " ": " ",  # narrow no-break space
    "​": "",  # zero-width space
    "‌": "",  # zero-width non-joiner
    "‍": "",  # zero-width joiner
    "⁠": "",  # word joiner
    "﻿": "",  # byte order mark
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "--",  # em dash
    "―": "--",  # horizontal bar
    "‘": "'",
    "’": "'",
    "‚": ",",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "…": "...",
    "•": "*",  # bullet
    "⁄": "/",  # fraction slash, which is what "⅓" breaks down into
    "←": "<-",
    "↑": "^",
    "→": "->",
    "↓": "v",
    "↔": "<->",
    "⇐": "<=",
    "⇒": "=>",
    "⇔": "<=>",
    "★": "*",  # black star
    "☆": "*",  # white star
    "♡": "<3",  # white heart suit
    "♥": "<3",  # black heart suit
    "❤": "<3",  # heavy black heart
}


def _stand_in(character: str) -> str:
    """Return the plain-text stand-in for one unencodable character."""
    known = _STAND_INS.get(character)
    if known is not None:
        return known

    # Accented letters, ligatures and fullwidth forms break down into plain
    # letters plus combining marks; keep the letters and drop the marks.
    # A breakdown that still holds something unprintable is no stand-in.
    pieces = []
    for part in unicodedata.normalize("NFKD", character):
        if unicodedata.combining(part):
            continue
        if part.isascii():
            pieces.append(part)
            continue
        replacement = _STAND_INS.get(part)
        if replacement is None:
            return "?"
        pieces.append(replacement)
    plain = "".join(pieces)
    if not plain:
        return "?"
    return plain


def _replace_unencodable(error: UnicodeError) -> tuple[str, int]:
    """Codec error handler: swap each unencodable character for its stand-in."""
    if not isinstance(error, UnicodeEncodeError):
        raise error
    unencodable = error.object[error.start:error.end]
    replacement = "".join(_stand_in(character) for character in unencodable)
    return replacement, error.end


codecs.register_error(ERROR_HANDLER_NAME, _replace_unencodable)


def _encodes_all_of_unicode(encoding: str | None) -> bool:
    """True for the UTF encodings, which have a byte sequence for everything."""
    if not encoding:
        return False
    try:
        name = codecs.lookup(encoding).name
    except LookupError:
        return False
    return name.startswith("utf-")


def make_console_output_safe() -> None:
    """Give stdout and stderr the stand-in handler when their encoding
    cannot represent every character.

    Streams that are already UTF-8 (a modern Windows console, or a redirect
    on a UTF-8 system) are left exactly as they are. So is anything without
    ``reconfigure``, such as the logging stream the desktop build swaps in
    once it has let go of its console.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if _encodes_all_of_unicode(getattr(stream, "encoding", None)):
            continue
        try:
            reconfigure(errors=ERROR_HANDLER_NAME)
        except (ValueError, OSError):
            # A closed or detached stream will not be printed to anyway.
            continue
