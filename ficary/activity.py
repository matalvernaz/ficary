"""What Ficary is doing right now, a sentence per job, for the windows' status bars.

A library update used to put every chapter of every story into its status pane, so finding which story was being updated meant arrowing through hundreds of chapter lines with a screen reader. The chapter-by-chapter progress lives here now instead. Each running job keeps one sentence saying what it is doing ("Updating 3 of 12: "Title", chapter 21 of 25."), and each window's status bar shows them, so NVDA+End answers "what is it doing?" at any moment without moving focus.

Jobs on any thread start, change and end their sentences; the windows read the result on a timer. Nothing here touches wx.
"""

from __future__ import annotations

import itertools
import threading

_lock = threading.Lock()

# Each running job's sentence, by job number, in the order the jobs started.
_running: dict[int, str] = {}

_job_numbers = itertools.count(1)

# What the last job to finish said about how it went. Shown while nothing is running, so someone coming back to the window can still ask how the update went; cleared as soon as another job starts, since it is old news by then.
_last_result = ""


class Job:
    """One running job's sentence in the status bars.

    Use it as a ``with`` block, so the sentence goes away however the job ends: finished, cancelled or failed. A sentence left behind would say something is still happening when nothing is.
    """

    def __init__(self, sentence: str):
        global _last_result
        with _lock:
            self._number = next(_job_numbers)
            _running[self._number] = sentence
            _last_result = ""

    def say(self, sentence: str) -> None:
        """Replace this job's sentence. Does nothing once the job has ended, so a late progress report can't bring it back."""
        with _lock:
            if self._number in _running:
                _running[self._number] = sentence

    def end(self, result: str = "") -> None:
        """Take this job's sentence away. ``result``, if given, is shown while nothing else is running, until the next job starts."""
        global _last_result
        with _lock:
            was_running = _running.pop(self._number, None) is not None
            if was_running and result:
                _last_result = result

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc_info) -> None:
        self.end()


def finished(result: str) -> None:
    """Say how something went once it is over, for a job whose sentence has already ended (an update run reports its counts after its last story)."""
    global _last_result
    with _lock:
        _last_result = result


def current() -> str:
    """Every running job's sentence, oldest first. With none running, the last result; with no result either, an empty string."""
    with _lock:
        if _running:
            return " ".join(_running.values())
        return _last_result


def reset() -> None:
    """Forget every job and result. For tests, which share this one record."""
    global _last_result
    with _lock:
        _running.clear()
        _last_result = ""
