"""Runs the Claude Code program installed on this computer for one AI
request, so the request is made on the Claude plan its user is signed in to
and needs no API key.

Ficary never sees the sign-in. It starts the person's own ``claude`` program
in print mode, hands it the question, and reads the answer it prints. That
is the route Anthropic's terms leave open for a plan: a plan sign-in is for
Claude Code and Anthropic's own apps, and may not be collected, stored or
passed on by another program (code.claude.com/docs/en/legal-and-compliance,
read 2026-11-26). It is also why this provider is for the person at the
keyboard only, and is not offered on the web page that other people use.

A request is made on the plan or not at all. Nothing in the environment that
would send it elsewhere is passed on, and before the first story goes to a
program it is asked a question with no story in it: its answer says how
the request was signed in, and one that came through an API key or went to
another service stops everything there. Every answer after that is read the
same way.

What the request must and must not carry was measured against Claude Code
2.1.332 on 2026-11-27, and each choice below says what was seen. One thing
is known to be coming: the manual says bare mode "will become the default
for -p in a future release", and bare mode never reads a plan sign-in. When
that happens this provider will answer "not signed in" until the command
here is changed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .attribution import (
    _LLM_STATUSES_THAT_REPEAT,
    LLMProbeResult,
    LLMRejected,
    LLMTimeout,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)

PROVIDER = "claude-code"

# The program's name, and what a blank "Program location" setting means.
PROGRAM_NAME = "claude"

# The model a new setup starts with. Measured on two chapters of one
# book (482 paragraphs, 2026-11-27): neither sonnet nor haiku removed
# any story; of seven note lines the built-in pass had left, sonnet
# caught six and haiku four. Haiku answers a little sooner and draws
# less on the plan's allowance, and is one choice away in AI settings.
DEFAULT_MODEL = "sonnet"

# The instructions are handed over in a file inside the folder the
# program is started in, and named without that folder: an argument
# that never changes, with nothing of the person's in it.
PROMPT_FILE_NAME = "system-prompt.txt"

# A request must be made with the plan sign-in or not at all, so
# nothing in the environment may point it anywhere else. Claude Code's
# documented order (code.claude.com/docs/en/authentication, read
# 2026-11-27) puts a cloud provider (CLAUDE_CODE_USE_BEDROCK and its
# kind), a bearer token, an API key and a named profile above a plan
# sign-in, and says of the key that in print mode it "is always used
# when present". ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS send
# a request to another address or add a credential to it without any
# of those. Every variable of the two families is left out, so that a
# switch added by a later Claude Code is left out too. Neither family
# holds a proxy, a certificate or the Git Bash path, which a request
# needs and which are passed on. So is the plan's own long-lived token
# (CLAUDE_CODE_OAUTH_TOKEN).
_REDIRECTING_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")

# Bare mode never reads a plan sign-in, and this variable turns it on.
_MODE_VARIABLES = ("CLAUDE_CODE_SIMPLE",)

# What the program can report as the credential a request was made
# with (the ``apiKeySource`` of its first line). Its own description,
# read from Claude Code 2.1.332: "'ANTHROPIC_API_KEY' (environment
# variable), 'apiKeyHelper' (the configured helper command), '/login
# managed key' (an API key created and stored by /login with an
# Anthropic Console account), or 'none' (no API key in use - e.g.
# claude.ai OAuth login, a bearer token, or a third-party cloud
# provider)". The four after those are older names for a key, which it
# says current versions never print. A value not listed here is one a
# later version has added: it is not known to be a key, so the answer
# is used, and not vouched for.
_KEY_SOURCES = frozenset({
    "ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key",
    "user", "project", "org", "temporary",
})
_NO_KEY = "none"

# What Claude Code tells the programs it starts about its own session.
# They are there when Ficary's command line is run from inside a Claude
# Code session, and passed on they made the request describe itself as
# that session's kind ("cc_entrypoint=sdk-py" where it should say
# "sdk-cli") and ask for a session title besides.
_SESSION_VARIABLES = (
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
)

# A model name is passed as an argument, so it may only be what a model
# name is: a setting that began with a dash would be read as an option.
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]*")

# Windows batch files. One is never run: Windows hands a batch file's
# whole command to cmd.exe, which reads it a second time by rules of
# its own (a percent sign or an ampersand in a folder name changes the
# command), and looks for the programs the batch file names in the
# folder Ficary was started in.
_BATCH_SUFFIXES = (".cmd", ".bat")

# Where an npm install keeps the program itself, beside the batch file
# it leaves on the search path (the package's ``bin`` entry, read from
# the npm registry on 2026-11-27).
_NPM_PROGRAM = ("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")

# How long the program gets to say which version it is, and who is
# signed in. Both answer in well under a second; the allowance is for
# a first start after an update.
_QUICK_COMMAND_TIMEOUT_S = 20

# How long to wait for a stopped program to be gone and its output to
# close before walking away from it.
_STOP_WAIT_S = 5

# How often the wait for an answer looks at the clock.
_POLL_S = 0.25

# The most a program may print before it is stopped. An answer is a
# few thousand characters, sent twice over in the stream; a program
# that prints without end must not be allowed to fill the memory.
_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
_ERROR_OUTPUT_LIMIT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

# The Test button's question, and how long it waits for the answer.
_PROBE_INSTRUCTIONS = "Reply with the single word: ready"
_PROBE_QUESTION = "Are you there?"
_PROBE_TIMEOUT_S = 90

# Longest piece of the program's own words quoted to the user.
_QUOTE_MAX_CHARS = 240

# How much of what the program wrote to its error output goes in the
# debug log when a request fails.
_ERROR_OUTPUT_LOG_CHARS = 500

# The program's words are quoted in status lines and the log, which
# people attach to bug reports. Its own messages name no one, but a
# company's gateway or a later version might, so anything shaped like
# an email address or a key is taken out of a quotation.
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_KEY_LIKE = re.compile(
    r"\b(?:sk|pk|rk|key|token)[-_][A-Za-z0-9_-]{8,}|\b[A-Za-z0-9_-]{40,}\b"
)

_SETUP_PAGE = "https://code.claude.com/docs/en/setup"

# Programs already asked for their version, and what they said. A
# program is known by its path, size and time of last change, so one
# that has been updated or replaced is asked again.
_versions_seen: dict[tuple, str] = {}

# Programs (known as for ``_versions_seen``) and models whose way of
# signing in has been checked in this run of Ficary, with a question that
# holds no story (see ``_check_route``).
_routes_checked: set[tuple] = set()

# Said once in the log, not for every chapter.
_unconfirmed_sign_in_logged = False


class ClaudeCodeUnavailable(LLMUnavailable):
    """The Claude Code program could not be started: it is not installed, not
    where the settings say, or is some other program.

    The same failure for every chapter still to come, so the chapter loops
    stop asking after the first. ``str()`` is a plain sentence without its
    full stop, ready for a status line.
    """

    def explain(self) -> str:
        return str(self)


class ClaudeCodeFailed(LLMRejected):
    """Claude Code ran, and what it printed was not an answer that can be
    used.

    A kind of :class:`~ficary.attribution.LLMRejected` so the chapter loops
    treat it as they treat a provider's refusal: ``will_repeat`` ends the
    check for the rest of the book, anything else is given three chapters in
    a row. There is no HTTP status to format, so the sentence is made here.
    """

    def __init__(self, sentence: str, *, will_repeat: bool, status: int | None = None):
        """``sentence`` is the failure in plain words without its full stop.
        ``status`` is the HTTP status Claude Code passed on from Anthropic,
        when there was one.
        """
        self.status = status
        self.provider = PROVIDER
        self.detail = sentence
        self.provider_message = sentence
        self.codes = frozenset()
        self.will_repeat = will_repeat
        RuntimeError.__init__(self, sentence)

    def explain(self) -> str:
        return self.detail


@dataclass
class Answer:
    """What one request came back with."""

    text: str
    # The models that worked on it, as Claude Code names them.
    models: list[str] = field(default_factory=list)
    # Everything sent, however it was counted for billing.
    input_tokens: int = 0
    output_tokens: int = 0
    # Whether the program said, of this request, that no API key was in
    # use and that it went to Anthropic itself.
    sign_in_confirmed: bool = False
    # What it did say, as printed, for the log: the credential's source
    # and whether the request went to Anthropic. None where it said
    # nothing.
    key_source: object = None
    from_anthropic: object = None


# ── Finding the program ──────────────────────────────────────────


def _home_folder() -> Path:
    return Path.home()


def program_file_names(name: str, platform: str | None = None) -> list[str]:
    """The file names a program called ``name`` can have, best first.

    On Windows the program itself comes before a batch file of the same
    name: the installer leaves ``claude.exe``, an npm install leaves
    ``claude.cmd``. A batch file is found only to look for the program
    beside it (see :func:`_program_in`).
    """
    if platform is None:
        platform = sys.platform
    if platform != "win32" or Path(name).suffix:
        return [name]
    return [name + ".exe", name + ".cmd", name + ".bat"]


def _can_be_run(path: Path, platform: str | None = None) -> bool:
    if platform is None:
        platform = sys.platform
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    return platform == "win32" or os.access(path, os.X_OK)


def _program_in(folder: Path, name: str, platform: str | None = None) -> Path | None:
    """The program called ``name`` in ``folder``, or None.

    A batch file is never the answer. Where one is found, the program an
    npm install keeps beside it is looked for, and used if it is there.
    """
    for file_name in program_file_names(name, platform):
        candidate = folder / file_name
        if not _can_be_run(candidate, platform):
            continue
        if candidate.suffix.lower() not in _BATCH_SUFFIXES:
            return candidate
        beside = folder.joinpath(*_NPM_PROGRAM)
        if _can_be_run(beside, platform):
            return beside
    return None


def _search_folders(platform: str | None = None) -> list[Path]:
    """Where to look for the program: the search path, then the folders the
    installers use.

    The search path is read here and not by ``shutil.which``, which on
    Windows also looks in the folder Ficary was started in. A file called
    claude in a downloads folder must not be run for that reason, so entries
    that are not full paths are passed over too. So is ``WindowsApps``: an
    older Claude Desktop can leave a ``Claude.exe`` there, ahead of the real
    one (code.claude.com/docs/en/troubleshoot-install).

    The installers' folders are for a Ficary started from the desktop, which
    on macOS gets a short search path without the person's own folders on
    it.
    """
    if platform is None:
        platform = sys.platform
    folders = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        folder = Path(entry)
        if not folder.is_absolute():
            continue
        if folder.name.lower() == "windowsapps":
            continue
        folders.append(folder)
    # The installer's own folder on Windows, macOS and Linux.
    folders.append(_home_folder() / ".local" / "bin")
    if platform == "win32":
        # npm's folder for programs installed with -g.
        roaming = os.environ.get("APPDATA")
        if roaming:
            folders.append(Path(roaming) / "npm")
    elif platform == "darwin":
        # Homebrew's, on Apple and Intel machines.
        folders.append(Path("/opt/homebrew/bin"))
        folders.append(Path("/usr/local/bin"))
    return folders


def _find_in_folders(name: str, folders: list[Path], platform: str | None = None) -> str | None:
    for folder in folders:
        found = _program_in(folder, name, platform)
        if found is not None:
            # A full path, so that it means the same program from the
            # empty folder a request is started in.
            return os.path.abspath(found)
    return None


def _is_a_web_address(setting: str) -> bool:
    return "://" in setting


def _named_in_settings(setting: str | None) -> str:
    """The program the settings name, or "" when they leave it to be found.

    A web address counts as leaving it: the command line reads the saved
    endpoint whichever provider it was saved for, so another provider's
    address can arrive here.
    """
    wanted = (setting or "").strip()
    if wanted == PROGRAM_NAME or _is_a_web_address(wanted):
        return ""
    return wanted


def find_program(setting: str | None = None, platform: str | None = None) -> str | None:
    """The full path of the Claude Code program to run, or None when there is
    none to be found.

    ``setting`` is "Program location" from AI settings. Blank, or the plain
    name ``claude``, means look for it. A path is used as it is and nothing
    else is tried, so a wrong path is reported and not quietly replaced.
    """
    wanted = _named_in_settings(setting)
    try:
        if not wanted:
            return _find_in_folders(PROGRAM_NAME, _search_folders(platform), platform)
        path = Path(wanted).expanduser()
        if path.parent == Path("."):
            # A plain name of the person's choosing.
            return _find_in_folders(wanted, _search_folders(platform), platform)
        return _find_in_folders(path.name, [path.parent], platform)
    except (OSError, RuntimeError, ValueError) as exc:
        # A home folder that cannot be worked out, a "~someone" who is
        # not a user here, a path the system will not read.
        logger.debug("Looking for Claude Code failed: %s", exc)
        return None


def _batch_file_found(setting: str | None, platform: str | None = None) -> str | None:
    """A batch file that answers to the program's name, when that is all
    there is of it. For saying so in place of "not found".
    """
    if (platform or sys.platform) != "win32":
        return None
    wanted = _named_in_settings(setting)
    try:
        if wanted and Path(wanted).expanduser().parent != Path("."):
            path = Path(wanted).expanduser()
            folders, name = [path.parent], path.name
        else:
            folders, name = _search_folders(platform), wanted or PROGRAM_NAME
        for folder in folders:
            for file_name in program_file_names(name, platform):
                candidate = folder / file_name
                if candidate.suffix.lower() in _BATCH_SUFFIXES and _can_be_run(candidate, platform):
                    return str(candidate)
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _not_found_sentence(setting: str | None, platform: str | None = None) -> str:
    batch_file = _batch_file_found(setting, platform)
    if batch_file:
        return (
            f'Claude Code was found only as a batch file, "{batch_file}", '
            "which Ficary does not run (install Claude Code with its own "
            f"installer from {_SETUP_PAGE}, or put the full path to "
            "claude.exe in AI settings under Program location)"
        )
    wanted = _named_in_settings(setting)
    if wanted:
        return (
            f'Claude Code was not found at "{wanted}", the Program location '
            "in AI settings (clear it to have Ficary look in the usual places)"
        )
    return (
        "Claude Code was not found on this computer (install it from "
        f"{_SETUP_PAGE}, run claude in a terminal once to sign in, and if "
        "it is installed somewhere unusual put the full path to the claude "
        "program in AI settings under Program location)"
    )


# ── Starting it ──────────────────────────────────────────────────


def child_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the program is started with: this one, without
    anything that would send the request somewhere other than the plan, and
    without the session Ficary may have been started from.
    """
    if environment is None:
        environment = os.environ
    left_out = set(_MODE_VARIABLES) | set(_SESSION_VARIABLES)
    kept = {}
    for name, value in environment.items():
        upper = name.upper()
        if upper in left_out or upper.startswith(_REDIRECTING_PREFIXES):
            continue
        kept[name] = value
    return kept


