"""Stands in for the Claude Code program in tests. It answers the three commands Ficary runs (``--version``, ``auth status`` and a print-mode request) with output captured from the real program, version 2.1.332, on 2026-11-27, and writes down how it was started so a test can read that back.

The captured output is in ``fixtures/claude_code``. Session numbers, folder names and the plan's usage figures in it were replaced, and the long lists of built-in skills emptied; nothing else was changed. Where a behaviour below prints something the real program was not seen to print, its comment says so.

Environment variables steer it. ``STAND_IN_BEHAVIOUR`` picks what it does with a request (see ``BEHAVIOURS``). ``STAND_IN_NOTES`` names a file that gets one JSON line for every time it is started. ``STAND_IN_REPLY``, when set, is the model's answer; otherwise it answers "not a note" for every paragraph it was sent. ``STAND_IN_LOOK_FOR`` holds words, one per line, and the notes say where each was seen.

The notes hold the names of the environment variables it was given and never their values: the computer running the tests has secrets of its own in there, and a failing test prints what it compared.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CAPTURED = Path(__file__).resolve().parent / "fixtures" / "claude_code"

BEHAVIOURS = (
    # The model answers.
    "answers",
    # Nobody is signed in to Claude Code.
    "not-signed-in",
    # The model name is not one the account can use.
    "unknown-model",
    # An older Claude Code that does not know one of the options it was given. The real program's words for an option it does not know, with the option's name changed.
    "too-old",
    # Something that is not Claude Code's output: a crash message. Made up.
    "prints-nonsense",
    # It stops with a failure status and prints nothing at all. Made up.
    "says-nothing",
    # It reads the question, never finishes, and has started a program of its own that never finishes either and holds the same output open. Made up.
    "never-answers",
    # It never reads the question and never finishes. Made up.
    "never-reads",
    # It ends at once without an answer, leaving behind a program it started, which holds its output open. Made up.
    "leaves-something-running",
    # It never finishes, and has started a program that has left its process group and session, so stopping it and everything in its group leaves that program running with the output open. Made up.
    "something-gets-away",
    # It prints far more than an answer can be (32 MB), then never finishes. Made up. Not without end: a test of the limit that failed would otherwise fill the memory of the computer running it.
    "prints-far-too-much",
    # The request was made with an API key from the environment and sent to another address. The first line is the real program's, from a run given a made-up key and a closed local address; that run never got an answer, so the answer here is the ordinary one.
    "uses-an-api-key",
    # No key, but the request went to a service that is not Anthropic. Made up from the two real first lines.
    "uses-another-service",
    # An answer with no first line saying how the request is set up, as a later version might print. Made up.
    "does-not-say-how-it-signed-in",
    # The model's answer is empty. Made up.
    "answers-nothing",
    # A failure whose words hold an email address and a key, as a company's gateway might send. Made up.
    "fails-with-private-words",
    # A different program that happens to be called claude.
    "another-program",
)

# What ``claude auth status`` prints for a plan sign-in, with a made-up person and organisation.
SIGNED_IN_STATUS = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "analyticsDisabled": False,
    "loginExpiresAt": "2027-01-01T00:00:00.000Z",
    "consoleProfileSignInOffered": True,
    "email": "reader@example.org",
    "orgId": "00000000-0000-4000-8000-00000000000a",
    "orgName": "Example Reading Circle",
    "subscriptionType": "team",
    "storedClaudeAiLogin": True,
    "planTier": "other",
}

SIGNED_OUT_STATUS = {
    "loggedIn": False,
    "authMethod": "none",
    "apiProvider": "firstParty",
    "analyticsDisabled": False,
    "loginExpiresAt": None,
    "consoleProfileSignInOffered": True,
}

PRIVATE_WORDS = "Access denied for reader@example.org on key sk-ant-api03-0123456789abcdefghij; contact your administrator"


def write_note(note: dict) -> None:
    """Add one line about this run to the notes file, if a test asked for one."""
    notes_path = os.environ.get("STAND_IN_NOTES")
    if not notes_path:
        return
    with open(notes_path, "a", encoding="utf-8") as notes:
        notes.write(json.dumps(note) + "\n")


def files_in(folder: str) -> dict:
    """The name and text of every file in ``folder``."""
    found = {}
    for path in sorted(Path(folder).iterdir()):
        if path.is_file():
            found[path.name] = path.read_bytes().decode("utf-8", errors="replace")
    return found


def places_seen(arguments: list, request: str, files: dict) -> dict:
    """For each word a test asked to look for, where this run was handed it."""
    seen = {}
    for word in os.environ.get("STAND_IN_LOOK_FOR", "").splitlines():
        if not word:
            continue
        places = []
        if any(word in argument for argument in arguments):
            places.append("arguments")
        if any(word in value for name, value in os.environ.items() if not name.startswith("STAND_IN_")):
            places.append("environment")
        if word in request:
            places.append("input")
        if any(word in text for text in files.values()):
            places.append("files")
        seen[word] = places
    return seen


def every_paragraph_is_story(request: str) -> str:
    """The answer "none of these is a note" for the paragraphs in ``request``."""
    count = request.count("<paragraph n=")
    return json.dumps({str(number): False for number in range(1, count + 1)})


def captured_lines(name: str) -> list:
    """The lines of one captured run, as objects."""
    return [json.loads(line) for line in (CAPTURED / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def print_lines(lines: list) -> None:
    out = sys.stdout.buffer
    for line in lines:
        out.write(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n")
    out.flush()


def with_answer(lines: list, answer: str) -> list:
    """A captured successful run with the model's words replaced by ``answer``, in both places the real program prints them."""
    for line in lines:
        if line.get("type") == "assistant":
            line["message"]["content"] = [{"type": "text", "text": answer}]
        if line.get("type") == "result":
            line["result"] = answer
    return lines


