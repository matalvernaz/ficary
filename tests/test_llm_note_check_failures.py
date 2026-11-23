"""Tests for what happens when the LLM author's-note check cannot do its job.

They exist because of one real download: an Anthropic account ran out of credit, every chapter's request was refused, and the app reported "no A/N paragraphs found" for all seventy chapters, kept asking for each one, and saved "no notes here" as every chapter's result so none of them would ever be checked again. The error body used below is the one that download logged.

Each test names one thing a person relies on: the check stops when asking again is pointless, a failure is never reported or remembered as a clean chapter, the book still gets written, and the closing line says what happened.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from ficary import attribution, cli, exporters, tts
from ficary.models import Chapter, Story


# The body Anthropic sent with HTTP 400 when the account had no credit left, as logged (only the request id is changed).
ANTHROPIC_OUT_OF_CREDIT_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"Your credit balance is too low to access the Anthropic API. Please go '
    'to Plans & Billing to upgrade or purchase credits."},'
    '"request_id":"req_test"}'
)

# OpenAI's answer for a spent quota. It arrives as a 429, the status that normally means "slow down and try again".
OPENAI_OUT_OF_QUOTA_BODY = (
    '{"error":{"message":"You exceeded your current quota, please check your '
    'plan and billing details.","type":"insufficient_quota","param":null,'
    '"code":"insufficient_quota"}}'
)

PARAGRAPHS_PER_CHAPTER = 4

# Words that make each chapter's text different, so each chapter gets its own cache entry and its own request. Plain prose on purpose: a word like "Chapter" would be caught by the regex pass before the LLM ever saw it.
CHAPTER_WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima",
]


def make_story(chapter_count: int) -> Story:
    """A story whose every chapter is long enough for the LLM pass to run."""
    story = Story(
        id=1,
        title="T",
        author="A",
        summary="",
        url="https://www.fanfiction.net/s/1",
    )
    for number in range(1, chapter_count + 1):
        word = CHAPTER_WORDS[(number - 1) % len(CHAPTER_WORDS)]
        story.chapters.append(Chapter(
            number=number,
            title=f"Part {number}",
            html=(
                f"<p>The {word} morning was bright and clear.</p>"
                f"<p>The {word} river ran swift past the village.</p>"
                f"<p>The {word} hawk circled overhead in silence.</p>"
                f"<p>The {word} cart wheeled away down the lane.</p>"
            ),
        ))
    return story


def all_false_reply(paragraph_count: int = PARAGRAPHS_PER_CHAPTER) -> str:
    """The model saying, in the documented shape, that nothing is a note."""
    return json.dumps({str(n): False for n in range(1, paragraph_count + 1)})


def anthropic_config() -> dict:
    return {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "api_key": "k",
        "endpoint": "",
    }


def ollama_config() -> dict:
    return {
        "provider": "ollama",
        "model": "llama3.1:8b",
        "api_key": "",
        "endpoint": "",
    }


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Keep the note-check cache inside the test's own folder."""
    root = tmp_path / "llm_an"

    def cache_path(site_name, story_id):
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{site_name}_{story_id}.json"

    monkeypatch.setattr(exporters, "_llm_an_cache_path", cache_path)
    return root


def cached_entries(cache_root: Path) -> dict:
    """Everything the note-check cache holds, across all stories."""
    entries: dict = {}
    if not cache_root.exists():
        return entries
    for cache_file in cache_root.glob("*.json"):
        entries.update(json.loads(cache_file.read_text(encoding="utf-8")))
    return entries


def http_error(status: int, body: str, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.example.invalid/v1/messages",
        code=status,
        msg="error",
        hdrs=headers,
        fp=io.BytesIO(body.encode("utf-8")),
    )


def refuse_every_request(monkeypatch, status: int, body: str) -> list:
    """Make the HTTP layer answer every LLM request with an error status, the way a real provider does. Returns the list of requests made."""
    requests: list = []

    def urlopen(request, timeout=None):
        requests.append(request)
        raise http_error(status, body)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    # A retried status sleeps between attempts; the tests must not.
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    return requests


def export(story, tmp_path, config, exporter=exporters.export_html):
    """Run an export with note stripping on, and return (path, status lines)."""
    lines: list[str] = []
    path = exporter(
        story,
        output_dir=str(tmp_path),
        strip_notes=True,
        llm_config=config,
        progress=lines.append,
    )
    return path, lines


# ── An account with no credit left ───────────────────────────────