def command_line(program: str, model: str) -> list[str]:
    """The command for one request. The question itself goes in on standard
    input and the instructions in a file, so nothing from a story is ever
    part of it.

    * ``-p``: answer once and stop.
    * ``--safe-mode``: the person's CLAUDE.md, hooks, plugins, skills and
      MCP servers stay out of it. Unlike ``--bare`` it keeps the plan
      sign-in.
    * ``--setting-sources ""``: their settings files stay out of it too.
      Safe mode still read them: an advisor model set there put a tool for
      consulting the largest model on a request to the smallest, and a
      key-fetching script named there (``apiKeyHelper``) was run. With
      this option neither happened.
    * ``--no-session-persistence``: no transcript is written; nothing was
      left in the Claude Code folder after a request.
    * ``--tools ""``: the model has no tools. Asked to run a command and
      read a file, it answered that it could not. Text from a story must
      never be able to do either.
    * ``--output-format stream-json --verbose``: a line saying how the
      request is set up (which sign-in, which tools), then the answer or
      the reason there is none. The first is what lets a request that
      did not go through the plan be stopped (:func:`_check_route`,
      :func:`read_answer`).
    """
    name = (model or "").strip()
    if not name:
        raise ClaudeCodeFailed(
            "Claude Code needs a model name in AI settings, such as sonnet",
            will_repeat=True,
        )
    if not _MODEL_NAME.fullmatch(name):
        raise ClaudeCodeFailed(
            f"{_quoted(name)} cannot be given to Claude Code as a model name "
            "(use a name such as sonnet, haiku or claude-sonnet-4-6 in AI settings)",
            will_repeat=True,
        )
    return [
        program,
        "-p",
        "--safe-mode",
        "--setting-sources", "",
        "--no-session-persistence",
        "--tools", "",
        "--model", name,
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt-file", PROMPT_FILE_NAME,
    ]


