"""Oversized-segment splitting for the TTS pipeline.

edge-tts silently returns empty audio for a payload over its internal
ceiling, so every chunk handed to it must stay within ``max_len`` —
including a single unbroken token (a URL, or a run-on with no spaces),
which the word-boundary splitter can't break on its own.
"""

from __future__ import annotations

from ffn_dl.tts import _MAX_SEGMENT_CHARS, _split_oversized_text


def test_short_text_unchanged():
    assert _split_oversized_text("Hello there.") == ["Hello there."]


def test_every_chunk_within_limit():
    text = ("A normal sentence. " * 500).strip()
    parts = _split_oversized_text(text)
    assert len(parts) > 1
    assert all(len(p) <= _MAX_SEGMENT_CHARS for p in parts)


def test_spaceless_token_is_hard_sliced():
    # A single token longer than the ceiling: must be sliced, never
    # emitted whole (which edge-tts answers with silent empty audio).
    token = "x" * (_MAX_SEGMENT_CHARS * 3 + 17)
    parts = _split_oversized_text(token)
    assert all(len(p) <= _MAX_SEGMENT_CHARS for p in parts)
    # No characters lost in the slice.
    assert "".join(parts) == token


def test_long_token_embedded_in_prose():
    token = "y" * (_MAX_SEGMENT_CHARS + 50)
    text = f"Before the link. {token} After the link."
    parts = _split_oversized_text(text)
    assert all(len(p) <= _MAX_SEGMENT_CHARS for p in parts)
    joined = " ".join(parts)
    assert "Before the link." in joined
    assert "After the link." in joined
    assert token in joined.replace(" ", "")
