"""Tests for the "Claude Code" AI provider: Ficary runs the Claude Code program installed on the computer, so a request is made on the Claude plan its user is signed in to and needs no API key.

Nothing here reaches Anthropic or the real program. Claude Code is played by ``claude_code_stand_in.py``, which answers with output captured from the real one (2.1.332, 2026-11-27) and writes down how it was started. Each test names one thing a person relies on: the request goes to the plan sign-in and to nothing else, the story's text never becomes part of a command, a failure is said in plain words and ends the book's check when asking again is pointless, and nothing is left running.

On Windows the stand-in is a real ``claude.exe`` (the launcher pip puts in front of every command it installs), because Ficary runs nothing else there. The Windows rules for finding the program are also tested on any system, by telling the finder which system to follow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from ficary import attribution, claude_code, cli, exporters
from ficary.models import Chapter, Story

STAND_IN = Path(__file__).resolve().parent / "claude_code_stand_in.py"
CAPTURED = Path(__file__).resolve().parent / "fixtures" / "claude_code"

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")

# Every way the environment can send a request somewhere other than the plan sign-in. The first six are the sources Claude Code ranks above a plan sign-in (code.claude.com/docs/en/authentication, read 2026-11-27); the next three redirect or re-label a request without any of them; the last two are a provider switch of a kind a later version might add, and the switch for bare mode.
AWAY_FROM_THE_PLAN = [
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_SOMETHING_NEW",
    "CLAUDE_CODE_SIMPLE",
]

# What a tool run by Claude Code 2.1.332 finds in its environment about the session that started it.
SESSION_VARIABLES = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_CHILD_NOTES_DIR",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
]

CHAPTER_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def captured(name: str) -> bytes:
    """One of the real program's outputs, as it printed it."""
    return (CAPTURED / name).read_bytes()


def captured_lines(name: str) -> list[dict]:
    return [json.loads(line) for line in captured(name).decode("utf-8").splitlines() if line.strip()]


def as_printed(lines: list[dict]) -> bytes:
    return "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")


def make_story(chapter_count: int) -> Story:
    """A story whose every chapter is long enough for the AI note check to run, and different from the others so each gets its own request."""
    story = Story(id=1, title="T", author="A", summary="", url="https://www.fanfiction.net/s/1")
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


def export(story, folder, config):
    """Run an HTML export with note stripping on, and return (path, status lines)."""
    lines: list[str] = []
    path = exporters.export_html(story, output_dir=str(folder), strip_notes=True, llm_config=config, progress=lines.append)
    return path, lines


@pytest.fixture(autouse=True)
def nothing_remembered(monkeypatch):
    """Start each test with no program's version or sign-in check remembered, as a new run of Ficary does."""
    monkeypatch.setattr(claude_code, "_versions_seen", {})
    monkeypatch.setattr(claude_code, "_routes_checked", set())
    monkeypatch.setattr(claude_code, "_unconfirmed_sign_in_logged", False)


@pytest.fixture(autouse=True)
def settings_of_its_own(monkeypatch):
    """Keep each test's saved settings in a file in its own data folder, on every system.

    Run from source on Windows, Ficary keeps its settings in the registry, which the data folder each test is given does not cover: on the first Windows run of this file, the model one test saved for Claude Code was the model a later test found in the settings window.
    """
    from ficary import portable
    from ficary import prefs as prefs_module

    def start(self):
        self.last_save_error = ""
        self._cfg = prefs_module._FileStore(portable.settings_file())

    monkeypatch.setattr(prefs_module.Prefs, "__init__", start)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Keep the note-check cache inside the test's own folder."""
    root = tmp_path / "llm_an"

    def cache_path(site_name, story_id):
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{site_name}_{story_id}.json"

    monkeypatch.setattr(exporters, "_llm_an_cache_path", cache_path)
    return root


def make_windows_program(folder: Path) -> Path:
    """A real ``claude.exe`` that runs the stand-in: the launcher pip puts in front of every command it installs, holding a script that passes everything on."""
    try:
        from distlib.scripts import ScriptMaker
    except ImportError:
        from pip._vendor.distlib.scripts import ScriptMaker

    maker = ScriptMaker(str(folder), str(folder), add_launchers=True)
    maker.clobber = True
    maker.variants = {""}
    maker.executable = sys.executable
    maker.script_template = "import runpy\nimport sys\nsys.argv[0] = {path!r}\nrunpy.run_path({path!r}, run_name='__main__')\n".format(path=str(STAND_IN))
    maker.make("claude = stand_in:main")
    return folder / "claude.exe"


class StandIn:
    """The stand-in program, placed in a folder of its own under the name ``claude``."""

    def __init__(self, folder: Path, monkeypatch):
        self.folder = folder
        self.notes_path = folder / "notes.jsonl"
        self._monkeypatch = monkeypatch
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            self.program = make_windows_program(folder)
        else:
            self.program = folder / "claude"
            self.program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n', encoding="utf-8")
            self.program.chmod(0o755)
        monkeypatch.setenv("STAND_IN_NOTES", str(self.notes_path))

    def behaves(self, behaviour: str) -> None:
        self._monkeypatch.setenv("STAND_IN_BEHAVIOUR", behaviour)

    def replies(self, reply: str) -> None:
        self._monkeypatch.setenv("STAND_IN_REPLY", reply)

    def looks_for(self, *words: str) -> None:
        self._monkeypatch.setenv("STAND_IN_LOOK_FOR", "\n".join(words))

    def notes(self, kind: str | None = None) -> list[dict]:
        """What the stand-in wrote down, one entry for each time it was started."""
        if not self.notes_path.exists():
            return []
        entries = [json.loads(line) for line in self.notes_path.read_text(encoding="utf-8").splitlines() if line]
        if kind is None:
            return entries
        return [entry for entry in entries if entry["kind"] == kind]

    def requests(self) -> list[dict]:
        return self.notes("request")

    def story_requests(self) -> list[dict]:
        """The requests that carried something other than the question asked before any story is sent."""
        return [request for request in self.requests() if request["input"] != claude_code._PROBE_QUESTION]

    def checks(self) -> list[dict]:
        """The requests that carried only the question asked before any story is sent."""
        return [request for request in self.requests() if request["input"] == claude_code._PROBE_QUESTION]

    def config(self, model: str = "haiku") -> dict:
        """AI settings that name this stand-in as the Claude Code program."""
        return {"provider": "claude-code", "model": model, "api_key": "", "endpoint": str(self.program)}


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    return StandIn(tmp_path / "claude code", monkeypatch)


def ask(stand_in, **changes) -> str:
    """One request through the stand-in, with everything a real one carries."""
    request = {
        "program": str(stand_in.program),
        "model": "haiku",
        "system_prompt": "Classify the paragraphs.",
        "user_prompt": '<paragraph n="1">Thanks for reading!</paragraph>',
        "timeout_s": 60,
    }
    request.update(changes)
    return claude_code.ask(**request)


def is_running(process: int) -> bool:
    """Whether the process with this number is still running."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        query_limited_information = 0x1000
        handle = kernel32.OpenProcess(query_limited_information, False, process)
        if not handle:
            return False
        code = wintypes.DWORD()
        answered = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        still_active = 259
        return bool(answered) and code.value == still_active
    try:
        os.kill(process, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Stopped but not yet collected by whatever adopted it: it shows as a zombie, which is no longer running.
    try:
        state = Path(f"/proc/{process}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        state = ""
    return state != "Z"


def stopped_within(process: int, seconds: float) -> bool:
    """Whether the process with this number has gone by the time ``seconds`` have passed."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_running(process):
            return True
        time.sleep(0.1)
    return False


def request_folders_left() -> list[str]:
    """Folders made for a request that are still there."""
    import tempfile

    return sorted(path.name for path in Path(tempfile.gettempdir()).glob("ficary-claude-code-*"))


@pytest.fixture
def own_temp_folder(tmp_path, monkeypatch):
    """Give this test a temporary folder of its own, so the request folders it leaves, if any, are its own."""
    import tempfile

    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


# ── The command Ficary runs ──────────────────────────────────────