class TestAccountOutOfCredit:
    def test_asks_once_then_stops(self, tmp_path, monkeypatch, cache_root):
        requests = refuse_every_request(
            monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY,
        )
        export(make_story(6), tmp_path, anthropic_config())
        assert len(requests) == 1

    @pytest.mark.parametrize(
        "exporter", [exporters.export_html, exporters.export_txt],
    )
    def test_stops_the_same_way_in_every_format(
        self, exporter, tmp_path, monkeypatch, cache_root,
    ):
        requests = refuse_every_request(
            monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY,
        )
        export(make_story(4), tmp_path, anthropic_config(), exporter)
        assert len(requests) == 1

    def test_epub_stops_too(self, tmp_path, monkeypatch, cache_root):
        pytest.importorskip("ebooklib")
        requests = refuse_every_request(
            monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY,
        )
        export(
            make_story(4), tmp_path, anthropic_config(), exporters.export_epub,
        )
        assert len(requests) == 1

    def test_never_claims_the_chapters_were_clean(
        self, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        _path, lines = export(make_story(5), tmp_path, anthropic_config())
        assert [line for line in lines if "no A/N paragraphs found" in line] == []

    def test_says_why_in_the_providers_own_words(
        self, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        _path, lines = export(make_story(5), tmp_path, anthropic_config())
        stop_lines = [
            line for line in lines
            if "skipping LLM for remaining chapters" in line
        ]
        assert len(stop_lines) == 1
        assert "Your credit balance is too low" in stop_lines[0]
        assert "Anthropic" in stop_lines[0]
        # The raw JSON envelope is noise to a person, and to a screen reader.
        assert "request_id" not in stop_lines[0]
        assert '{"type"' not in stop_lines[0]

    def test_closing_line_counts_the_chapters_that_went_without(
        self, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        _path, lines = export(make_story(5), tmp_path, anthropic_config())
        closing = lines[-1]
        assert "LLM note check did not run on 5 chapters" in closing
        assert "Your credit balance is too low" in closing
        assert "download the story again" in closing

    def test_closing_line_is_a_warning_in_the_log_file(
        self, tmp_path, monkeypatch, cache_root, caplog,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        with caplog.at_level(logging.INFO, logger="ficary.exporters"):
            export(make_story(3), tmp_path, anthropic_config())
        warnings = [
            record for record in caplog.records
            if record.levelno == logging.WARNING
            and "did not run on 3 chapters" in record.getMessage()
        ]
        assert len(warnings) == 1
        # The status pane already has the line; the GUI must not add it twice.
        assert warnings[0].ui_already_emitted is True

    def test_nothing_is_saved_as_the_chapters_result(
        self, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        export(make_story(5), tmp_path, anthropic_config())
        assert cached_entries(cache_root) == {}

    def test_next_download_checks_every_chapter_again(
        self, tmp_path, monkeypatch, cache_root,
    ):
        """The point of not saving a failure: once the account is topped up, downloading the story again has to ask about every chapter."""
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        story = make_story(5)
        export(story, tmp_path, anthropic_config())

        asked: list = []

        def working_llm(**kwargs):
            asked.append(kwargs)
            return all_false_reply()

        monkeypatch.setattr(attribution, "_llm_call", working_llm)
        _path, lines = export(story, tmp_path, anthropic_config())
        assert len(asked) == 5
        assert [line for line in lines if "cache hit" in line] == []

    def test_the_book_is_still_written_in_full(
        self, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        path, _lines = export(make_story(5), tmp_path, anthropic_config())
        body = path.read_text(encoding="utf-8")
        for word in CHAPTER_WORDS[:5]:
            assert f"The {word} morning was bright and clear." in body

    def test_spent_openai_quota_is_not_retried(self, monkeypatch):
        """A spent quota comes back as a 429. Sleeping and asking again cannot put money in the account, so it must fail at once."""
        requests = refuse_every_request(monkeypatch, 429, OPENAI_OUT_OF_QUOTA_BODY)
        with pytest.raises(attribution.LLMRejected) as raised:
            attribution._llm_call(
                provider="openai", model="gpt-x", api_key="k",
                endpoint="https://api.example.invalid/v1",
                system_prompt="", user_prompt="",
            )
        assert len(requests) == 1
        assert raised.value.will_repeat is True


# ── What counts as "this will keep happening" ────────────────────


class TestWhichRejectionsEndTheCheck:
    @pytest.mark.parametrize(
        "status, body",
        [
            (400, ANTHROPIC_OUT_OF_CREDIT_BODY),
            (401, '{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'),
            (402, '{"type":"error","error":{"type":"billing_error","message":"Payment required"}}'),
            (404, '{"type":"error","error":{"type":"not_found_error","message":"model: claude-nope"}}'),
            (404, '{"error":"model \'nope\' not found, try pulling it first"}'),
            (429, OPENAI_OUT_OF_QUOTA_BODY),
            (429, '{"type":"error","error":{"type":"rate_limit_error","message":"Spend limit reached","details":{"error_code":"enforced_spend_limit_reached"}}}'),
        ],
    )
    def test_account_and_settings_problems_repeat(self, status, body):
        assert attribution.LLMRejected(status, "anthropic", body).will_repeat is True

    @pytest.mark.parametrize(
        "status, body",
        [
            (400, '{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 250000 tokens > 200000 maximum"}}'),
            (403, '{"error":{"message":"Your input was flagged by moderation","code":403}}'),
            (413, '{"type":"error","error":{"type":"request_too_large","message":"Request exceeds the maximum allowed number of bytes."}}'),
            (429, '{"type":"error","error":{"type":"rate_limit_error","message":"Too many requests"}}'),
            (500, '{"type":"error","error":{"type":"api_error","message":"Internal server error"}}'),
            (529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
            (503, "<html>Bad gateway</html>"),
            (500, ""),
        ],
    )
    def test_problems_with_one_request_do_not(self, status, body):
        assert attribution.LLMRejected(status, "anthropic", body).will_repeat is False

    def test_a_rejection_is_not_an_unreachable_endpoint(self):
        """Code that stops on the first unreachable endpoint must not also stop on the first busy one."""
        rejected = attribution.LLMRejected(503, "openai", "")
        assert not isinstance(rejected, attribution.LLMUnavailable)
        assert isinstance(rejected, RuntimeError)

    def test_message_keeps_the_form_existing_logs_use(self):
        rejected = attribution.LLMRejected(400, "anthropic", "body text")
        assert str(rejected) == "LLM HTTP 400 from anthropic: body text"

    def test_explains_itself_without_a_readable_body(self):
        rejected = attribution.LLMRejected(503, "openai-compatible", "<html>")
        assert rejected.explain() == "The LLM server refused the request (HTTP 503)"

    def test_quotes_an_ollama_error(self):
        rejected = attribution.LLMRejected(
            404, "ollama", '{"error":"model \'nope\' not found"}',
        )
        assert rejected.explain() == (
            "Ollama refused the request (HTTP 404): \"model 'nope' not found\""
        )

    def test_a_long_provider_message_is_cut_short(self):
        long_message = "x" * 1000
        body = json.dumps({"error": {"message": long_message}})
        explained = attribution.LLMRejected(400, "openai", body).explain()
        assert len(explained) < 320
        assert explained.endswith('…"')


class TestRetries:
    def test_overloaded_is_retried(self, monkeypatch):
        """529 is Anthropic's "busy right now". One of those used to fail the chapter on the spot."""
        requests = refuse_every_request(
            monkeypatch, 529,
            '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        )
        with pytest.raises(attribution.LLMRejected) as raised:
            attribution._llm_call(
                provider="anthropic", model="m", api_key="k",
                endpoint="https://api.example.invalid/v1",
                system_prompt="", user_prompt="",
            )
        assert len(requests) == attribution._LLM_HTTP_RETRY_MAX
        assert raised.value.status == 529
        assert raised.value.will_repeat is False

    def test_the_error_body_survives_the_retries(self, monkeypatch):
        """The body can be read once per reply. The error raised after the last attempt still has to carry it."""
        body = '{"type":"error","error":{"type":"api_error","message":"Try later"}}'
        refuse_every_request(monkeypatch, 500, body)
        with pytest.raises(attribution.LLMRejected) as raised:
            attribution._llm_call(
                provider="anthropic", model="m", api_key="k",
                endpoint="https://api.example.invalid/v1",
                system_prompt="", user_prompt="",
            )
        assert raised.value.provider_message == "Try later"


# ── Failures that might pass ─────────────────────────────────────


class TestPassingFailures:
    def test_one_busy_reply_does_not_end_the_check(
        self, tmp_path, monkeypatch, cache_root,
    ):
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise attribution.LLMRejected(503, "anthropic", "")
            return all_false_reply()

        monkeypatch.setattr(attribution, "_llm_call", llm)
        _path, lines = export(make_story(5), tmp_path, anthropic_config())
        assert len(calls) == 5
        assert [
            line for line in lines
            if "skipping LLM for remaining chapters" in line
        ] == []
        assert "did not run on 1 chapter " in lines[-1]
        assert "(4 checked in full)" in lines[-1]

    def test_three_in_a_row_end_it(self, tmp_path, monkeypatch, cache_root):
        limit = attribution.NOTE_CHECK_MAX_CONSECUTIVE_FAILURES
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMRejected(503, "anthropic", "")

        monkeypatch.setattr(attribution, "_llm_call", llm)
        _path, lines = export(make_story(limit + 4), tmp_path, anthropic_config())
        assert len(calls) == limit
        stop_lines = [
            line for line in lines
            if "skipping LLM for remaining chapters" in line
        ]
        assert len(stop_lines) == 1
        assert f"({limit} chapters in a row)" in stop_lines[0]
        assert f"did not run on {limit + 4} chapters" in lines[-1]

    def test_a_good_chapter_resets_the_count(
        self, tmp_path, monkeypatch, cache_root,
    ):
        outcomes = ["fail", "fail", "ok", "fail", "fail", "ok"]
        calls: list = []

        def llm(**kwargs):
            outcome = outcomes[len(calls)]
            calls.append(outcome)
            if outcome == "fail":
                raise attribution.LLMRejected(503, "anthropic", "")
            return all_false_reply()

        monkeypatch.setattr(attribution, "_llm_call", llm)
        export(make_story(len(outcomes)), tmp_path, anthropic_config())
        assert len(calls) == len(outcomes)

    def test_mixed_kinds_of_failure_count_together(
        self, tmp_path, monkeypatch, cache_root,
    ):
        """A timeout, a busy reply and an empty reply in a row are three chapters with nothing to show, whatever each one's reason."""
        failures = [
            attribution.LLMTimeout("timed out"),
            attribution.LLMRejected(503, "anthropic", ""),
            "",
        ]
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            if len(calls) > len(failures):
                # Only reached if the check was not stopped. Answer
                # properly, so every remaining chapter is then asked
                # about and the count below shows it.
                return all_false_reply()
            failure = failures[len(calls) - 1]
            if isinstance(failure, Exception):
                raise failure
            return failure

        monkeypatch.setattr(attribution, "_llm_call", llm)
        export(make_story(8), tmp_path, anthropic_config())
        assert len(calls) == 3

    def test_gives_up_after_exactly_three(self, tmp_path, monkeypatch, cache_root):
        """The number is part of what the user is told ("3 chapters in a row"), so it is pinned here as a number and not read from the constant."""
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMTimeout("timed out")

        monkeypatch.setattr(attribution, "_llm_call", llm)
        _path, lines = export(make_story(9), tmp_path, ollama_config())
        assert len(calls) == 3
        assert "(3 chapters in a row)" in "".join(lines)


class TestTallyRules:
    """The stop rule on its own, without an export around it."""

    def failing_run(self):
        return attribution.NoteCheckRun(anthropic_config())

    def test_a_partly_answered_chapter_shows_the_llm_is_working(self):
        """Two failures, then a chapter that was half answered, then two more failures: never three in a row with nothing to show."""
        run = self.failing_run()
        busy = attribution.LLMRejected(503, "anthropic", "")
        partial = attribution.LLMIncompleteAnswer(
            {1}, unreadable_batches=1, batches=2, reason="not JSON",
        )
        run.chapter_failed(busy, "chapter 1")
        run.chapter_failed(busy, "chapter 2")
        run.chapter_partly_checked(partial)
        run.chapter_failed(busy, "chapter 4")
        run.chapter_failed(busy, "chapter 5")
        assert run.stopped is False
        assert run.config is not None

    def test_stopping_hands_out_no_more_configs(self):
        run = self.failing_run()
        run.chapter_failed(
            attribution.LLMRejected(401, "anthropic", ""), "chapter 1",
        )
        assert run.stopped is True
        assert run.config is None

    def test_the_first_problem_is_the_one_reported(self):
        run = self.failing_run()
        run.chapter_failed(attribution.LLMTimeout("slow"), "chapter 1")
        run.chapter_failed(
            attribution.LLMRejected(400, "anthropic", ANTHROPIC_OUT_OF_CREDIT_BODY),
            "chapter 2",
        )
        assert run.problem == "timeout (slow)"

    def test_a_run_nobody_asked_for_is_never_stopped_or_reported(self):
        run = attribution.NoteCheckRun(None)
        assert run.requested is False
        assert run.config is None
        assert run.summary() is None

    def test_closing_line_does_not_double_its_full_stop(self):
        run = self.failing_run()
        run.chapter_failed(
            attribution.LLMRejected(400, "anthropic", ANTHROPIC_OUT_OF_CREDIT_BODY),
            "chapter 1",
        )
        assert 'credits." Where it did not run' in run.summary()

    def test_closing_line_ends_a_plain_reason_with_a_full_stop(self):
        run = self.failing_run()
        run.chapter_failed(attribution.LLMUnavailable("refused"), "chapter 1")
        assert "endpoint unreachable (refused). Where it did not run" in run.summary()


# ── Replies that are not an answer ───────────────────────────────


class TestRepliesThatAreNotAnAnswer:
    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "   ",
            "I'm sorry, I can't help with that.",
            '{"1": true, "2": fal',
            '{"summary": "A quiet chapter about a river."}',
            '{"content": {"Harry looked up": {"speaker": "Harry"}}}',
        ],
    )
    def test_is_not_reported_or_saved_as_a_clean_chapter(
        self, reply, tmp_path, monkeypatch, cache_root,
    ):
        monkeypatch.setattr(attribution, "_llm_call", lambda **kwargs: reply)
        path, lines = export(make_story(1), tmp_path, anthropic_config())
        assert [line for line in lines if "no A/N paragraphs found" in line] == []
        assert cached_entries(cache_root) == {}
        assert "did not run on 1 chapter" in lines[-1]
        assert "The alpha morning was bright and clear." in path.read_text(
            encoding="utf-8",
        )

    @pytest.mark.parametrize(
        "reply",
        [
            all_false_reply(),
            "{}",
            '{"author_notes": []}',
            "```json\n" + all_false_reply() + "\n```",
        ],
    )
    def test_a_real_answer_of_none_is_reported_and_saved(
        self, reply, tmp_path, monkeypatch, cache_root,
    ):
        monkeypatch.setattr(attribution, "_llm_call", lambda **kwargs: reply)
        _path, lines = export(make_story(1), tmp_path, anthropic_config())
        assert len(
            [line for line in lines if "no A/N paragraphs found" in line]
        ) == 1
        assert list(cached_entries(cache_root).values()) == [{"flags": []}]
        assert lines[-1].strip() == (
            "LLM note check: 1 chapter checked, notes removed from 0."
        )

    def test_a_refusal_is_named_in_the_log(self, caplog):
        body = json.dumps({
            "type": "message", "role": "assistant", "content": [],
            "stop_reason": "refusal",
        })
        with caplog.at_level(logging.WARNING, logger="ficary.attribution"):
            text = attribution._extract_llm_text(body, "anthropic")
        assert text == ""
        assert "declined to answer" in caplog.text

    def test_a_cut_off_openai_reply_is_named_in_the_log(self, caplog):
        body = json.dumps({"choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": '{"1": tr'},
        }]})
        with caplog.at_level(logging.WARNING, logger="ficary.attribution"):
            text = attribution._extract_llm_text(body, "openai")
        assert text == '{"1": tr'
        assert "cut off" in caplog.text

    def test_an_ordinary_reply_logs_nothing(self, caplog):
        body = json.dumps({
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
        })
        with caplog.at_level(logging.WARNING, logger="ficary.attribution"):
            attribution._extract_llm_text(body, "anthropic")
        assert caplog.text == ""


class TestPartlyAnsweredChapter:
    """A long chapter goes to a local model in batches. One unreadable batch should cost that batch, not the chapter, and not be remembered as final."""

    def long_chapter_story(self) -> tuple[Story, int]:
        batch_size = attribution._AN_BATCH_SIZE
        paragraph_count = batch_size * 2
        story = Story(
            id=7, title="T", author="A", summary="",
            url="https://www.fanfiction.net/s/7",
        )
        story.chapters.append(Chapter(
            number=1,
            title="Part 1",
            html="".join(
                f"<p>Sentence number {n} of a long quiet scene by the water.</p>"
                for n in range(1, paragraph_count + 1)
            ),
        ))
        return story, batch_size

    def test_keeps_what_the_readable_batch_found(
        self, tmp_path, monkeypatch, cache_root,
    ):
        story, batch_size = self.long_chapter_story()
        # Batch one is unreadable. Batch two flags its last paragraph, which is the last paragraph of the chapter.
        replies = [
            "not JSON at all",
            json.dumps({str(n): n == batch_size for n in range(1, batch_size + 1)}),
        ]
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: replies.pop(0),
        )
        path, lines = export(story, tmp_path, ollama_config())
        body = path.read_text(encoding="utf-8")
        assert f"Sentence number {batch_size * 2} of" not in body
        assert "Sentence number 1 of" in body
        assert replies == []

    def test_is_not_saved_and_says_so(self, tmp_path, monkeypatch, cache_root):
        story, batch_size = self.long_chapter_story()
        replies = ["not JSON at all", all_false_reply(batch_size)]
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: replies.pop(0),
        )
        _path, lines = export(story, tmp_path, ollama_config())
        assert cached_entries(cache_root) == {}
        assert len(
            [line for line in lines if "part of this chapter could not be checked" in line]
        ) == 1
        # Half the chapter was never looked at, so "none found" would be a claim too far.
        assert [line for line in lines if "no A/N paragraphs found" in line] == []
        assert "only partly covered 1 chapter" in lines[-1]

    def test_classifier_hands_back_the_partial_flags(self, monkeypatch):
        batch_size = attribution._AN_BATCH_SIZE
        replies = [
            "not JSON at all",
            json.dumps({str(n): n == 7 for n in range(1, batch_size + 1)}),
        ]
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: replies.pop(0),
        )
        with pytest.raises(attribution.LLMIncompleteAnswer) as raised:
            attribution.classify_authors_notes_via_llm(
                ["p"] * (batch_size * 2), llm_config=ollama_config(),
            )
        assert raised.value.flagged == {batch_size + 6}
        assert raised.value.answered_batches == 1
        assert raised.value.unreadable_batches == 1