def start_something_that_holds_the_output(away: bool = False) -> int:
    """Start a program that sleeps, with this one's outputs, and return its process number. With ``away``, in a session of its own, for a shorter time: nothing will stop it but the test that asked for it."""
    seconds = 40 if away else 120
    child = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
        start_new_session=away,
    )
    return child.pid


def main() -> int:
    arguments = sys.argv[1:]
    behaviour = os.environ.get("STAND_IN_BEHAVIOUR", "answers")
    note = {
        "arguments": arguments,
        "folder": os.getcwd(),
        "environment_names": sorted(os.environ),
        "process": os.getpid(),
    }

    if arguments == ["--version"]:
        write_note(dict(note, kind="version", files=files_in(os.getcwd())))
        if behaviour == "another-program":
            print("Claude 1.4.2")
        else:
            print("2.1.332 (Claude Code)")
        return 0

    if arguments[:2] == ["auth", "status"]:
        write_note(dict(note, kind="sign-in"))
        if behaviour == "not-signed-in":
            print(json.dumps(SIGNED_OUT_STATUS, indent=2))
            return 1
        print(json.dumps(SIGNED_IN_STATUS, indent=2))
        return 0

    if behaviour == "never-reads":
        write_note(dict(note, kind="request", input=None, files=files_in(os.getcwd())))
        time.sleep(120)
        return 0

    request = sys.stdin.buffer.read().decode("utf-8")
    note.update(kind="request", input=request, files=files_in(os.getcwd()))
    note["seen"] = places_seen(arguments, request, note["files"])

    if behaviour == "never-answers":
        write_note(dict(note, started=start_something_that_holds_the_output()))
        time.sleep(120)
        return 0
    if behaviour == "something-gets-away":
        write_note(dict(note, started=start_something_that_holds_the_output(away=True)))
        time.sleep(120)
        return 0
    if behaviour == "leaves-something-running":
        write_note(dict(note, started=start_something_that_holds_the_output()))
        return 0

    write_note(note)

    if behaviour == "prints-far-too-much":
        chunk = b"x" * 65536
        for _ in range(512):
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        time.sleep(120)
        return 0
    if behaviour == "too-old":
        sys.stderr.write((CAPTURED / "unknown_option.stderr").read_text(encoding="utf-8"))
        return 1
    if behaviour == "prints-nonsense":
        print("Segmentation fault (core dumped)")
        return 139
    if behaviour == "says-nothing":
        return 1
    if behaviour == "not-signed-in":
        print_lines(captured_lines("not_signed_in.jsonl"))
        return 1
    if behaviour == "unknown-model":
        sys.stderr.write((CAPTURED / "unknown_model.stderr").read_text(encoding="utf-8"))
        print_lines(captured_lines("unknown_model.jsonl"))
        return 1
    if behaviour == "fails-with-private-words":
        lines = captured_lines("not_signed_in.jsonl")
        for line in lines:
            if line.get("type") == "result":
                line["result"] = PRIVATE_WORDS
                line["api_error_status"] = 403
        sys.stderr.write(PRIVATE_WORDS + "\n")
        print_lines(lines)
        return 1

    answer = os.environ.get("STAND_IN_REPLY") or every_paragraph_is_story(request)
    if behaviour == "answers-nothing":
        answer = ""
    lines = with_answer(captured_lines("answered.jsonl"), answer)
    if behaviour == "uses-an-api-key":
        lines[0] = captured_lines("api_key_in_use.init.jsonl")[0]
    if behaviour == "uses-another-service":
        lines[0]["served_by_anthropic"] = False
    if behaviour == "does-not-say-how-it-signed-in":
        lines = [line for line in lines if line.get("type") != "system"]
    print_lines(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