class TestTheCommand:
    def test_is_one_question_with_no_tools_and_none_of_the_persons_own_settings(self):
        assert claude_code.command_line("/somewhere/claude", "haiku") == [
            "/somewhere/claude",
            "-p",
            "--safe-mode",
            "--setting-sources", "",
            "--no-session-persistence",
            "--tools", "",
            "--model", "haiku",
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt-file", claude_code.PROMPT_FILE_NAME,
        ]

    def test_never_asks_for_bare_mode(self):
        """Bare mode never reads a plan sign-in (code.claude.com/docs/en/headless), so it would turn every request into "not signed in" or onto an API key."""
        assert "--bare" not in claude_code.command_line("claude", "sonnet")

    def test_the_prompt_file_is_named_without_a_folder(self):
        """A full path would put the person's user name into the command."""
        assert Path(claude_code.PROMPT_FILE_NAME).name == claude_code.PROMPT_FILE_NAME
        assert " " not in claude_code.PROMPT_FILE_NAME

    @pytest.mark.parametrize("model", ["haiku", "sonnet", "opus", "fable", "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "opus[1m]"])
    def test_takes_the_model_names_people_use(self, model):
        assert model in claude_code.command_line("claude", model)

    @pytest.mark.parametrize("model", ["", "--bare", "-p", "haiku --bare", "haiku&calc", 'ha"iku', "haiku\nsonnet", "%PATH%", "llama 3"])
    def test_a_model_name_cannot_be_an_option_or_a_command(self, model):
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.command_line("claude", model)
        assert caught.value.will_repeat


# ── What the program is allowed to inherit ───────────────────────


class TestTheEnvironment:
    def test_everything_that_would_send_the_request_elsewhere_is_left_out(self):
        around = {name: "set" for name in AWAY_FROM_THE_PLAN}
        around["PATH"] = "/usr/bin"
        assert claude_code.child_environment(around) == {"PATH": "/usr/bin"}

    def test_the_plans_own_token_and_the_way_out_to_the_network_stay(self):
        around = {
            "CLAUDE_CODE_OAUTH_TOKEN": "plan token",
            "CLAUDE_CONFIG_DIR": "/home/someone/.claude",
            "HTTPS_PROXY": "http://proxy.example:3128",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/company.pem",
            "CLAUDE_CODE_CLIENT_CERT": "/etc/ssl/me.pem",
            "CLAUDE_CODE_GIT_BASH_PATH": r"C:\Program Files\Git\bin\bash.exe",
            "PATH": "/usr/bin",
        }
        assert claude_code.child_environment(around) == around

    def test_the_session_ficary_was_started_from_is_not_passed_on(self):
        """Run from inside a Claude Code session, the request went out labelled as that session's kind (sdk-py) and made an extra request for a session title."""
        around = {name: "set" for name in SESSION_VARIABLES}
        around["HOME"] = "/home/someone"
        assert claude_code.child_environment(around) == {"HOME": "/home/someone"}

    def test_names_are_matched_whatever_their_case(self):
        """Windows gives the names in capitals whatever case they were set in; elsewhere a name can be in any case."""
        assert claude_code.child_environment({"Anthropic_Api_Key": "k", "claude_code_use_bedrock": "1", "Path": "x"}) == {"Path": "x"}

    def test_uses_the_real_environment_when_given_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-this")
        monkeypatch.setenv("FICARY_TEST_KEPT", "yes")
        environment = claude_code.child_environment()
        assert "ANTHROPIC_API_KEY" not in environment
        assert environment["FICARY_TEST_KEPT"] == "yes"


# ── Reading what the program printed ─────────────────────────────


class TestReadingTheAnswer:
    def test_gives_back_the_models_words(self):
        answer = claude_code.read_answer(0, captured("answered.jsonl"), b"")
        assert answer.text == "ok"
        assert answer.models == ["claude-haiku-5-5"]
        assert (answer.input_tokens, answer.output_tokens) == (603, 4)

    def test_the_program_says_the_request_was_on_the_plan(self):
        assert claude_code.read_answer(0, captured("answered.jsonl"), b"").sign_in_confirmed

    def test_not_signed_in_is_said_in_the_programs_words_with_what_to_do(self):
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, captured("not_signed_in.jsonl"), b"")
        sentence = caught.value.explain()
        assert "Not logged in · Please run /login" in sentence
        assert "sign in" in sentence
        assert caught.value.will_repeat

    def test_a_model_the_account_cannot_use_ends_the_asking(self):
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, captured("unknown_model.jsonl"), captured("unknown_model.stderr"))
        assert "There's an issue with the selected model (no-such-model-xyz)" in caught.value.explain()
        assert caught.value.status == 404
        assert caught.value.will_repeat

    def test_an_older_claude_code_is_told_to_update(self):
        """The real program's words for an option it does not know, with the name changed to the one an older version would not know."""
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, b"", captured("unknown_option.stderr"))
        sentence = caught.value.explain()
        assert "unknown option '--safe-mode'" in sentence
        assert "claude update" in sentence
        assert caught.value.will_repeat

    def test_a_crash_is_quoted_and_may_be_tried_again(self):
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(139, b"Segmentation fault (core dumped)\n", b"")
        assert "Segmentation fault (core dumped)" in caught.value.explain()
        assert not caught.value.will_repeat

    def test_stopping_with_nothing_printed_still_says_something(self):
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, b"", b"")
        assert "exit status 1" in caught.value.explain()
        assert not caught.value.will_repeat

    def test_a_failure_status_counts_even_when_the_result_does_not_say_so(self):
        """The manual gives the exit status as the sign of a failed run; ``is_error`` is not in it."""
        with pytest.raises(claude_code.ClaudeCodeFailed):
            claude_code.read_answer(1, captured("answered.jsonl"), b"")

    def test_a_result_that_says_it_failed_is_a_failure_whatever_the_status(self):
        """Not seen from the real program, which stops with status 1 when its result says ``is_error``. Should a version stop with 0, the reason must still not be read as the model's words."""
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(0, captured("not_signed_in.jsonl"), b"")
        assert "Not logged in" in caught.value.explain()

    def test_a_failure_it_has_not_seen_before_is_left_to_the_three_in_a_row_rule(self):
        lines = captured_lines("not_signed_in.jsonl")
        lines[-1]["result"] = "API Error: Repeated 529 Overloaded errors"
        lines[-1]["api_error_status"] = 529
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, as_printed(lines), b"")
        assert not caught.value.will_repeat

    def test_an_answer_with_no_text_is_an_empty_reply_not_a_crash(self):
        lines = captured_lines("answered.jsonl")
        lines[-1]["result"] = None
        assert claude_code.read_answer(0, as_printed(lines), b"").text == ""

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
    def test_a_line_separator_in_the_answer_does_not_cut_it_in_two(self, separator):
        """These may stand as they are inside a JSON string, and Claude Code, which is written in JavaScript, leaves them so. Python's ``splitlines`` breaks a line at each of them, and the answer was then no answer at all."""
        lines = captured_lines("answered.jsonl")
        for line in lines:
            if line["type"] == "assistant":
                line["message"]["content"] = [{"type": "text", "text": f"first{separator}second"}]
            if line["type"] == "result":
                line["result"] = f"first{separator}second"
        printed = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")
        assert separator.encode("utf-8") in printed
        assert claude_code.read_answer(0, printed, b"").text == f"first{separator}second"

    def test_some_other_json_is_not_an_answer(self):
        """An object that is not Claude Code's result, holding the words a model uses for "no notes here", must not be taken for a checked chapter."""
        with pytest.raises(claude_code.ClaudeCodeFailed):
            claude_code.read_answer(0, b'{"result": "{}"}\n', b"")

    def test_an_answer_made_with_an_api_key_is_refused(self):
        """The first line is what the real program printed for a request with a key in its environment."""
        lines = captured_lines("answered.jsonl")
        lines[0] = captured_lines("api_key_in_use.init.jsonl")[0]
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(0, as_printed(lines), b"")
        sentence = caught.value.explain()
        assert "API key" in sentence
        assert "not used" in sentence
        assert caught.value.will_repeat

    @pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", "user", "project", "org", "temporary"])
    def test_every_name_the_program_has_for_a_key_is_refused(self, source):
        """The names are the program's own list (read from Claude Code 2.1.332), the last four being older ones."""
        lines = captured_lines("answered.jsonl")
        lines[0]["apiKeySource"] = source
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(0, as_printed(lines), b"")
        assert caught.value.will_repeat

    def test_an_answer_from_another_service_is_refused(self):
        lines = captured_lines("answered.jsonl")
        lines[0]["served_by_anthropic"] = False
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(0, as_printed(lines), b"")
        assert "another service" in caught.value.explain()
        assert caught.value.will_repeat

    def test_a_refusal_quotes_what_the_program_said(self):
        """The second of the two is a field the program calls internal. A refusal on a computer nobody can look at has to be readable from its log."""
        lines = captured_lines("answered.jsonl")
        lines[0] = captured_lines("api_key_in_use.init.jsonl")[0]
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(0, as_printed(lines), b"")
        sentence = caught.value.explain()
        assert 'apiKeySource "ANTHROPIC_API_KEY"' in sentence
        assert "served_by_anthropic false" in sentence

    def test_a_name_ficary_does_not_know_is_not_taken_for_a_key(self):
        """A later Claude Code may name the plan sign-in something new. Refusing whatever is not "none" would lock out everybody on a plan."""
        lines = captured_lines("answered.jsonl")
        lines[0]["apiKeySource"] = "something a later version calls it"
        answer = claude_code.read_answer(0, as_printed(lines), b"")
        assert answer.text == "ok"
        assert not answer.sign_in_confirmed

    def test_no_key_is_not_enough_to_vouch_for_an_answer(self):
        """The program leaves out whether a request went to Anthropic where it cannot tell."""
        lines = captured_lines("answered.jsonl")
        del lines[0]["served_by_anthropic"]
        answer = claude_code.read_answer(0, as_printed(lines), b"")
        assert answer.text == "ok"
        assert not answer.sign_in_confirmed

    def test_an_answer_that_does_not_say_how_it_was_signed_in_is_used_but_not_vouched_for(self):
        """A later version may drop the line. The request is then no worse than it was before the line was read; it is only not confirmed."""
        lines = [line for line in captured_lines("answered.jsonl") if line["type"] != "system"]
        answer = claude_code.read_answer(0, as_printed(lines), b"")
        assert answer.text == "ok"
        assert not answer.sign_in_confirmed

    def test_an_email_address_or_a_key_in_the_programs_words_is_not_repeated(self):
        """Its words go into status lines and the log, which people attach to bug reports."""
        lines = captured_lines("not_signed_in.jsonl")
        lines[-1]["result"] = "Access denied for reader@example.org on key sk-ant-api03-0123456789abcdefghij; contact your administrator"
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            claude_code.read_answer(1, as_printed(lines), b"")
        sentence = caught.value.explain()
        assert "reader@example.org" not in sentence
        assert "0123456789abcdefghij" not in sentence
        assert "Access denied" in sentence
        assert "contact your administrator" in sentence

    def test_every_failure_is_one_the_chapter_loop_knows(self):
        """A plain RuntimeError would be counted as an unreadable reply for one batch, and the next chapter asked again."""
        assert issubclass(claude_code.ClaudeCodeFailed, attribution.LLMRejected)
        assert issubclass(claude_code.ClaudeCodeUnavailable, attribution.LLMUnavailable)
        assert not issubclass(claude_code.ClaudeCodeUnavailable, attribution.LLMTimeout)