class TestVerificationRoundFails:
    """When the first pass flags a suspicious share of a chapter, a second request confirms it. If that request fails, nothing was confirmed."""

    def story_and_first_reply(self):
        story = make_story(1)
        # Three of four paragraphs flagged is well over the runaway threshold.
        first_reply = json.dumps({"1": True, "2": True, "3": True, "4": False})
        return story, first_reply

    def test_keeps_the_chapter_and_saves_nothing(
        self, tmp_path, monkeypatch, cache_root,
    ):
        story, first_reply = self.story_and_first_reply()
        replies = [first_reply, "the second look came back as prose"]
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: replies.pop(0),
        )
        path, lines = export(story, tmp_path, anthropic_config())
        body = path.read_text(encoding="utf-8")
        assert body.count("The alpha") == PARAGRAPHS_PER_CHAPTER
        assert cached_entries(cache_root) == {}
        said = [line for line in lines if "verification round could not be read" in line]
        assert len(said) == 1
        # "Dropped every flag" is what a model that answered would have done. This one did not answer.
        assert [line for line in lines if "verification dropped every flag" in line] == []
        assert "only partly covered 1 chapter" in lines[-1]

    def test_a_rejected_second_request_keeps_the_chapter_too(
        self, tmp_path, monkeypatch, cache_root,
    ):
        story, first_reply = self.story_and_first_reply()
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return first_reply
            raise attribution.LLMRejected(400, "anthropic", ANTHROPIC_OUT_OF_CREDIT_BODY)

        monkeypatch.setattr(attribution, "_llm_call", llm)
        path, lines = export(story, tmp_path, anthropic_config())
        body = path.read_text(encoding="utf-8")
        assert body.count("The alpha") == PARAGRAPHS_PER_CHAPTER
        assert cached_entries(cache_root) == {}
        assert "did not run on 1 chapter" in lines[-1]


