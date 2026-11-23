"""The update check says so when GitHub is limiting requests, and the tests no longer make such requests.

GitHub answers 60 requests an hour without a sign-in, counted per internet address. Past that it answers 403, which the updater showed as "Update check failed: HTTP Error 403". Every test that opened the main window made one of those requests for real, so a day of test runs used the allowance up for the whole household, the real updater on another computer included.
"""

from __future__ import annotations

import os
import time

import pytest

from ficary import self_update as su

NOW = 1_800_000_000.0


class Reply:
    """What the updater gets back from GitHub."""

    def __init__(self, status_code=200, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self):
        return self.payload


def used_up(minutes_until_reset: float = 44) -> Reply:
    return Reply(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW + minutes_until_reset * 60))},
        text='{"message": "API rate limit exceeded for 203.0.113.7."}',
    )


def answer_with(monkeypatch, reply: Reply) -> None:
    monkeypatch.setattr(su.curl_requests, "get", lambda *args, **kwargs: reply)


class TestRecognisingTheLimit:
    def test_the_allowance_being_used_up_is_recognised(self):
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su._raise_if_rate_limited(used_up(), now=NOW)
        assert limited.value.reset_at == NOW + 44 * 60

    def test_header_names_are_matched_whatever_their_capitals(self):
        reply = Reply(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW + 600))})
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su._raise_if_rate_limited(reply, now=NOW)
        assert limited.value.reset_at == NOW + 600

    def test_being_told_to_wait_is_recognised(self):
        """GitHub's other kind of limit says how many seconds to wait."""
        reply = Reply(403, headers={"Retry-After": "120"})
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su._raise_if_rate_limited(reply, now=NOW)
        assert limited.value.reset_at == NOW + 120

    def test_a_429_is_a_limit_even_with_nothing_else_to_go_on(self):
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su._raise_if_rate_limited(Reply(429), now=NOW)
        assert limited.value.reset_at is None

    def test_the_words_in_the_reply_are_enough(self):
        reply = Reply(403, text='{"message": "API rate limit exceeded for 203.0.113.7."}')
        with pytest.raises(su.UpdateCheckRateLimited):
            su._raise_if_rate_limited(reply, now=NOW)

    def test_another_kind_of_403_is_not_called_a_limit(self):
        reply = Reply(403, headers={"X-RateLimit-Remaining": "37"}, text='{"message": "Forbidden"}')
        su._raise_if_rate_limited(reply, now=NOW)

    @pytest.mark.parametrize("status", [200, 404, 500])
    def test_other_answers_are_left_alone(self, status):
        su._raise_if_rate_limited(Reply(status, headers={"X-RateLimit-Remaining": "0"}), now=NOW)

    def test_a_reply_with_no_status_or_headers_is_left_alone(self):
        """Stand-ins used by other tests have only what the updater used to read."""
        class Bare:
            def json(self):
                return {}

        su._raise_if_rate_limited(Bare(), now=NOW)

    def test_a_reset_time_that_is_not_a_number_is_treated_as_unknown(self):
        reply = Reply(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "soon"})
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su._raise_if_rate_limited(reply, now=NOW)
        assert limited.value.reset_at is None


class TestWhatTheUserIsTold:
    def test_it_says_what_is_happening_when_it_ends_and_where_the_download_is(self):
        limited = su.UpdateCheckRateLimited(NOW + 44 * 60, now=NOW)
        message = str(limited)
        clock = time.strftime("%H:%M", time.localtime(NOW + 44 * 60))
        assert "GitHub is not answering update checks from this internet connection" in message
        assert "60 requests an hour" in message
        assert f"at {clock}, in 44 minutes" in message
        assert "https://github.com/matalvernaz/ficary/releases/latest" in message
        assert "403" not in message

    @pytest.mark.parametrize(
        "seconds, words",
        [(44 * 60, "in 44 minutes"), (60, "in 1 minute"), (61, "in 2 minutes"), (20, "in 1 minute")],
    )
    def test_the_wait_is_rounded_up_to_whole_minutes(self, seconds, words):
        assert words in su.UpdateCheckRateLimited(NOW + seconds, now=NOW).when()

    def test_a_time_already_past_is_any_moment_now(self):
        assert su.UpdateCheckRateLimited(NOW - 5, now=NOW).when() == "any moment now"

    def test_an_unknown_time_is_within_the_hour(self):
        limited = su.UpdateCheckRateLimited(None, now=NOW)
        assert limited.when() == "within the hour"
        assert "It will answer again within the hour." in str(limited)

    def test_the_line_for_the_status_pane_is_one_short_sentence_pair(self):
        brief = su.UpdateCheckRateLimited(NOW + 600, now=NOW).brief()
        assert brief.startswith("Update check skipped: GitHub is limiting requests")
        assert "in 10 minutes" in brief
        assert "\n" not in brief
        assert len(brief) < 160