# ── Finding the program ──────────────────────────────────────────


@pytest.fixture
def nowhere(tmp_path, monkeypatch):
    """A computer with nothing on its search path and an empty home folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(claude_code, "_home_folder", lambda: home)
    return home


def place_program(folder: Path, name: str | None = None) -> Path:
    """An empty program file called ``claude`` (or ``name``) in ``folder``, as this system would run it."""
    folder.mkdir(parents=True, exist_ok=True)
    if name is None:
        name = "claude.exe" if sys.platform == "win32" else "claude"
    program = folder / name
    program.write_text("", encoding="utf-8")
    program.chmod(0o755)
    return program


class TestFindingTheProgram:
    def test_a_full_path_in_the_settings_is_used_as_it_is(self, nowhere, tmp_path):
        own_name = "my-claude.exe" if sys.platform == "win32" else "my-claude"
        program = place_program(tmp_path / "odd place", own_name)
        assert claude_code.find_program(str(program)) == str(program)

    def test_a_path_in_the_settings_that_is_not_there_finds_nothing(self, nowhere, tmp_path):
        place_program(nowhere / ".local" / "bin")
        assert claude_code.find_program(str(tmp_path / "gone" / "claude")) is None

    def test_blank_looks_along_the_search_path(self, nowhere, tmp_path, monkeypatch):
        program = place_program(tmp_path / "tools")
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "empty"), str(tmp_path / "tools")]))
        assert claude_code.find_program("") == str(program)

    def test_blank_and_the_plain_name_mean_the_same(self, nowhere, tmp_path, monkeypatch):
        program = place_program(tmp_path / "tools")
        monkeypatch.setenv("PATH", str(tmp_path / "tools"))
        assert claude_code.find_program("claude") == str(program)
        assert claude_code.find_program(None) == str(program)

    def test_falls_back_to_where_the_installer_puts_it(self, nowhere):
        """A program started from the desktop on macOS gets a short search path without the person's own folders on it."""
        program = place_program(nowhere / ".local" / "bin")
        assert claude_code.find_program("") == str(program)

    def test_the_search_path_comes_before_the_installers_folder(self, nowhere, tmp_path, monkeypatch):
        place_program(nowhere / ".local" / "bin")
        chosen = place_program(tmp_path / "tools")
        monkeypatch.setenv("PATH", str(tmp_path / "tools"))
        assert claude_code.find_program("") == str(chosen)

    def test_the_folder_ficary_was_started_in_is_not_searched(self, nowhere, tmp_path, monkeypatch):
        """A file called claude in a downloads folder must not be run because Ficary was started there."""
        place_program(tmp_path / "downloads")
        monkeypatch.chdir(tmp_path / "downloads")
        monkeypatch.setenv("PATH", os.pathsep.join(["", "."]))
        assert claude_code.find_program("") is None

    def test_a_path_from_here_is_made_whole(self, nowhere, tmp_path, monkeypatch):
        """The program is checked from one folder and asked from another, so a path that depends on the folder would name two different files."""
        program = place_program(tmp_path / "work" / "tools", "my-claude.exe" if sys.platform == "win32" else "my-claude")
        monkeypatch.chdir(tmp_path / "work")
        found = claude_code.find_program(os.path.join("tools", program.name))
        assert found == str(program)
        assert os.path.isabs(found)

    def test_a_web_address_left_from_another_provider_is_not_a_program(self, nowhere):
        """The command line reads the saved endpoint whichever provider it was saved for."""
        program = place_program(nowhere / ".local" / "bin")
        assert claude_code.find_program("http://localhost:11434") == str(program)

    def test_a_location_that_cannot_be_worked_out_finds_nothing(self, nowhere):
        """"~someone" for someone who is not a user here made Python raise an error of its own."""
        assert claude_code.find_program("~no-such-user-here/bin/claude") is None

    def test_windows_app_shortcuts_are_passed_over(self, nowhere, tmp_path, monkeypatch):
        """An older Claude Desktop can put a Claude.exe in WindowsApps, ahead of the real one on the search path (code.claude.com/docs/en/troubleshoot-install)."""
        place_program(tmp_path / "Microsoft" / "WindowsApps")
        real = place_program(tmp_path / "tools")
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "Microsoft" / "WindowsApps"), str(tmp_path / "tools")]))
        assert claude_code.find_program("") == str(real)

    def test_nothing_found_is_none(self, nowhere):
        assert claude_code.find_program("") is None