# ── Results saved by earlier versions ────────────────────────────


class TestResultsSavedByEarlierVersions:
    """Earlier versions saved an empty list both for "no notes" and for "the request failed". Those cannot be told apart now, so an empty one is asked about again; one with flags can only be a real answer."""

    def seed_cache(self, cache_root: Path, story: Story, config: dict, value) -> str:
        chapter = story.chapters[0]
        paragraphs = [
            f"The alpha {rest}" for rest in (
                "morning was bright and clear.",
                "river ran swift past the village.",
                "hawk circled overhead in silence.",
                "cart wheeled away down the lane.",
            )
        ]
        assert all(paragraph in chapter.html for paragraph in paragraphs)
        key = exporters._llm_an_cache_key(paragraphs, config)
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / f"ffn_{story.id}.json").write_text(
            json.dumps({key: value}), encoding="utf-8",
        )
        return key

    def test_an_old_empty_result_is_asked_about_again(
        self, tmp_path, monkeypatch, cache_root,
    ):
        story = make_story(1)
        config = anthropic_config()
        key = self.seed_cache(cache_root, story, config, [])
        asked: list = []

        def llm(**kwargs):
            asked.append(kwargs)
            return json.dumps({"1": True, "2": False, "3": False, "4": False})

        monkeypatch.setattr(attribution, "_llm_call", llm)
        path, lines = export(story, tmp_path, config)
        assert len(asked) == 1
        assert [line for line in lines if "cache hit" in line] == []
        assert "The alpha morning was bright and clear." not in path.read_text(
            encoding="utf-8",
        )
        assert cached_entries(cache_root) == {key: {"flags": [0]}}

    def test_an_old_result_with_flags_is_still_used(
        self, tmp_path, monkeypatch, cache_root,
    ):
        story = make_story(1)
        config = anthropic_config()
        self.seed_cache(cache_root, story, config, [0])

        def llm(**kwargs):
            raise AssertionError("a result that has flags must not be asked about again")

        monkeypatch.setattr(attribution, "_llm_call", llm)
        path, lines = export(story, tmp_path, config)
        assert len([line for line in lines if "cache hit" in line]) == 1
        assert "The alpha morning was bright and clear." not in path.read_text(
            encoding="utf-8",
        )

    def test_a_new_empty_result_is_trusted(self, tmp_path, monkeypatch, cache_root):
        story = make_story(1)
        config = anthropic_config()
        asked: list = []

        def llm(**kwargs):
            asked.append(kwargs)
            return all_false_reply()

        monkeypatch.setattr(attribution, "_llm_call", llm)
        export(story, tmp_path, config)
        _path, lines = export(story, tmp_path, config)
        assert len(asked) == 1
        assert len([line for line in lines if "cache hit" in line]) == 1

    @pytest.mark.parametrize("contents", ["[]", '"text"', "{not json", ""])
    def test_a_damaged_cache_file_is_treated_as_empty(
        self, contents, tmp_path, monkeypatch, cache_root,
    ):
        story = make_story(1)
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / f"ffn_{story.id}.json").write_text(contents, encoding="utf-8")
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: all_false_reply(),
        )
        _path, lines = export(story, tmp_path, anthropic_config())
        assert len(
            [line for line in lines if "no A/N paragraphs found" in line]
        ) == 1