class TestTheCheckItself:
    def test_a_limited_check_raises_the_explanation_not_the_bare_status(self, monkeypatch):
        answer_with(monkeypatch, used_up())
        with pytest.raises(su.UpdateCheckRateLimited) as limited:
            su.check_for_update()
        assert "HTTP Error" not in str(limited.value)

    def test_another_403_still_fails_the_ordinary_way(self, monkeypatch):
        answer_with(monkeypatch, Reply(403, text='{"message": "Forbidden"}'))
        with pytest.raises(RuntimeError, match="HTTP Error 403") as failed:
            su.check_for_update()
        assert not isinstance(failed.value, su.UpdateCheckRateLimited)

    def test_a_normal_answer_still_offers_the_update(self, monkeypatch):
        monkeypatch.setattr(su, "__version__", "1.0.0")
        monkeypatch.setattr(su.sys, "platform", "win32")
        answer_with(monkeypatch, Reply(200, payload={
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/matalvernaz/ficary/releases/tag/v9.9.9",
            "assets": [{"name": "ficary-portable.zip", "browser_download_url": "https://example.invalid/ficary-portable.zip", "size": 123, "digest": "sha256:abc"}],
        }))
        offered = su.check_for_update()
        assert offered["tag"] == "v9.9.9"
        assert offered["size"] == 123
        assert offered["digest"] == "sha256:abc"

    def test_the_notes_for_an_update_come_back_empty_when_limited(self, monkeypatch):
        """They are a nicety. A limit on fetching them must not get in the way of the update prompt."""
        answer_with(monkeypatch, used_up())
        assert su.fetch_changelog_since("1.0.0") == ""


class TestTheMainWindow:
    @pytest.fixture
    def gui(self):
        pytest.importorskip("wx")
        return pytest.importorskip("ficary.gui")

    class Window:
        """Stands in for the main window: what the two methods under test read."""

        def __init__(self):
            self.lines: list[str] = []
            self.prefs = self

        def get_bool(self, key):
            return True

        def get(self, key, default=None):
            return default

        def _log(self, line):
            self.lines.append(line)

        def _run_update_check(self):
            """Only its identity matters."""

        def _run_pending_repair_check(self):
            """Only its identity matters."""

    def test_at_launch_a_limit_is_one_plain_line_in_the_status_pane(self, gui, monkeypatch, caplog):
        window = self.Window()
        limited = su.UpdateCheckRateLimited(NOW + 600, now=NOW)

        def check():
            raise limited

        monkeypatch.setattr(su, "check_for_update", check)
        monkeypatch.setattr(gui.wx, "CallAfter", lambda function, *args: function(*args))
        gui.MainFrame._run_update_check(window)
        assert window.lines == [f"({limited.brief()})"]
        assert "Update check failed" not in caplog.text
        assert "Traceback" not in caplog.text

    def test_any_other_failure_is_reported_as_before(self, gui, monkeypatch):
        window = self.Window()

        def check():
            raise RuntimeError("HTTP Error 500")

        monkeypatch.setattr(su, "check_for_update", check)
        monkeypatch.setattr(gui.wx, "CallAfter", lambda function, *args: function(*args))
        gui.MainFrame._run_update_check(window)
        assert window.lines == ["(Update check failed: HTTP Error 500)"]

    def started_threads(self, gui, monkeypatch, window):
        started = []

        class Thread:
            def __init__(self, target=None, daemon=None, **options):
                self.target = target

            def start(self):
                started.append(self.target)

        monkeypatch.setattr(gui.threading, "Thread", Thread)
        monkeypatch.setattr(su, "cleanup_old_exe", lambda: None)
        monkeypatch.setattr(su, "is_frozen", lambda: False)
        gui.MainFrame._start_update_check(window)
        return started

    def test_the_launch_check_is_skipped_when_asked(self, gui, monkeypatch):
        monkeypatch.setenv("FICARY_SKIP_UPDATE_CHECK", "1")
        assert self.started_threads(gui, monkeypatch, self.Window()) == []

    def test_the_launch_check_runs_otherwise(self, gui, monkeypatch):
        monkeypatch.delenv("FICARY_SKIP_UPDATE_CHECK", raising=False)
        window = self.Window()
        assert self.started_threads(gui, monkeypatch, window) == [window._run_update_check]


class TestTheTestSuiteItself:
    def test_every_test_runs_with_the_launch_check_switched_off(self):
        assert os.environ.get(su.ENV_SKIP_UPDATE_CHECK)