class TestFindingTheProgramOnWindows:
    """The Windows rules, followed on whatever system the tests run on."""

    def test_the_program_itself_is_looked_for_before_a_batch_file(self):
        names = claude_code.program_file_names("claude", "win32")
        assert names.index("claude.exe") < names.index("claude.cmd")
        assert claude_code.program_file_names("claude", "linux") == ["claude"]
        assert claude_code.program_file_names("claude.exe", "win32") == ["claude.exe"]

    def test_a_batch_file_is_never_the_program(self, nowhere, tmp_path, monkeypatch):
        """Windows gives a batch file's command to cmd.exe, which reads it again by its own rules and looks for the programs it names in the folder Ficary was started in."""
        place_program(tmp_path / "npm", "claude.cmd")
        place_program(tmp_path / "other", "claude.bat")
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "npm"), str(tmp_path / "other")]))
        assert claude_code.find_program("", platform="win32") is None

    def test_the_program_npm_keeps_beside_its_batch_file_is_used(self, nowhere, tmp_path, monkeypatch):
        place_program(tmp_path / "npm", "claude.cmd")
        real = place_program(tmp_path / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin", "claude.exe")
        monkeypatch.setenv("PATH", str(tmp_path / "npm"))
        assert claude_code.find_program("", platform="win32") == str(real)

    def test_npms_folder_is_looked_in_when_it_is_not_on_the_search_path(self, nowhere, tmp_path, monkeypatch):
        roaming = tmp_path / "AppData" / "Roaming"
        place_program(roaming / "npm", "claude.cmd")
        real = place_program(roaming / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin", "claude.exe")
        monkeypatch.setenv("APPDATA", str(roaming))
        assert claude_code.find_program("", platform="win32") == str(real)

    def test_the_program_wins_over_a_batch_file_in_the_same_folder(self, nowhere, tmp_path, monkeypatch):
        place_program(tmp_path / "tools", "claude.cmd")
        real = place_program(tmp_path / "tools", "claude.exe")
        monkeypatch.setenv("PATH", str(tmp_path / "tools"))
        assert claude_code.find_program("", platform="win32") == str(real)

    def test_a_batch_file_named_in_the_settings_is_not_run_either(self, nowhere, tmp_path):
        batch_file = place_program(tmp_path / "npm", "claude.cmd")
        assert claude_code.find_program(str(batch_file), platform="win32") is None

    def test_finding_only_a_batch_file_says_so_and_what_to_do(self, nowhere, tmp_path, monkeypatch):
        batch_file = place_program(tmp_path / "npm", "claude.cmd")
        monkeypatch.setenv("PATH", str(tmp_path / "npm"))
        sentence = claude_code._not_found_sentence("", platform="win32")
        assert "batch file" in sentence
        assert str(batch_file) in sentence
        assert "claude.exe" in sentence

    def test_a_program_named_without_its_ending_is_found(self, nowhere, tmp_path):
        real = place_program(tmp_path / "tools", "claude.exe")
        assert claude_code.find_program(str(tmp_path / "tools" / "claude"), platform="win32") == str(real)


# ── Running it ───────────────────────────────────────────────────


class TestRunningTheProgram:
    def test_gives_back_the_answer(self, stand_in):
        stand_in.replies('{"1": true}')
        assert ask(stand_in) == '{"1": true}'

    def test_the_story_goes_in_on_standard_input_and_nowhere_else(self, stand_in):
        story_text = '<paragraph n="1">“Don’t,” she said… 物語 naïve café</paragraph>'
        stand_in.looks_for("物語")
        ask(stand_in, user_prompt=story_text)
        request = stand_in.story_requests()[0]
        assert request["input"] == story_text
        assert request["seen"] == {"物語": ["input"]}
        assert stand_in.checks()[0]["seen"] == {"物語": []}

    def test_the_instructions_go_in_a_file_read_whole(self, stand_in):
        instructions = attribution._AN_SYSTEM_PROMPT
        assert "…" in instructions
        ask(stand_in, system_prompt=instructions)
        request = stand_in.story_requests()[0]
        assert request["files"] == {claude_code.PROMPT_FILE_NAME: instructions}

    def test_runs_the_command_the_command_tests_pin(self, stand_in):
        ask(stand_in, model="sonnet")
        for request in stand_in.requests():
            assert request["arguments"] == claude_code.command_line("claude", "sonnet")[1:]

    def test_runs_in_an_empty_folder_of_its_own_and_takes_it_away(self, stand_in):
        ask(stand_in)
        folder = Path(stand_in.requests()[0]["folder"])
        assert folder.resolve() != Path.cwd().resolve()
        assert not folder.exists()

    def test_is_asked_what_it_is_in_an_empty_folder_too(self, stand_in, tmp_path, monkeypatch):
        """Asked from the folder Ficary was started in, a program can pick up what lies there before it has said what it is."""
        (tmp_path / "started here").mkdir()
        (tmp_path / "started here" / "something.txt").write_text("left lying about", encoding="utf-8")
        monkeypatch.chdir(tmp_path / "started here")
        ask(stand_in)
        version_run = stand_in.notes("version")[0]
        assert Path(version_run["folder"]).resolve() != Path.cwd().resolve()
        assert version_run["files"] == {}

    def test_an_api_key_never_reaches_the_program(self, stand_in, monkeypatch):
        """With a key in the environment Claude Code always bills the key, whoever is signed in (code.claude.com/docs/en/authentication). A key saved for another provider is still in the settings the command line reads."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-in-the-environment")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-in-the-environment")
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-api-key: header-in-the-environment")
        stand_in.looks_for("sk-ant-in-the-environment", "bearer-in-the-environment", "header-in-the-environment", "sk-saved-for-another-provider")
        attribution._llm_call(
            provider="claude-code", model="haiku", api_key="sk-saved-for-another-provider",
            endpoint=str(stand_in.program), system_prompt="Classify.", user_prompt="A paragraph.",
        )
        request = stand_in.requests()[0]
        assert request["seen"] == {
            "sk-ant-in-the-environment": [], "bearer-in-the-environment": [],
            "header-in-the-environment": [], "sk-saved-for-another-provider": [],
        }
        for name in AWAY_FROM_THE_PLAN:
            assert name not in request["environment_names"]

    def test_a_long_chapter_goes_through_whole(self, stand_in):
        """Two hundred paragraphs is the most one request carries; the pipe must not fill and stall."""
        paragraphs = [f'<paragraph n="{number}">' + ("word " * 120) + "</paragraph>" for number in range(1, 201)]
        chapter = "\n\n".join(paragraphs)
        reply = ask(stand_in, user_prompt=chapter)
        received = stand_in.story_requests()[0]["input"]
        assert len(received) == len(chapter)
        assert hashlib.sha256(received.encode("utf-8")).hexdigest() == hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        assert len(json.loads(reply)) == 200

    def test_half_a_character_from_a_bad_page_does_not_stop_the_request(self, stand_in):
        """One half of a character pair cannot be written as UTF-8, and used to be an error of Python's own."""
        ask(stand_in, user_prompt="A paragraph with half a character \ud83d in it.")
        assert "A paragraph with half a character" in stand_in.story_requests()[0]["input"]

    def test_gives_up_at_the_time_limit_and_leaves_nothing_running(self, stand_in, own_temp_folder):
        stand_in.behaves("never-answers")
        started = time.monotonic()
        with pytest.raises(attribution.LLMTimeout):
            ask(stand_in, timeout_s=3)
        assert time.monotonic() - started < 30
        request = stand_in.requests()[0]
        for process in (request["process"], request["started"]):
            assert stopped_within(process, seconds=10), f"process {process} is still running"
        assert request_folders_left() == []

    def test_the_time_limit_covers_a_program_that_never_reads_a_long_chapter(self, stand_in, own_temp_folder):
        """Writing two megabytes to a program that is not reading fills the pipe. Written from the waiting thread, that blocked on Windows with no limit at all."""
        ask(stand_in)
        stand_in.behaves("never-reads")
        chapter = "word " * 400_000
        started = time.monotonic()
        with pytest.raises(attribution.LLMTimeout):
            ask(stand_in, user_prompt=chapter, timeout_s=3)
        assert time.monotonic() - started < 30
        assert stopped_within(stand_in.requests()[-1]["process"], seconds=10)
        assert request_folders_left() == []

    def test_what_a_program_started_is_stopped_even_after_the_program_has_gone(self, stand_in, own_temp_folder):
        """It holds the output open, so the read never ends; and with the program gone, it is no longer found under the program's number."""
        stand_in.behaves("leaves-something-running")
        with pytest.raises(attribution.LLMTimeout):
            ask(stand_in, timeout_s=3)
        assert stopped_within(stand_in.requests()[0]["started"], seconds=10)
        assert request_folders_left() == []

    def test_a_program_that_prints_far_too_much_is_stopped(self, stand_in, own_temp_folder, monkeypatch):
        """Everything a program prints is held in memory until it ends, so one that will not stop printing has to be stopped."""
        monkeypatch.setattr(claude_code, "_OUTPUT_LIMIT_BYTES", 1024 * 1024)
        stand_in.behaves("prints-far-too-much")
        started = time.monotonic()
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in, timeout_s=20)
        assert time.monotonic() - started < 15
        assert "printed more than an answer can hold" in caught.value.explain()
        assert stopped_within(stand_in.requests()[0]["process"], seconds=10)
        assert request_folders_left() == []

    def test_ctrl_c_stops_the_program_too(self, stand_in, own_temp_folder, monkeypatch):
        """The program is in a session of its own, so a terminal's Ctrl+C does not reach it. Ctrl+C is played here by raising what Python raises for it, from inside the wait: sending this process the real signal ended the whole test run when an earlier test had left the signal at its default."""
        stand_in.behaves("never-answers")

        real_wait = claude_code._wait

        def interrupted(process, readers, deadline, too_much):
            if "-p" not in process.args:
                # Being asked what it is, before the request.
                return real_wait(process, readers, deadline, too_much)
            waited = time.monotonic() + 20
            while not stand_in.requests() and time.monotonic() < waited:
                time.sleep(0.05)
            raise KeyboardInterrupt

        monkeypatch.setattr(claude_code, "_wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            ask(stand_in, timeout_s=60)
        request = stand_in.requests()[0]
        for process in (request["process"], request["started"]):
            assert stopped_within(process, seconds=10), f"process {process} is still running"
        assert request_folders_left() == []

    def test_an_answer_holding_a_line_separator_comes_back_whole(self, stand_in):
        stand_in.replies("first\u2028second")
        assert ask(stand_in) == "first\u2028second"

    def test_a_reader_that_will_not_start_is_reported_as_itself_and_the_program_stopped(self, stand_in, own_temp_folder, monkeypatch):
        """A thread can fail to start, on a computer out of them. Stopping the program then waited for the thread that never started, which is an error of its own, and that error took the place of the first."""
        import subprocess
        import threading

        ask(stand_in)
        started = []
        real_popen = subprocess.Popen

        class Recording(real_popen):
            def __init__(self, *arguments, **options):
                super().__init__(*arguments, **options)
                started.append(self)

        readers = []
        real_thread = threading.Thread

        class Refusing(real_thread):
            def start(self):
                if self._target is claude_code._collect:
                    readers.append(self)
                    if len(readers) == 2:
                        raise RuntimeError("can't start new thread")
                return super().start()

        monkeypatch.setattr(subprocess, "Popen", Recording)
        monkeypatch.setattr(threading, "Thread", Refusing)
        stand_in.behaves("never-answers")
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in)
        assert "can't start new thread" in caught.value.explain()
        assert "join" not in caught.value.explain()
        request = [process for process in started if "-p" in process.args]
        assert len(request) == 1
        assert request[0].poll() is not None
        assert request_folders_left() == []

    @pytest.mark.skipif(sys.platform == "win32", reason="on Windows whatever the program starts is in Ficary's job, which it cannot leave, and is stopped with it")
    def test_stopping_does_not_wait_for_something_that_got_away(self, stand_in, own_temp_folder, monkeypatch):
        """The stand-in starts a program in a session of its own, out of reach of the stop, and that program holds the output open for 40 seconds. Closing the output from Ficary waited for a read that ends only when that program does."""
        monkeypatch.setattr(claude_code, "_STOP_WAIT_S", 1)
        ask(stand_in)
        stand_in.behaves("something-gets-away")
        try:
            started = time.monotonic()
            with pytest.raises(attribution.LLMTimeout):
                ask(stand_in, timeout_s=2)
            assert time.monotonic() - started < 20
            assert request_folders_left() == []
        finally:
            for request in stand_in.requests():
                if request.get("started"):
                    try:
                        os.kill(request["started"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_a_different_program_called_claude_is_not_given_the_story(self, stand_in):
        stand_in.behaves("another-program")
        with pytest.raises(claude_code.ClaudeCodeUnavailable) as caught:
            ask(stand_in)
        assert "is not Claude Code" in str(caught.value)
        assert stand_in.requests() == []

    def test_a_program_that_changes_is_asked_what_it_is_again(self, stand_in):
        ask(stand_in)
        ask(stand_in)
        assert len(stand_in.notes("version")) == 1
        changed = stand_in.program.read_bytes() + (b"\n" if sys.platform != "win32" else b"")
        stand_in.program.write_bytes(changed)
        later = time.time() + 5
        os.utime(stand_in.program, (later, later))
        ask(stand_in)
        assert len(stand_in.notes("version")) == 2

    def test_a_program_that_is_not_there_says_how_to_get_it(self, nowhere, tmp_path):
        with pytest.raises(claude_code.ClaudeCodeUnavailable) as caught:
            claude_code.ask(program="", model="haiku", system_prompt="s", user_prompt="u", timeout_s=5)
        sentence = str(caught.value)
        assert "was not found" in sentence
        assert "code.claude.com/docs/en/setup" in sentence
        assert "sign in" in sentence

    def test_a_named_program_that_is_not_there_names_it(self, nowhere, tmp_path):
        missing = str(tmp_path / "gone" / "claude")
        with pytest.raises(claude_code.ClaudeCodeUnavailable) as caught:
            claude_code.ask(program=missing, model="haiku", system_prompt="s", user_prompt="u", timeout_s=5)
        assert missing in str(caught.value)

    def test_no_room_for_the_requests_folder_is_said_once_and_plainly(self, stand_in, monkeypatch):
        """A full or locked temporary folder is the same for every chapter, and used to leave as an error of Python's own."""
        import tempfile

        def no_room(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", no_room)
        with pytest.raises(claude_code.ClaudeCodeUnavailable) as caught:
            ask(stand_in)
        assert "No space left on device" in str(caught.value)

    def test_a_fault_of_ficarys_own_still_leaves_as_a_failure_the_chapter_loop_knows(self, stand_in, monkeypatch):
        def broken(*args, **kwargs):
            raise KeyError("something nobody thought of")

        monkeypatch.setattr(claude_code, "read_answer", broken)
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in)
        assert "KeyError" in caught.value.explain()
        assert not caught.value.will_repeat

    def test_the_log_says_which_program_answered_how_fast_and_with_what(self, stand_in, caplog):
        """The first run on a Windows computer can only be read from its log."""
        caplog.set_level(logging.INFO, logger="ficary.claude_code")
        ask(stand_in)
        said = " ".join(record.getMessage() for record in caplog.records)
        assert str(stand_in.program) in said
        assert "2.1.332" in said
        assert "claude-haiku-5-5" in said
        assert "603 tokens in" in said
        assert 'plan sign-in confirmed: apiKeySource "none", served_by_anthropic true' in said

    def test_the_log_does_not_hold_the_story(self, stand_in, caplog):
        caplog.set_level(logging.DEBUG, logger="ficary.claude_code")
        ask(stand_in, user_prompt="The lighthouse keeper counted seventeen gulls.")
        assert "seventeen gulls" not in " ".join(record.getMessage() for record in caplog.records)

    def test_a_failure_with_private_words_keeps_them_out_of_the_log(self, stand_in, caplog):
        """The stand-in's failure names a person and holds a key, in its result and on its error output, as a company's gateway might."""
        caplog.set_level(logging.DEBUG)
        stand_in.behaves("fails-with-private-words")
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in)
        logged = " ".join(record.getMessage() for record in caplog.records)
        for private in ("reader@example.org", "0123456789abcdefghij"):
            assert private not in logged
            assert private not in caught.value.explain()
        assert "Access denied" in logged


class TestNothingIsSentBeforeTheCheck:
    """Claude Code says how a request was signed in only with its answer. So before the first story goes to it, it is asked a question with no story in it."""

    def test_no_story_reaches_a_claude_code_that_would_use_an_api_key(self, stand_in):
        stand_in.behaves("uses-an-api-key")
        stand_in.looks_for("lighthouse keeper")
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in, user_prompt="The lighthouse keeper counted seventeen gulls.")
        assert "API key" in caught.value.explain()
        assert stand_in.story_requests() == []
        assert [request["seen"] for request in stand_in.requests()] == [{"lighthouse keeper": []}]

    def test_no_story_reaches_one_that_sends_its_requests_elsewhere(self, stand_in):
        stand_in.behaves("uses-another-service")
        with pytest.raises(claude_code.ClaudeCodeFailed):
            ask(stand_in, user_prompt="The lighthouse keeper counted seventeen gulls.")
        assert stand_in.story_requests() == []

    def test_nobody_signed_in_is_found_before_any_story_is_sent(self, stand_in):
        stand_in.behaves("not-signed-in")
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in)
        assert "Not logged in" in caught.value.explain()
        assert stand_in.story_requests() == []

    def test_the_check_comes_first_and_once_for_each_program_and_model(self, stand_in):
        ask(stand_in)
        ask(stand_in)
        ask(stand_in, model="sonnet")
        assert stand_in.requests()[0]["input"] == claude_code._PROBE_QUESTION
        assert len(stand_in.checks()) == 2
        assert len(stand_in.story_requests()) == 3

    def test_a_program_that_changes_is_checked_again(self, stand_in):
        ask(stand_in)
        stand_in.program.write_bytes(stand_in.program.read_bytes() + (b"\n" if sys.platform != "win32" else b""))
        later = time.time() + 5
        os.utime(stand_in.program, (later, later))
        ask(stand_in)
        assert len(stand_in.checks()) == 2

    def test_a_check_that_failed_is_made_again_next_time(self, stand_in):
        stand_in.behaves("prints-nonsense")
        with pytest.raises(claude_code.ClaudeCodeFailed):
            ask(stand_in)
        stand_in.behaves("answers")
        ask(stand_in)
        assert len(stand_in.checks()) == 2
        assert len(stand_in.story_requests()) == 1

    def test_the_test_button_stands_in_for_the_check(self, stand_in):
        stand_in.replies("ready")
        assert attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku").ok
        ask(stand_in)
        assert len(stand_in.checks()) == 1
        assert len(stand_in.story_requests()) == 1