# ── The closing line when all went well ──────────────────────────


class TestClosingLine:
    def test_tallies_a_clean_run(self, tmp_path, monkeypatch, cache_root):
        replies = [
            json.dumps({"1": True, "2": False, "3": False, "4": False}),
            all_false_reply(),
            json.dumps({"1": False, "2": False, "3": False, "4": True}),
        ]
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: replies.pop(0),
        )
        _path, lines = export(make_story(3), tmp_path, anthropic_config())
        assert lines[-1].strip() == (
            "LLM note check: 3 chapters checked, notes removed from 2."
        )

    def test_says_nothing_when_no_check_was_asked_for(self, tmp_path, cache_root):
        lines: list[str] = []
        exporters.export_html(
            make_story(2), output_dir=str(tmp_path),
            strip_notes=True, llm_config=None, progress=lines.append,
        )
        assert [line for line in lines if "LLM note check" in line] == []

    def test_says_nothing_when_note_stripping_is_off(
        self, tmp_path, monkeypatch, cache_root,
    ):
        def llm(**kwargs):
            raise AssertionError("the LLM pass only runs behind the regex pass")

        monkeypatch.setattr(attribution, "_llm_call", llm)
        lines: list[str] = []
        exporters.export_html(
            make_story(2), output_dir=str(tmp_path),
            strip_notes=False, llm_config=anthropic_config(),
            progress=lines.append,
        )
        assert [line for line in lines if "LLM note check" in line] == []

    def test_chapters_from_the_existing_file_are_not_counted(
        self, tmp_path, monkeypatch, cache_root,
    ):
        """An update re-exports old chapters next to the new ones. The old ones are never sent to the LLM, so a stopped check must not count them as missed."""
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        story = make_story(4)
        for chapter in story.chapters[:3]:
            chapter.from_existing_file = True
        _path, lines = export(story, tmp_path, anthropic_config())
        assert "did not run on 1 chapter:" in lines[-1]

    def test_reaches_the_log_when_nobody_is_watching(
        self, tmp_path, monkeypatch, cache_root, caplog,
    ):
        """With no status callback the closing line still has to land somewhere, and must not be marked as already shown."""
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        with caplog.at_level(logging.INFO, logger="ficary.exporters"):
            exporters.export_html(
                make_story(2), output_dir=str(tmp_path),
                strip_notes=True, llm_config=anthropic_config(),
            )
        closing = [
            record for record in caplog.records
            if "did not run on 2 chapters" in record.getMessage()
        ]
        assert len(closing) == 1
        assert closing[0].levelno == logging.WARNING
        assert closing[0].ui_already_emitted is False


