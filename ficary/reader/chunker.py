"""Split chapter text into ordered chunks with exact character offsets.

Each :class:`Chunk` carries ``start``/``end`` offsets into the *exact* string
the reader displays, so a highlight range or caret position lines up with the
text. Paragraphs are the natural unit (``html_to_text`` separates them with a
blank line); an oversized paragraph is sub-split at sentence boundaries so
live-TTS first-audio latency stays about one sentence rather than a whole
paragraph. Used by the screen-reader view (paragraph navigation) and, in
Phase 2, by live TTS.
"""
from __future__ import annotations

from dataclasses import dataclass

# A chunk longer than this is sub-split at sentence boundaries.
MAX_CHUNK_CHARS = 400


@dataclass
class Chunk:
    index: int
    start: int
    end: int
    text: str


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Return ordered chunks whose ``text`` equals ``text[start:end]``."""
    from ..tts import _split_oversized_text  # lazy: avoids importing tts at reader load

    chunks: list[Chunk] = []
    index = 0
    for para_start, para_end in _paragraph_spans(text):
        para = text[para_start:para_end]
        pieces = [para] if len(para) <= max_chars else _split_oversized_text(para, max_chars)
        search_from = para_start
        for piece in pieces:
            if not piece.strip():
                continue
            found = text.find(piece, search_from, para_end)
            start = found if found >= 0 else search_from
            end = start + len(piece)
            chunks.append(Chunk(index=index, start=start, end=end, text=piece))
            index += 1
            search_from = end
    return chunks


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offset pairs for each non-empty paragraph, where
    paragraphs are separated by a blank line."""
    spans: list[tuple[int, int]] = []
    pos, n = 0, len(text)
    while pos < n:
        while pos < n and text[pos] == "\n":
            pos += 1
        if pos >= n:
            break
        nl = text.find("\n\n", pos)
        block_end = n if nl == -1 else nl
        end = block_end
        while end > pos and text[end - 1] in " \t\n":
            end -= 1
        if end > pos:
            spans.append((pos, end))
        pos = block_end
    return spans