class TestWhichSignInAnswered:
    def test_an_answer_that_came_through_an_api_key_is_refused(self, stand_in):
        stand_in.behaves("uses-an-api-key")
        with pytest.raises(claude_code.ClaudeCodeFailed) as caught:
            ask(stand_in)
        assert "API key" in caught.value.explain()

    def test_an_answer_from_another_service_is_refused(self, stand_in):
        stand_in.behaves("uses-another-service")
        with pytest.raises(claude_code.ClaudeCodeFailed):
            ask(stand_in)

    def test_a_book_stops_asking_after_one_such_answer(self, stand_in, tmp_path, cache_root):
        """Each further chapter would be one more request somebody pays for by the token."""
        stand_in.behaves("uses-an-api-key")
        _path, lines = export(make_story(5), tmp_path / "out", stand_in.config())
        assert len(stand_in.requests()) == 1
        assert any("not with a Claude plan" in line for line in lines), lines

    def test_a_version_that_does_not_say_is_used_and_the_log_says_it_could_not_be_confirmed(self, stand_in, caplog, monkeypatch):
        monkeypatch.setattr(claude_code, "_unconfirmed_sign_in_logged", False)
        caplog.set_level(logging.INFO, logger="ficary.claude_code")
        stand_in.behaves("does-not-say-how-it-signed-in")
        stand_in.replies('{"1": false}')
        assert ask(stand_in) == '{"1": false}'
        assert ask(stand_in) == '{"1": false}'
        warnings = [record.getMessage() for record in caplog.records if "could not confirm" in record.getMessage()]
        assert len(warnings) == 1


