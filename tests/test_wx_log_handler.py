"""Tests that the handler which shows log messages in the status pane never raises into the code that logged.

A worker thread can outlive the window: an update check that fails just as the app is closing still logs its failure. The handler passes each message to the main thread with wx.CallAfter, which raises RuntimeError once the app is being torn down and fails a plain assert when there is no app at all. Only the first was caught, so the second surfaced as a traceback in the worker thread, for what was only a log message.
"""

from __future__ import annotations

import logging

import pytest

wx = pytest.importorskip("wx")

from ficary.gui import _WxLogHandler  # noqa: E402


def record(message: str = "something happened") -> logging.LogRecord:
    return logging.LogRecord("ficary.test", logging.WARNING, __file__, 1, message, None, None)


def test_a_message_logged_with_no_app_is_dropped_quietly(monkeypatch):
    monkeypatch.setattr(wx, "GetApp", lambda: None)
    shown: list[str] = []
    _WxLogHandler(shown.append).emit(record())
    assert shown == []


def test_a_message_logged_while_the_app_is_closing_is_dropped_quietly(monkeypatch):
    def closing(*args, **kwargs):
        raise RuntimeError("wrapped C/C++ object of type App has been deleted")

    monkeypatch.setattr(wx, "CallAfter", closing)
    _WxLogHandler(lambda message: None).emit(record())


def test_other_failures_are_not_swallowed(monkeypatch):
    """Only the two "no app" cases are expected. Anything else is a bug worth hearing about."""
    def broken(*args, **kwargs):
        raise ValueError("unexpected")

    monkeypatch.setattr(wx, "CallAfter", broken)
    with pytest.raises(ValueError):
        _WxLogHandler(lambda message: None).emit(record())


def test_a_message_reaches_the_pane_when_there_is_an_app(monkeypatch):
    passed_on: list = []
    monkeypatch.setattr(wx, "CallAfter", lambda target, message: passed_on.append((target, message)))
    shown: list[str] = []
    handler = _WxLogHandler(shown.append)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(record("hello"))
    assert len(passed_on) == 1
    assert passed_on[0][1] == "hello"


def test_a_line_the_status_pane_already_has_is_not_sent_again(monkeypatch):
    passed_on: list = []
    monkeypatch.setattr(wx, "CallAfter", lambda target, message: passed_on.append(message))
    already_shown = record()
    already_shown.ui_already_emitted = True
    _WxLogHandler(lambda message: None).emit(already_shown)
    assert passed_on == []