# ── Audiobooks ───────────────────────────────────────────────────


class TestAudiobookNoteCheck:
    """An audiobook render runs the same check before it starts reading. It had no stop rule at all: every chapter asked again, and the listener found out by hearing the notes."""

    def render(self, monkeypatch, story, config):
        monkeypatch.setattr(tts, "_check_ffmpeg", lambda *a, **k: None)
        monkeypatch.setattr(
            tts, "_generate_audiobook_inner", lambda **kwargs: Path("/tmp/x.m4b"),
        )
        return tts.generate_audiobook(
            story, "/tmp", strip_notes=True, attribution_backend="builtin",
            llm_an_config=config,
        )

    def audio_story(self, chapter_count: int):
        story = make_story(chapter_count)
        return SimpleNamespace(
            id="t", title="T", author="A", url="https://example.invalid",
            metadata={}, chapters=story.chapters,
        )

    def test_out_of_credit_asks_once(self, monkeypatch):
        requests = refuse_every_request(
            monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY,
        )
        self.render(monkeypatch, self.audio_story(6), anthropic_config())
        assert len(requests) == 1

    def test_says_how_it_went_before_the_render_starts(self, monkeypatch, caplog):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        with caplog.at_level(logging.INFO, logger="ficary.tts"):
            self.render(monkeypatch, self.audio_story(4), anthropic_config())
        warnings = [
            record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        stop_lines = [
            message for message in warnings
            if "skipping LLM for remaining chapters" in message
        ]
        assert len(stop_lines) == 1
        assert "Your credit balance is too low" in stop_lines[0]
        closing = [
            message for message in warnings
            if "did not run on 4 chapters" in message
        ]
        assert len(closing) == 1

    def test_three_timeouts_in_a_row_stop_it(self, monkeypatch):
        calls: list = []

        def llm(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMTimeout("timed out after 300s")

        monkeypatch.setattr(attribution, "_llm_call", llm)
        self.render(monkeypatch, self.audio_story(8), ollama_config())
        assert len(calls) == attribution.NOTE_CHECK_MAX_CONSECUTIVE_FAILURES

    def test_a_clean_run_is_tallied(self, monkeypatch, caplog):
        monkeypatch.setattr(
            attribution, "_llm_call", lambda **kwargs: all_false_reply(),
        )
        with caplog.at_level(logging.INFO, logger="ficary.tts"):
            self.render(monkeypatch, self.audio_story(3), anthropic_config())
        assert "LLM note check: 3 chapters checked, notes removed from 0." in caplog.text

    def test_direct_call_without_a_tally_still_never_raises(self, monkeypatch):
        def llm(**kwargs):
            raise attribution.LLMRejected(400, "anthropic", ANTHROPIC_OUT_OF_CREDIT_BODY)

        monkeypatch.setattr(attribution, "_llm_call", llm)
        text = "Para one.\n\nPara two.\n\nPara three."
        assert tts._llm_strip_an_paragraphs(text, anthropic_config()) == text


# ── On the command line ──────────────────────────────────────────


@contextlib.contextmanager
def fresh_command_line_logging():
    """Run the body with a root logger that has no handlers, which is what a real command-line run starts with.

    This has to happen inside the test body. pytest puts its own log handlers on the root logger at the start of each test's call phase, after fixtures have run, and the function under test leaves logging alone when it finds any.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


class TestCommandLineShowsEachLineOnce:
    """Status lines are printed by the status callback and mirrored to the log. On the command line both end up in the same terminal, so each line used to appear, and be read out, twice."""

    def test_a_console_handler_really_is_installed(self):
        """Guards the tests below: if nothing were installed, "printed once" would pass for the wrong reason."""
        with fresh_command_line_logging() as root:
            cli._configure_console_logging(verbose=False)
            assert len(root.handlers) == 1
            assert root.level == logging.INFO

    def test_a_status_line_is_printed_once(self, capsys):
        with fresh_command_line_logging():
            cli._configure_console_logging(verbose=False)
            exporters._emit(print, "  [llm-an] chapter 1: cache hit")
        shown = capsys.readouterr()
        assert shown.out.count("chapter 1: cache hit") == 1
        assert "cache hit" not in shown.err

    def test_the_closing_line_is_printed_once(
        self, capsys, tmp_path, monkeypatch, cache_root,
    ):
        refuse_every_request(monkeypatch, 400, ANTHROPIC_OUT_OF_CREDIT_BODY)
        with fresh_command_line_logging():
            cli._configure_console_logging(verbose=False)
            exporters.export_html(
                make_story(3), output_dir=str(tmp_path),
                strip_notes=True, llm_config=anthropic_config(), progress=print,
            )
        shown = capsys.readouterr()
        everything = shown.out + shown.err
        assert everything.count("did not run on 3 chapters") == 1
        assert everything.count("skipping LLM for remaining chapters") == 1

    def test_other_log_messages_still_reach_the_terminal(self, capsys):
        with fresh_command_line_logging():
            cli._configure_console_logging(verbose=False)
            logging.getLogger("ficary.somewhere").warning("something went wrong")
            logging.getLogger("ficary.somewhere").debug("fine detail")
        shown = capsys.readouterr()
        assert "something went wrong" in shown.err
        assert "fine detail" not in shown.err

    def test_verbose_shows_the_fine_detail(self, capsys):
        with fresh_command_line_logging():
            cli._configure_console_logging(verbose=True)
            logging.getLogger("ficary.somewhere").debug("fine detail")
        assert "fine detail" in capsys.readouterr().err

    def test_logging_someone_else_set_up_is_left_alone(self):
        with fresh_command_line_logging() as root:
            existing = logging.NullHandler()
            root.addHandler(existing)
            root.setLevel(logging.ERROR)
            cli._configure_console_logging(verbose=True)
            assert root.handlers == [existing]
            assert root.level == logging.ERROR


class TestLongErrorBodies:
    """The log line keeps only the start of an error body. What the error means has to be read from all of it, because a JSON body cut short no longer parses."""

    def long_out_of_credit_body(self) -> str:
        padding = "x" * 2000
        return json.dumps({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
                "extra": padding,
            },
        })

    def test_a_long_body_is_still_understood(self, monkeypatch):
        body = self.long_out_of_credit_body()
        assert len(body) > attribution._LLM_ERROR_DETAIL_MAX_CHARS
        refuse_every_request(monkeypatch, 400, body)
        with pytest.raises(attribution.LLMRejected) as raised:
            attribution._llm_call(
                provider="anthropic", model="m", api_key="k",
                endpoint="https://api.example.invalid/v1",
                system_prompt="", user_prompt="",
            )
        assert raised.value.will_repeat is True
        assert raised.value.provider_message.startswith("Your credit balance")

    def test_the_log_line_stays_short(self, monkeypatch):
        refuse_every_request(monkeypatch, 400, self.long_out_of_credit_body())
        with pytest.raises(attribution.LLMRejected) as raised:
            attribution._llm_call(
                provider="anthropic", model="m", api_key="k",
                endpoint="https://api.example.invalid/v1",
                system_prompt="", user_prompt="",
            )
        assert len(str(raised.value)) < 600