# ── The rest of Ficary knows the provider ────────────────────────


class TestEveryProviderIsKnownEverywhere:
    """A provider missing from one of these places is quietly treated as another one: its request sent to an OpenAI address, its settings saved with a warning about a key it does not use."""

    def test_the_command_line_offers_the_same_providers(self):
        parser = cli._build_parser()
        offered = next(action.choices for action in parser._actions if "--llm-provider" in action.option_strings)
        assert list(offered) == list(attribution.LLM_PROVIDERS)

    def test_claude_code_is_one_of_them(self):
        assert "claude-code" in attribution.LLM_PROVIDERS

    @pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic", "claude-code", "openai-compatible"])
    def test_each_is_supported(self, provider):
        assert provider in attribution.LLM_PROVIDERS
        assert attribution._llm_provider_supported(provider)

    def test_an_unknown_provider_is_not(self):
        assert not attribution._llm_provider_supported("claude")

    def test_a_blank_program_setting_means_find_it(self):
        assert attribution._llm_normalize_endpoint("claude-code", "") == "claude"
        assert attribution._llm_normalize_endpoint("claude-code", None) == "claude"

    def test_it_is_asked_for_a_whole_chapter_at_once_and_trusted_in_the_middle(self):
        assert attribution._an_batch_size_for_provider("claude-code") == attribution._AN_BATCH_SIZE_CLOUD
        assert attribution._chunk_chars_for_provider("claude-code") == attribution._LLM_CHUNK_CHARS_CLOUD
        assert not attribution.should_constrain_an_to_boundaries("claude-code")

    def test_a_request_goes_to_the_program_and_not_to_a_web_address(self, stand_in, monkeypatch):
        def no_web_request(*args, **kwargs):
            raise AssertionError("a Claude Code request was sent to a web address")

        monkeypatch.setattr("urllib.request.urlopen", no_web_request)
        stand_in.replies('{"1": false}')
        reply = attribution._llm_call(
            provider="claude-code", model="haiku", api_key=None, endpoint=str(stand_in.program),
            system_prompt="Classify.", user_prompt='<paragraph n="1">Rain.</paragraph>',
        )
        assert reply == '{"1": false}'

    def test_speaker_detection_runs_with_a_blank_program_setting(self, stand_in, monkeypatch):
        from ficary.tts import Segment

        monkeypatch.setenv("PATH", str(stand_in.folder))
        stand_in.replies('{"1": {"speaker": "Harry Potter", "emotion": "neutral"}}')
        segments = [Segment("Chapter one."), Segment('"Hello there,"')]
        labelled = attribution._refine_with_llm(
            segments, 'Chapter one. "Hello there," said the boy.',
            provider="claude-code", model="haiku", api_key="", endpoint="",
            character_list=["Harry Potter"],
        )
        assert labelled[1].speaker == "Harry Potter"

    def test_the_time_limit_from_the_settings_reaches_the_program(self, stand_in, monkeypatch):
        seen = {}
        real_ask = claude_code.ask

        def watching(**request):
            seen.update(request)
            return real_ask(**request)

        monkeypatch.setattr(claude_code, "ask", watching)
        attribution._llm_call(
            provider="claude-code", model="haiku", api_key=None, endpoint=str(stand_in.program),
            system_prompt="s", user_prompt="u", request_timeout_s=77,
        )
        assert seen["timeout_s"] == 77


