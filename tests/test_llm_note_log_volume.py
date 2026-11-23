"""Tests for how much the LLM author's-note check writes to the debug log.

At the Debug log level it used to copy the start of every paragraph it sent to the classifier into the log. In one real set of log files that was 20,459 of 22,106 lines and 3.7 of 3.9 MB: a megabyte every thirty seconds of a download. The log is kept to four megabytes, so one long story pushed out everything else, and the text of every story read ended up in a file that gets attached to bug reports.
"""

from __future__ import annotations

import json
import logging

import pytest

from ficary import attribution

PARAGRAPH_COUNT = 60
FLAGGED = (1, 60)


def paragraphs() -> list[str]:
    return [f"Paragraph number {number} of the story." for number in range(1, PARAGRAPH_COUNT + 1)]


def config() -> dict:
    return {"provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "k", "endpoint": ""}


def classify_at_debug(monkeypatch, caplog) -> list[str]:
    """Classify one chapter with debug logging on and return the classifier's log lines."""
    reply = json.dumps({
        str(number): number in FLAGGED for number in range(1, PARAGRAPH_COUNT + 1)
    })
    monkeypatch.setattr(attribution, "_llm_call", lambda **kwargs: reply)
    with caplog.at_level(logging.DEBUG, logger="ficary.attribution"):
        flagged = attribution.classify_authors_notes_via_llm(paragraphs(), llm_config=config())
    assert flagged == {number - 1 for number in FLAGGED}
    return [record.getMessage() for record in caplog.records]


def test_the_debug_log_does_not_copy_the_whole_chapter(monkeypatch, caplog):
    monkeypatch.delenv("FICARY_LLM_LOG_PARAGRAPHS", raising=False)
    monkeypatch.delenv("FFN_DL_LLM_LOG_PARAGRAPHS", raising=False)
    lines = classify_at_debug(monkeypatch, caplog)
    # A summary, the raw reply, the parsed count and the flagged paragraphs: a handful of lines, not one per paragraph.
    assert len(lines) < 10
    story_lines = [line for line in lines if "Paragraph number 30 of the story" in line]
    assert story_lines == []


def test_the_debug_log_shows_the_paragraphs_that_were_flagged(monkeypatch, caplog):
    """They are what is about to be removed from the story, so they are what someone checking the classifier's work needs to see."""
    monkeypatch.delenv("FICARY_LLM_LOG_PARAGRAPHS", raising=False)
    lines = classify_at_debug(monkeypatch, caplog)
    flagged_lines = [line for line in lines if line.startswith("LLM A/N flagged")]
    assert len(flagged_lines) == len(FLAGGED)
    assert "[1]" in flagged_lines[0] and "Paragraph number 1 of the story." in flagged_lines[0]
    assert "[60]" in flagged_lines[1] and "Paragraph number 60 of the story." in flagged_lines[1]


def test_the_switch_brings_every_paragraph_back(monkeypatch, caplog):
    monkeypatch.setenv("FICARY_LLM_LOG_PARAGRAPHS", "1")
    lines = classify_at_debug(monkeypatch, caplog)
    input_lines = [line for line in lines if line.startswith("LLM A/N input  [")]
    assert len(input_lines) == PARAGRAPH_COUNT


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " OFF "])
def test_switch_values_that_mean_off(monkeypatch, value):
    monkeypatch.setenv("FICARY_LLM_LOG_PARAGRAPHS", value)
    assert attribution._log_every_note_paragraph() is False


@pytest.mark.parametrize("value", ["1", "yes", "true", "on"])
def test_switch_values_that_mean_on(monkeypatch, value):
    monkeypatch.setenv("FICARY_LLM_LOG_PARAGRAPHS", value)
    assert attribution._log_every_note_paragraph() is True


def test_nothing_is_written_at_the_default_level(monkeypatch, caplog):
    """The default level is Info. The classifier's own chatter is all Debug."""
    reply = json.dumps({str(number): False for number in range(1, PARAGRAPH_COUNT + 1)})
    monkeypatch.setattr(attribution, "_llm_call", lambda **kwargs: reply)
    with caplog.at_level(logging.INFO, logger="ficary.attribution"):
        attribution.classify_authors_notes_via_llm(paragraphs(), llm_config=config())
    assert caplog.records == []


def test_a_long_paragraph_is_cut_to_one_short_line():
    preview = attribution._note_log_preview("  First line.\nSecond line. " + "x" * 500)
    assert "\n" not in preview
    assert len(preview) == attribution._NOTE_LOG_PREVIEW_CHARS
    assert preview.startswith("First line. Second line.")