def _kernel32():
    """Windows' process functions, with their arguments declared."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_job_for(process_id: int):
    """A Windows job with the started program in it, or None when one could
    not be made.

    Whatever the program starts joins the job too, and stays in it after the
    program itself has ended, so all of it can be stopped together. Stopping
    by process number alone cannot: once the program has gone, what it
    started is no longer found under it.
    """
    try:
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        set_quota_and_terminate = 0x0100 | 0x0001
        handle = kernel32.OpenProcess(set_quota_and_terminate, False, process_id)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        joined = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not joined:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 — a request can still be made without one
        logger.debug("No Windows job could be made for Claude Code", exc_info=True)
        return None


def _end_windows_job(job, *, stop: bool) -> None:
    """Let go of a job, first stopping everything in it when ``stop``."""
    if job is None:
        return
    try:
        kernel32 = _kernel32()
        if stop:
            kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
    except Exception:  # noqa: BLE001 — stopping goes on by other means
        logger.debug("The Windows job for Claude Code could not be ended", exc_info=True)


def _stop(process: subprocess.Popen, job, readers: list) -> None:
    """Stop the program and whatever it started, then wait a moment for it
    to be gone and for its output to close.

    Stopping only the program is not enough: something it started can hold
    its output open, and the read of that output then never ends.

    ``readers`` is each output with the thread reading it, for the threads
    that did start. Every wait here has a limit, so that stopping cannot
    itself wait for ever on something that got away.
    """
    try:
        if sys.platform == "win32":
            _end_windows_job(job, stop=True)
            # As well as the job, which a program started a moment before
            # it joined would not be in. By its full path: the search path
            # is the person's own, and is not where to find a program that
            # stops others.
            taskkill = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "taskkill.exe",
            )
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=_STOP_WAIT_S,
                check=False,
            )
        else:
            # The program was started in a session of its own, so its
            # process group is it and everything it started, and outlives
            # it for as long as any of them runs.
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=_STOP_WAIT_S)
    except (OSError, subprocess.SubprocessError):
        pass
    until = time.monotonic() + _STOP_WAIT_S
    for reader, _stream in readers:
        reader.join(max(0.0, until - time.monotonic()))
    for reader, stream in readers:
        if reader.is_alive():
            # Something that got away still holds this output open, and
            # the thread is in the middle of reading it. Closing it from
            # here would wait for that read to end, which it never will;
            # the thread is left to it.
            continue
        _close(stream)


def _close(stream) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass


def _feed(stream, data: bytes) -> None:
    """Write the question to the program's input and close it.

    In a thread of its own, so that a program which stops reading cannot
    hold up the clock: written from the waiting thread, a long chapter
    would block it on Windows with no time limit at all.
    """
    try:
        stream.write(data)
    except (OSError, ValueError):
        # The program has ended or been stopped without reading it all.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _collect(stream, limit: int, into: bytearray, too_much: threading.Event) -> None:
    """Read one of the program's outputs into ``into`` until it ends, or
    until it has printed more than ``limit`` bytes, which sets ``too_much``.
    """
    try:
        while True:
            chunk = stream.read1(_READ_CHUNK_BYTES)
            if not chunk:
                return
            if len(into) + len(chunk) > limit:
                too_much.set()
                return
            into.extend(chunk)
    except (OSError, ValueError):
        return


def _wait(
    process: subprocess.Popen, readers: list[threading.Thread],
    deadline: float, too_much: threading.Event,
) -> bool:
    """Wait for the program to finish. True when it ended, with its output
    read whole, before ``deadline``; False when time ran out or it printed
    too much.
    """
    while True:
        if too_much.is_set():
            return False
        running = [reader for reader in readers if reader.is_alive()]
        if not running:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        running[0].join(min(remaining, _POLL_S))
    if too_much.is_set():
        return False
    # Its output has closed; the program itself ends a moment later.
    try:
        process.wait(timeout=max(deadline - time.monotonic(), 1.0))
    except subprocess.TimeoutExpired:
        return False
    return True


def _remove_folder(folder: str) -> None:
    """Remove a request's folder. On Windows a folder cannot go while a
    program that was started in it is still ending, so it is tried a few
    times.
    """
    for _attempt in range(10):
        shutil.rmtree(folder, ignore_errors=True)
        if not os.path.exists(folder):
            return
        time.sleep(0.2)


def _run(
    command: list[str], *, input_bytes: bytes | None = None,
    files: Mapping[str, bytes] | None = None, timeout_s: float,
) -> tuple[int, bytes, bytes]:
    """Run ``command`` and return its exit status, what it printed, and what
    it wrote to its error output.

    Every start of the program goes through here, and every one is made the
    same way:

    * in an empty folder of its own, holding only ``files``, and removed
      afterwards. No project's CLAUDE.md or settings are near it, and
      nothing in the folder Ficary was started in can be picked up.
    * with all three of its streams given explicitly. The packaged Windows
      app has none of its own to pass on, and a program started with those
      fails at once ([WinError 6], see ``cf_solve.py``). They carry bytes,
      written and read as UTF-8; text mode on Windows would use the
      system's own encoding.
    * with one time limit over the whole of it, writing the question
      included, and a limit on how much it may print.

    Raises :class:`ClaudeCodeUnavailable` when it cannot be started,
    :class:`~ficary.attribution.LLMTimeout` when the time runs out, and
    :class:`ClaudeCodeFailed` when it prints too much. In each case, and on
    Ctrl+C, the program and whatever it started are stopped first.
    """
    try:
        folder = tempfile.mkdtemp(prefix="ficary-claude-code-")
    except OSError as exc:
        raise ClaudeCodeUnavailable(
            f"Claude Code could not be started: no folder could be made for it ({exc})"
        ) from exc
    try:
        try:
            for name, content in (files or {}).items():
                (Path(folder) / name).write_bytes(content)
        except OSError as exc:
            raise ClaudeCodeUnavailable(
                "Claude Code could not be started: its instructions could "
                f"not be written ({exc})"
            ) from exc
        return _run_in(folder, command, input_bytes, timeout_s)
    finally:
        _remove_folder(folder)


def _run_in(
    folder: str, command: list[str], input_bytes: bytes | None, timeout_s: float,
) -> tuple[int, bytes, bytes]:
    options: dict = {}
    if sys.platform == "win32":
        # Without this a console window flashes up for every chapter.
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=folder,
            env=child_environment(),
            **options,
        )
    except OSError as exc:
        raise ClaudeCodeUnavailable(
            f'Claude Code could not be started from "{command[0]}" ({exc})'
        ) from exc

    job = None
    # Each output with the thread reading it, added once the thread has
    # started.
    readers: list = []
    output, errors = bytearray(), bytearray()
    too_much = threading.Event()
    deadline = time.monotonic() + timeout_s
    # Everything from here on is undone by stopping the program, whatever
    # goes wrong: a thread that will not start, or Ctrl+C at any moment.
    try:
        if sys.platform == "win32":
            job = _windows_job_for(process.pid)
        for stream, limit, into in (
            (process.stdout, _OUTPUT_LIMIT_BYTES, output),
            (process.stderr, _ERROR_OUTPUT_LIMIT_BYTES, errors),
        ):
            reader = threading.Thread(
                target=_collect, args=(stream, limit, into, too_much),
                daemon=True,
            )
            reader.start()
            readers.append((reader, stream))
        if input_bytes is not None:
            threading.Thread(
                target=_feed, args=(process.stdin, input_bytes), daemon=True,
            ).start()
        finished = _wait(
            process, [reader for reader, _stream in readers], deadline, too_much,
        )
    except BaseException:
        # Ctrl+C in a terminal does not reach a program in a session of
        # its own, so it is stopped here.
        _stop(process, job, readers)
        raise
    if not finished:
        _stop(process, job, readers)
        if too_much.is_set():
            megabytes = _OUTPUT_LIMIT_BYTES / (1024 * 1024)
            raise ClaudeCodeFailed(
                "Claude Code printed more than an answer can hold (over "
                f"{megabytes:g} MB) and was stopped",
                will_repeat=False,
            )
        raise LLMTimeout(f"Claude Code did not answer within {timeout_s:g}s")
    _end_windows_job(job, stop=False)
    for _reader, stream in readers:
        _close(stream)
    return process.returncode, bytes(output), bytes(errors)


def _lines(text: str) -> list[str]:
    """The lines of what a program printed: what it ended with a line feed.

    Not ``str.splitlines``, which also breaks at a dozen other characters,
    among them U+2028 and U+2029. Those may stand as they are inside a JSON
    string, so an answer holding one would be cut in two and no longer be
    read as an answer.
    """
    return [line.rstrip("\r") for line in text.split("\n")]


def _first_line(text: str) -> str:
    for line in _lines(text):
        if line.strip():
            return line.strip()
    return ""


def _quoted(words: str) -> str:
    """The program's ``words`` in quotation marks for a status line or the
    log: shortened, and with anything shaped like an email address or a key
    taken out. Not for paths, which are shown whole.
    """
    words = _EMAIL_ADDRESS.sub("(an email address)", words)
    words = _KEY_LIKE.sub("(a key)", words)
    if len(words) > _QUOTE_MAX_CHARS:
        words = words[:_QUOTE_MAX_CHARS].rstrip() + "…"
    return f'"{words}"'


def _known_as(program: str) -> tuple:
    """What tells one program file from another at the same path."""
    try:
        details = os.stat(program)
    except OSError:
        return (program, None, None)
    return (program, details.st_size, details.st_mtime_ns)


def program_version(program: str) -> str:
    """The version of the Claude Code program at ``program``.

    Raises :class:`ClaudeCodeUnavailable` when it is something else. A
    program is given no request until it has said it is Claude Code, so a
    story is not handed to whatever else answers to the name. That guards
    against the wrong program; it is no proof of where a program came from.
    One that has answered is not asked again until its file changes.
    """
    known_as = _known_as(program)
    if known_as in _versions_seen:
        return _versions_seen[known_as]
    try:
        _status, output, _errors = _run(
            [program, "--version"], timeout_s=_QUICK_COMMAND_TIMEOUT_S,
        )
    except LLMTimeout:
        # Claude Code says its version at once. Something that does not
        # is something else, and that is the same for every chapter.
        raise ClaudeCodeUnavailable(
            f'"{program}" is not Claude Code (asked for its version, '
            f"it had not answered after {_QUICK_COMMAND_TIMEOUT_S} seconds)"
        ) from None
    except ClaudeCodeFailed as exc:
        raise ClaudeCodeUnavailable(
            f'"{program}" is not Claude Code (asked for its version: '
            f"{exc.explain()})"
        ) from None
    said = _first_line(output.decode("utf-8", errors="replace"))
    if "(Claude Code)" not in said:
        raise ClaudeCodeUnavailable(
            f'"{program}" is not Claude Code (asked for its version, '
            f"it answered {_quoted(said)})"
        )
    version = said.split()[0]
    _versions_seen[known_as] = version
    logger.info("Claude Code %s found at %s", version, program)
    return version


# ── Reading what it printed ──────────────────────────────────────


def _printed_objects(printed: str) -> list[dict]:
    """Every line of what the program printed that is a JSON object."""
    objects = []
    for line in _lines(printed):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def _as_printed(value) -> str:
    """A value from the program's own output, as it printed it, for a
    status line or the log. Kept short: it is meant to be one word.
    """
    try:
        shown = json.dumps(value)
    except (TypeError, ValueError):
        shown = repr(value)
    return shown[:60]


def _whole_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def read_answer(exit_status: int, output: bytes, errors: bytes) -> Answer:
    """Turn what the program printed into the model's answer, or raise
    :class:`ClaudeCodeFailed` saying why there is none.

    Two of the lines it prints are read. The first says how the request is
    set up; the last is the result.

    A run has failed when its exit status is not zero (the manual's rule) or
    its result says ``is_error`` (what the program does: a failed run still
    prints a result, with ``subtype`` "success" and the reason as its text).

    Three failures mean the same for every later request, and were each seen
    from the real program: nobody signed in, a model the account cannot use
    (Anthropic's 404, passed on), and an option this version does not know.
    The first two are in the result; the third is a line on the error output
    and no result at all. Any other failure is left to the chapter loop's
    three-in-a-row rule. That includes a plan's usage limit, whose wording
    has not been seen.

    An answer is refused, however good, when the first line says the request
    was made with an API key or was sent somewhere other than Anthropic:
    that is not the plan, and somebody is paying for it by the token. The
    real program was seen to say both of a request with a key in its
    environment and another address to send it to. Ficary leaves both out of
    the environment, so this is for what it cannot leave out, such as a
    setup an organisation manages. The refusal quotes what the program
    said, so that a wrong one can be read from the log.

    The line is only vouched for when it says both things outright: no key,
    and sent to Anthropic. The program calls the second an internal field
    and leaves it out where it cannot tell. A line that says less, or
    something this version of Ficary does not know, leaves the answer used
    and unconfirmed.
    """
    printed = output.decode("utf-8", errors="replace")
    complaints = errors.decode("utf-8", errors="replace").strip()
    objects = _printed_objects(printed)
    setup = None
    result = None
    for printed_object in objects:
        kind = printed_object.get("type")
        if kind == "system" and printed_object.get("subtype") == "init" and setup is None:
            setup = printed_object
        elif kind == "result":
            result = printed_object

    if result is None:
        for line in _lines(complaints):
            if "unknown option" in line.lower():
                raise ClaudeCodeFailed(
                    f"Claude Code could not answer: {_quoted(line.strip())} "
                    "(this Claude Code is too old for Ficary; update it by "
                    "running claude update in a terminal)",
                    will_repeat=True,
                )
        words = _first_line(complaints)
        if not words and not objects:
            words = _first_line(printed)
        if not words:
            raise ClaudeCodeFailed(
                f"Claude Code stopped with exit status {exit_status} and "
                "gave no answer",
                will_repeat=False,
            )
        raise ClaudeCodeFailed(
            f"Claude Code stopped with exit status {exit_status} and "
            f"printed: {_quoted(words)}",
            will_repeat=False,
        )

    text = result.get("result")
    if not isinstance(text, str):
        text = ""

    if result.get("is_error") or exit_status != 0:
        status = result.get("api_error_status")
        if isinstance(status, bool) or not isinstance(status, int):
            status = None
        if not text:
            raise ClaudeCodeFailed(
                "Claude Code reported a failure without saying what it was "
                f"(exit status {exit_status})",
                will_repeat=status in _LLM_STATUSES_THAT_REPEAT, status=status,
            )
        sentence = f"Claude Code could not answer: {_quoted(text)}"
        not_signed_in = "not logged in" in text.lower()
        if not_signed_in:
            sentence += " (to sign in, open a terminal, run claude and type /login)"
        elif status == 404:
            sentence += (
                " (in Ficary the model is chosen in AI settings; sonnet, "
                "haiku and opus are the usual names)"
            )
        raise ClaudeCodeFailed(
            sentence,
            will_repeat=not_signed_in or status in _LLM_STATUSES_THAT_REPEAT,
            status=status,
        )

    key_source = None
    from_anthropic = None
    if setup is not None:
        key_source = setup.get("apiKeySource")
        from_anthropic = setup.get("served_by_anthropic")
        said = (
            f"apiKeySource {_as_printed(key_source)}, "
            f"served_by_anthropic {_as_printed(from_anthropic)}"
        )
        if isinstance(key_source, str) and key_source in _KEY_SOURCES:
            raise ClaudeCodeFailed(
                "Claude Code made the request with an API key and not with "
                f"a Claude plan (it reports {said}), so its answer was not "
                "used (sign in to a plan in Claude Code, or choose the "
                "Anthropic provider in AI settings to use a key)",
                will_repeat=True,
            )
        if from_anthropic is False:
            raise ClaudeCodeFailed(
                "Claude Code sent the request to another service and not to "
                f"Anthropic on a Claude plan (it reports {said}), so its "
                "answer was not used",
                will_repeat=True,
            )
    sign_in_confirmed = key_source == _NO_KEY and from_anthropic is True

    usage = result.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    models = result.get("modelUsage")
    return Answer(
        text=text,
        models=list(models) if isinstance(models, dict) else [],
        input_tokens=(
            _whole_number(usage.get("input_tokens"))
            + _whole_number(usage.get("cache_creation_input_tokens"))
            + _whole_number(usage.get("cache_read_input_tokens"))
        ),
        output_tokens=_whole_number(usage.get("output_tokens")),
        sign_in_confirmed=sign_in_confirmed,
        key_source=key_source,
        from_anthropic=from_anthropic,
    )


# ── One request ──────────────────────────────────────────────────


def _request(
    found: str, version: str, model: str, *, system_prompt: str,
    user_prompt: str, timeout_s: float, purpose: str,
) -> Answer:
    """One run of the program at ``found`` with one question, its answer
    read and logged. ``purpose`` names the run in the log.
    """
    global _unconfirmed_sign_in_logged

    command = command_line(found, model)
    started = time.monotonic()
    exit_status, output, errors = _run(
        command,
        # ``replace``: a half of a character pair left by a badly made
        # page must not stop the chapter from being sent.
        input_bytes=user_prompt.encode("utf-8", errors="replace"),
        files={PROMPT_FILE_NAME: system_prompt.encode("utf-8", errors="replace")},
        timeout_s=timeout_s,
    )
    seconds = time.monotonic() - started

    try:
        answer = read_answer(exit_status, output, errors)
    except ClaudeCodeFailed as exc:
        logger.warning(
            "Claude Code %s at %s failed (%s) after %.1f s: %s",
            version, found, purpose, seconds, exc.explain(),
        )
        logger.debug(
            "Claude Code exit status %d; its error output began: %s",
            exit_status,
            _quoted(errors.decode("utf-8", errors="replace")[:_ERROR_OUTPUT_LOG_CHARS]),
        )
        raise
    logger.info(
        "Claude Code %s at %s answered (%s) in %.1f s (model %s, %d tokens "
        "in, %d out, plan sign-in %s: apiKeySource %s, served_by_anthropic %s)",
        version, found, purpose, seconds,
        ", ".join(answer.models) or model,
        answer.input_tokens, answer.output_tokens,
        "confirmed" if answer.sign_in_confirmed else "not confirmed",
        _as_printed(answer.key_source), _as_printed(answer.from_anthropic),
    )
    if len(answer.models) > 1:
        logger.warning(
            "Claude Code used more than one model for one request: %s",
            ", ".join(answer.models),
        )
    if not answer.sign_in_confirmed and not _unconfirmed_sign_in_logged:
        _unconfirmed_sign_in_logged = True
        logger.warning(
            "Claude Code %s did not say how its request was signed in, so "
            "Ficary could not confirm it was made on a Claude plan",
            version,
        )
    return answer


def _route_key(found: str, model: str) -> tuple:
    return (_known_as(found), (model or "").strip())


def _check_route(found: str, version: str, model: str, timeout_s: float) -> None:
    """Before the first story goes to a program, ask it a question that holds
    no story, and stop if its answer shows the request would not be made on
    the plan.

    The program says how a request was signed in only along with its answer:
    it prints nothing before it has read the question (measured on 2.1.332),
    so a question is the only way to find out before a chapter is sent. The
    same failures that stop a chapter stop the check, so nobody signed in, a
    model the plan cannot use and a Claude Code too old for Ficary are all
    found before any story is sent too.

    Done once for each program and model in a run of Ficary; a program whose
    file changes is checked again. Every answer after it is still read the
    same way (:func:`read_answer`), for whatever changes in between.
    """
    key = _route_key(found, model)
    if key in _routes_checked:
        return
    _request(
        found, version, model,
        system_prompt=_PROBE_INSTRUCTIONS, user_prompt=_PROBE_QUESTION,
        timeout_s=timeout_s, purpose="the check made before any story is sent",
    )
    _routes_checked.add(key)


def _ask(
    *, program: str | None, model: str, system_prompt: str,
    user_prompt: str, timeout_s: float,
) -> Answer:
    found = find_program(program)
    if found is None:
        raise ClaudeCodeUnavailable(_not_found_sentence(program))
    version = program_version(found)
    _check_route(found, version, model, timeout_s)
    return _request(
        found, version, model,
        system_prompt=system_prompt, user_prompt=user_prompt,
        timeout_s=timeout_s, purpose="a request",
    )


def ask(
    *, program: str | None, model: str, system_prompt: str,
    user_prompt: str, timeout_s: float,
) -> str:
    """Ask the model one question through Claude Code and return its answer
    as text.

    ``program`` is the "Program location" setting; blank means find it.
    Raises :class:`ClaudeCodeUnavailable` when the program cannot be found
    or started, :class:`~ficary.attribution.LLMTimeout` when it does not
    answer in ``timeout_s`` seconds, and :class:`ClaudeCodeFailed` when it
    answers with a failure. Nothing else leaves here: the chapter loops know
    those three, and would count anything else as one unreadable reply and
    ask again for the next chapter.

    A failed request is not tried again here: Claude Code has already
    retried what is worth retrying.
    """
    try:
        return _ask(
            program=program, model=model, system_prompt=system_prompt,
            user_prompt=user_prompt, timeout_s=timeout_s,
        ).text
    except (LLMUnavailable, LLMRejected):
        raise
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.debug("A Claude Code request failed in Ficary itself", exc_info=True)
        raise ClaudeCodeFailed(
            f"Claude Code could not be asked ({type(exc).__name__}: {exc})",
            will_repeat=False,
        ) from exc


# ── The Test button ──────────────────────────────────────────────


def _sign_in_line(program: str) -> str:
    """Who Claude Code says is signed in, as a sentence for the Test button,
    or "" when it will not say.

    For the person's eyes only: the organisation's name tells two accounts
    apart, so it is shown, but it and the email address stay out of the log,
    which people attach to bug reports. This is information and not the
    test. It reads the settings files a request leaves out, so it can report
    a sign-in a request will not have.
    """
    try:
        _status, output, _errors = _run(
            [program, "auth", "status"], timeout_s=_QUICK_COMMAND_TIMEOUT_S,
        )
    except (LLMUnavailable, LLMRejected) as exc:
        logger.debug("Claude Code would not say who is signed in: %s", exc)
        return ""
    try:
        status = json.loads(output.decode("utf-8", errors="replace"))
    except ValueError:
        status = None
    if not isinstance(status, dict):
        logger.debug("Claude Code's sign-in status was not readable")
        return ""
    signed_in = bool(status.get("loggedIn"))
    method = status.get("authMethod")
    plan = status.get("subscriptionType")
    logger.info(
        "Claude Code sign-in: signed in %s, method %s, plan %s",
        signed_in,
        method if isinstance(method, str) else None,
        plan if isinstance(plan, str) else None,
    )
    if not signed_in:
        return "Nobody is signed in to it."
    if method not in ("claude.ai", "oauth_token"):
        return (
            f"Its sign-in is not a Claude plan (it reports {method}). Ficary "
            "leaves API keys out of a request, so one may fail until a plan "
            "is signed in."
        )
    line = "Signed in with a Claude plan"
    if isinstance(plan, str) and plan:
        line += f" ({plan})"
    organization = status.get("orgName")
    if isinstance(organization, str) and organization:
        line += f", organization {organization}"
    return line + "."


def probe(*, program: str | None, model: str | None) -> LLMProbeResult:
    """Find the program, then ask the model one short question with the
    command a chapter gets.

    The question is the test. The program's version and its sign-in status
    can both look right while a request fails: on a build too old to know an
    option, or with a sign-in that comes from the settings a request leaves
    out. It costs a few hundred tokens of the plan's allowance, and stands in
    for the check a book would otherwise begin with (:func:`_check_route`).
    """
    found = find_program(program)
    if found is None:
        return LLMProbeResult(ok=False, detail=_not_found_sentence(program) + ".")
    try:
        version = program_version(found)
    except (LLMUnavailable, LLMRejected) as exc:
        return LLMProbeResult(ok=False, detail=f"{exc.explain()}.")

    parts = [f"Claude Code {version} at {found}."]
    sign_in = _sign_in_line(found)
    if sign_in:
        parts.append(sign_in)

    asked = (model or "").strip() or DEFAULT_MODEL
    started = time.monotonic()
    try:
        answer = _request(
            found, version, asked,
            system_prompt=_PROBE_INSTRUCTIONS, user_prompt=_PROBE_QUESTION,
            timeout_s=_PROBE_TIMEOUT_S, purpose="the Test button's question",
        )
    except (LLMUnavailable, LLMRejected) as exc:
        parts.append(f"The test question failed: {exc.explain()}.")
        return LLMProbeResult(ok=False, detail=" ".join(parts))
    except Exception as exc:  # noqa: BLE001 — the window shows it, never a crash
        logger.debug("The Claude Code test failed in Ficary itself", exc_info=True)
        parts.append(f"The test question failed ({type(exc).__name__}: {exc}).")
        return LLMProbeResult(ok=False, detail=" ".join(parts))
    seconds = time.monotonic() - started
    if not answer.text.strip():
        parts.append(
            f"Asked {asked} a test question and the answer came back empty."
        )
        return LLMProbeResult(ok=False, detail=" ".join(parts))
    parts.append(
        f"Asked {asked} a test question and it answered in {seconds:.1f} seconds."
    )
    # The same question the first chapter would wait for: a book started
    # now need not ask it again.
    _routes_checked.add(_route_key(found, asked))
    if answer.sign_in_confirmed:
        parts.append("The request was made on the plan sign-in, with no API key.")
    else:
        parts.append(
            "This Claude Code did not say how the request was signed in, so "
            "that could not be confirmed."
        )
    return LLMProbeResult(ok=True, detail=" ".join(parts))