class TestTheNoteCheckThroughClaudeCode:
    def test_checks_every_chapter_and_takes_out_what_the_model_marks(self, stand_in, tmp_path, cache_root):
        stand_in.replies('{"1": true, "2": false, "3": false, "4": false}')
        path, lines = export(make_story(3), tmp_path / "out", stand_in.config())
        assert len(stand_in.story_requests()) == 3
        assert len(stand_in.checks()) == 1
        book = Path(path).read_text(encoding="utf-8")
        assert "alpha morning" not in book
        assert "alpha river" in book
        assert any("3 chapters checked" in line for line in lines), lines

    def test_not_signed_in_is_asked_once_and_the_book_is_still_written(self, stand_in, tmp_path, cache_root):
        stand_in.behaves("not-signed-in")
        path, lines = export(make_story(5), tmp_path / "out", stand_in.config())
        assert len(stand_in.requests()) == 1
        book = Path(path).read_text(encoding="utf-8")
        for word in CHAPTER_WORDS[:5]:
            assert f"The {word} morning was bright and clear." in book
        stop_lines = [line for line in lines if "skipping the AI check for the remaining chapters" in line]
        assert len(stop_lines) == 1
        assert 'Claude Code could not answer: "Not logged in · Please run /login"' in stop_lines[0]
        assert "HTTP" not in stop_lines[0]

    def test_not_signed_in_is_never_reported_or_remembered_as_clean(self, stand_in, tmp_path, cache_root):
        stand_in.behaves("not-signed-in")
        _path, lines = export(make_story(4), tmp_path / "out", stand_in.config())
        assert [line for line in lines if "no A/N paragraphs found" in line] == []
        assert list(cache_root.glob("*.json")) == [] or all(json.loads(saved.read_text(encoding="utf-8")) == {} for saved in cache_root.glob("*.json"))
        closing = [line for line in lines if "AI note check did not run" in line]
        assert len(closing) == 1
        assert "4 chapters" in closing[0]
        assert "download the story again" in closing[0]

    def test_a_missing_program_is_asked_for_once_and_said_plainly(self, nowhere, tmp_path, cache_root):
        config = {"provider": "claude-code", "model": "haiku", "api_key": "", "endpoint": ""}
        _path, lines = export(make_story(4), tmp_path / "out", config)
        stop_lines = [line for line in lines if "skipping the AI check for the remaining chapters" in line]
        assert len(stop_lines) == 1
        assert "Claude Code was not found" in stop_lines[0]
        assert "endpoint unreachable" not in stop_lines[0]

    def test_an_older_claude_code_is_asked_once(self, stand_in, tmp_path, cache_root):
        stand_in.behaves("too-old")
        _path, lines = export(make_story(4), tmp_path / "out", stand_in.config())
        assert len(stand_in.requests()) == 1
        assert any("claude update" in line for line in lines), lines

    def test_a_crash_is_tried_again_on_the_next_chapter_then_given_up_on(self, stand_in, tmp_path, cache_root):
        stand_in.behaves("prints-nonsense")
        _path, lines = export(make_story(6), tmp_path / "out", stand_in.config())
        assert len(stand_in.requests()) == attribution.NOTE_CHECK_MAX_CONSECUTIVE_FAILURES

    def test_an_empty_answer_is_not_a_checked_chapter(self, stand_in, tmp_path, cache_root):
        stand_in.behaves("answers-nothing")
        _path, lines = export(make_story(2), tmp_path / "out", stand_in.config())
        assert [line for line in lines if "no A/N paragraphs found" in line] == []
        assert any("AI note check did not run" in line for line in lines), lines


# ── The Test button ──────────────────────────────────────────────


class TestTheTestButton:
    def test_asks_one_real_question_with_the_command_a_chapter_gets(self, stand_in):
        """The version and the sign-in can both look fine while a request fails: an older build that does not know an option, or a sign-in that comes from the settings a request leaves out."""
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="sonnet")
        assert result.ok
        assert stand_in.requests()[0]["arguments"] == claude_code.command_line("claude", "sonnet")[1:]

    def test_says_what_it_found_and_that_the_model_answered_on_the_plan(self, stand_in):
        stand_in.replies("ready")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert "Claude Code 2.1.332" in result.detail
        assert str(stand_in.program) in result.detail
        assert "Claude plan (team)" in result.detail
        assert "answered" in result.detail
        assert "on the plan sign-in, with no API key" in result.detail

    def test_a_blank_model_asks_the_usual_one(self, stand_in):
        attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program))
        assert claude_code.DEFAULT_MODEL in stand_in.requests()[0]["arguments"]

    def test_who_is_signed_in_stays_out_of_the_log(self, stand_in, caplog):
        """The log is what people attach to a bug report."""
        caplog.set_level(logging.DEBUG)
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        logged = " ".join(record.getMessage() for record in caplog.records)
        for private in ("reader@example.org", "Example Reading Circle", "00000000-0000-4000-8000-00000000000a"):
            assert private not in logged
        assert "reader@example.org" not in result.detail
        assert "00000000-0000-4000-8000-00000000000a" not in result.detail

    def test_not_signed_in_fails_and_says_what_to_do(self, stand_in):
        stand_in.behaves("not-signed-in")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert not result.ok
        assert "Not logged in" in result.detail
        assert "sign in" in result.detail

    def test_a_missing_program_fails_and_says_how_to_get_it(self, nowhere):
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint="", model="haiku")
        assert not result.ok
        assert "was not found" in result.detail
        assert "Endpoint is empty" not in result.detail

    def test_an_older_claude_code_fails_the_test_though_it_is_installed_and_signed_in(self, stand_in):
        stand_in.behaves("too-old")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert not result.ok
        assert "claude update" in result.detail

    def test_a_different_program_fails_without_being_asked_anything(self, stand_in):
        stand_in.behaves("another-program")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert not result.ok
        assert stand_in.requests() == []

    def test_an_empty_answer_fails_the_test(self, stand_in):
        stand_in.behaves("answers-nothing")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert not result.ok
        assert "empty" in result.detail

    def test_an_answer_through_an_api_key_fails_the_test(self, stand_in):
        stand_in.behaves("uses-an-api-key")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert not result.ok
        assert "API key" in result.detail

    def test_a_version_that_does_not_say_how_it_signed_in_passes_and_says_so(self, stand_in):
        stand_in.behaves("does-not-say-how-it-signed-in")
        stand_in.replies("ready")
        result = attribution.probe_llm_endpoint(provider="claude-code", endpoint=str(stand_in.program), model="haiku")
        assert result.ok
        assert "could not be confirmed" in result.detail


# ── The command line ─────────────────────────────────────────────


def parsed(*arguments: str):
    return cli._build_parser().parse_args([*arguments, "https://example.org/s/1"])


class TestTheCommandLine:
    def test_takes_claude_code_as_the_provider(self):
        assert parsed("--llm-provider", "claude-code", "--llm-model", "haiku").llm_provider == "claude-code"

    def test_builds_note_check_settings_without_a_key(self, monkeypatch):
        """The saved key is whichever provider's the settings window last saved, and the environment's is Anthropic's."""
        from ficary import prefs as prefs_module

        prefs_module.Prefs().set(prefs_module.KEY_LLM_API_KEY, "sk-saved-for-another-provider")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-for-the-anthropic-provider")
        config = cli._llm_strip_notes_config(parsed("--strip-notes", "--llm-strip-notes", "--llm-provider", "claude-code", "--llm-model", "haiku"))
        assert config["provider"] == "claude-code"
        assert config["model"] == "haiku"
        assert config["api_key"] == ""

    def test_builds_speaker_detection_settings_without_a_key(self):
        from ficary import prefs as prefs_module

        prefs_module.Prefs().set(prefs_module.KEY_LLM_API_KEY, "sk-saved-for-another-provider")
        config = cli._llm_config_from_args(parsed("--attribution", "llm", "--llm-provider", "claude-code", "--llm-model", "haiku"))
        assert config["provider"] == "claude-code"
        assert config["api_key"] == ""

    @pytest.mark.parametrize("build, switches", [
        (cli._llm_strip_notes_config, ("--strip-notes", "--llm-strip-notes")),
        (cli._llm_config_from_args, ("--attribution", "llm")),
    ])
    def test_another_providers_saved_model_and_address_are_not_used(self, build, switches):
        """The saved model and endpoint belong to the provider the settings window last saved. Claude Code was handed "llama3.1:8b" and an Ollama address as its program."""
        from ficary import prefs as prefs_module

        saved = prefs_module.Prefs()
        saved.set(prefs_module.KEY_LLM_PROVIDER, "ollama")
        saved.set(prefs_module.KEY_LLM_MODEL, "llama3.1:8b")
        saved.set(prefs_module.KEY_LLM_ENDPOINT, "http://localhost:11434")
        config = build(parsed(*switches, "--llm-provider", "claude-code"))
        assert config["model"] == claude_code.DEFAULT_MODEL
        assert config["endpoint"] == ""

    @pytest.mark.parametrize("build, switches", [
        (cli._llm_strip_notes_config, ("--strip-notes", "--llm-strip-notes")),
        (cli._llm_config_from_args, ("--attribution", "llm")),
    ])
    def test_what_the_settings_window_keeps_for_claude_code_is_used(self, build, switches):
        from ficary import prefs as prefs_module

        saved = prefs_module.Prefs()
        saved.set(prefs_module.KEY_LLM_PROVIDER, "ollama")
        saved.set(prefs_module.KEY_LLM_MODEL, "llama3.1:8b")
        model_key, _key_key, endpoint_key = prefs_module.llm_provider_pref_keys("claude-code")
        saved.set(model_key, "opus")
        saved.set(endpoint_key, "/opt/claude/claude")
        config = build(parsed(*switches, "--llm-provider", "claude-code"))
        assert config["model"] == "opus"
        assert config["endpoint"] == "/opt/claude/claude"

    def test_claude_code_as_the_saved_provider_uses_its_saved_settings(self):
        from ficary import prefs as prefs_module

        saved = prefs_module.Prefs()
        saved.set(prefs_module.KEY_LLM_PROVIDER, "claude-code")
        saved.set(prefs_module.KEY_LLM_MODEL, "haiku")
        saved.set(prefs_module.KEY_LLM_ENDPOINT, "/opt/claude/claude")
        config = cli._llm_strip_notes_config(parsed("--strip-notes", "--llm-strip-notes"))
        assert config["provider"] == "claude-code"
        assert config["model"] == "haiku"
        assert config["endpoint"] == "/opt/claude/claude"

    def test_what_is_typed_on_the_command_line_wins(self):
        config = cli._llm_strip_notes_config(parsed(
            "--strip-notes", "--llm-strip-notes", "--llm-provider", "claude-code", "--llm-model", "opus", "--llm-endpoint", "/somewhere/claude",
        ))
        assert config["model"] == "opus"
        assert config["endpoint"] == "/somewhere/claude"


# ── The AI settings window ───────────────────────────────────────


def choose_provider(dialog, provider: str) -> None:
    """Pick ``provider`` in the dialog the way a person does, so its handler runs."""
    import wx

    dialog.provider_ctrl.SetSelection(dialog._PROVIDER_KEYS.index(provider))
    event = wx.CommandEvent(wx.EVT_CHOICE.typeId, dialog.provider_ctrl.GetId())
    event.SetEventObject(dialog.provider_ctrl)
    dialog.provider_ctrl.GetEventHandler().ProcessEvent(event)
    for _ in range(8):
        wx.SafeYield()


@pytest.fixture
def settings_window(wx_app):
    from ficary import gui_dialogs
    from ficary import prefs as prefs_module

    saved = prefs_module.Prefs()
    dialog = gui_dialogs.LlmSettingsDialog(None, saved)
    yield dialog
    dialog.Destroy()


@needs_display
class TestTheSettingsWindow:
    def test_lists_the_providers_the_rest_of_ficary_knows(self, settings_window):
        assert list(settings_window._PROVIDER_KEYS) == list(attribution.LLM_PROVIDERS)
        for provider in attribution.LLM_PROVIDERS:
            assert provider in settings_window._PROVIDER_LABELS
            assert provider in settings_window._DEFAULT_ENDPOINTS
            assert provider in settings_window._CURATED_MODELS
            assert provider in settings_window._HINTS

    def test_claude_code_says_it_uses_the_plan_and_needs_no_key(self, settings_window):
        choose_provider(settings_window, "claude-code")
        label = settings_window.provider_ctrl.GetStringSelection()
        assert "Claude Code" in label
        hint = settings_window.hint.GetLabel()
        assert "plan" in hint
        assert "API key" in hint and "not" in hint

    def test_claude_code_has_no_key_to_type(self, settings_window):
        settings_window.api_key_ctrl.SetValue("sk-typed-for-another-provider")
        choose_provider(settings_window, "claude-code")
        assert not settings_window.api_key_ctrl.IsEnabled()
        assert settings_window.api_key_ctrl.GetValue() == ""

    def test_the_address_box_becomes_the_programs_location(self, settings_window):
        choose_provider(settings_window, "claude-code")
        assert "location" in settings_window.endpoint_label.GetLabel().lower()
        assert "Claude Code" in settings_window.endpoint_ctrl.GetName()
        choose_provider(settings_window, "anthropic")
        assert "Endpoint" in settings_window.endpoint_label.GetLabel()
        assert settings_window.endpoint_ctrl.GetName() == "Endpoint URL"
        assert settings_window.api_key_ctrl.IsEnabled()

    def test_the_line_under_the_fields_stays_wrapped_and_the_window_its_width(self, settings_window):
        """On Windows, with wxWidgets 3.3, every change of provider left the line unwrapped. For Claude Code it is five lines' worth: the window grew to the width of the screen and the end of it was cut off."""
        import wx

        settings_window.Show()
        for _ in range(8):
            wx.SafeYield()
        opened_at = settings_window.GetSize().width
        for provider in ["openai", "claude-code", "anthropic", "claude-code", "ollama"]:
            choose_provider(settings_window, provider)
            hint = settings_window.hint
            assert hint.GetBestSize().width <= settings_window.FromDIP(560), f"{provider}: the line is {hint.GetBestSize().width} pixels wide"
            assert settings_window.GetSize().width == opened_at, f"{provider}: the window went from {opened_at} to {settings_window.GetSize().width} pixels wide"
        choose_provider(settings_window, "claude-code")
        assert settings_window.hint.GetLabel().count("\n") >= 2

    def test_no_two_fields_answer_to_the_same_key(self, settings_window):
        """Alt and a letter moves to a field; a letter shared by two fields moves to only one of them. Checked for the labels of the boxes, not the buttons, which already shared letters."""
        import wx

        choose_provider(settings_window, "claude-code")
        letters = []
        for control in settings_window.GetChildren()[0].GetChildren():
            if isinstance(control, wx.StaticText) and "&" in control.GetLabel():
                letters.append(control.GetLabel().split("&", 1)[1][0].lower())
        assert sorted(letters) == sorted(set(letters)), letters

    def test_the_other_providers_keys_come_back_after_a_look_at_claude_code(self, settings_window):
        choose_provider(settings_window, "anthropic")
        settings_window.api_key_ctrl.SetValue("sk-ant-mine")
        choose_provider(settings_window, "claude-code")
        choose_provider(settings_window, "anthropic")
        assert settings_window.api_key_ctrl.GetValue() == "sk-ant-mine"

    def test_claude_code_starts_with_a_model_filled_in(self, settings_window):
        choose_provider(settings_window, "claude-code")
        assert settings_window.model_ctrl.GetValue() == claude_code.DEFAULT_MODEL
        assert {"haiku", "sonnet", "opus"} <= set(settings_window.model_ctrl.GetItems())

    def test_saving_claude_code_asks_for_no_key_and_keeps_none(self, settings_window, monkeypatch):
        import wx

        from ficary import prefs as prefs_module

        def no_question(*args, **kwargs):
            raise AssertionError(f"saving Claude Code settings asked: {args[0]}")

        monkeypatch.setattr(wx, "MessageBox", no_question)
        monkeypatch.setattr(settings_window, "EndModal", lambda code: None)
        choose_provider(settings_window, "anthropic")
        settings_window.api_key_ctrl.SetValue("sk-ant-mine")
        settings_window.model_ctrl.SetValue("claude-sonnet-4-6")
        choose_provider(settings_window, "claude-code")
        settings_window._on_save(None)
        saved = settings_window._prefs
        assert saved.get(prefs_module.KEY_LLM_PROVIDER) == "claude-code"
        assert saved.get(prefs_module.KEY_LLM_MODEL) == claude_code.DEFAULT_MODEL
        assert saved.get(prefs_module.KEY_LLM_API_KEY) == ""
        claude_code_keys = prefs_module.llm_provider_pref_keys("claude-code")
        assert not saved.get(claude_code_keys[1])
        anthropic_keys = prefs_module.llm_provider_pref_keys("anthropic")
        assert saved.get(anthropic_keys[1]) == "sk-ant-mine"

    def test_the_test_button_sends_the_model_that_is_showing(self, settings_window, monkeypatch):
        import wx

        asked = {}

        def probe(**request):
            asked.update(request)
            return attribution.LLMProbeResult(ok=True, detail="fine")

        monkeypatch.setattr(attribution, "probe_llm_endpoint", probe)
        choose_provider(settings_window, "claude-code")
        settings_window.model_ctrl.SetValue("sonnet")
        settings_window._on_test_connection(None)
        deadline = time.monotonic() + 5
        while not asked and time.monotonic() < deadline:
            time.sleep(0.05)
        # Let the result reach the window while it is still there to write it.
        for _ in range(8):
            time.sleep(0.05)
            wx.SafeYield()
        assert asked["provider"] == "claude-code"
        assert asked["model"] == "sonnet"
