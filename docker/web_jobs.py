"""Keeps track of the web page's jobs: which are waiting, which are running, and when a finished one and its file are thrown away.

There are two kinds, each with a registry of its own. An audiobook can take an hour; an EPUB, HTML or TXT file takes seconds to a few minutes, and must not wait behind one. Text files used to be made while the page's request waited, and a long story could outlast Cloudflare's limit of 100 seconds for one request, which ends the download with an error page.

It exists apart from app.py so it can be tested without the web framework. Before it, every job started its own thread at once and nothing was ever removed: each finished audiobook stayed in the container's temporary folder, and each job stayed in memory, until the container was replaced. Two audiobooks at once could also need more memory than a small server has.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("ficary-web")

# The states a job passes through. "queued" until a worker picks it up, then "downloading" and "synthesizing", ending in "done" or "error".
STATUS_QUEUED = "queued"
STATUS_DONE = "done"
STATUS_ERROR = "error"
FINISHED_STATUSES = (STATUS_DONE, STATUS_ERROR)

DEFAULT_MAX_RUNNING_JOBS = 1
DEFAULT_KEEP_FINISHED_HOURS = 24.0
SECONDS_PER_HOUR = 3600.0

# How often the background sweep looks for finished jobs that are old enough to remove.
SWEEP_INTERVAL_SECONDS = 600.0

# The server's working folder, made inside whichever folder the FICARY_WEB_WORK_DIR setting names. This one folder is all the server ever empties, so a setting that points at a folder with other things in it costs those things nothing.
WORK_FOLDER_NAME = "ficary-web-work"


@dataclass
class Job:
    id: str
    url: str
    format: str
    # The rest of these are for audiobooks; a text file's job leaves them as they are.
    attribution: str = ""
    model_size: str = ""
    speech_rate: int = 0
    strip_notes: bool = False
    hr_as_stars: bool = False
    status: str = STATUS_QUEUED  # queued | downloading | synthesizing | done | error
    stage: str = ""
    current: int = 0
    total: int = 0
    detail: str = ""
    error: str | None = None
    path: str | None = None
    # The temporary folder the job writes into. Removed with the job, or straight away if the job fails.
    folder: str | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    finished: float | None = None


def positive_number(text: str | None, default: float) -> float:
    """Read a setting that has to be an ordinary number above zero. Anything else (missing, not a number, zero or less, "inf", "nan") gives the default, because a server that starts with a guessed setting is better than one that does not start."""
    if text is None or not text.strip():
        return default
    try:
        value = float(text)
    except ValueError:
        logger.warning("Setting %r is not a number; using %s", text, default)
        return default
    # float() accepts "inf" and "nan". Neither can be turned into a count of jobs, and "nan" compares false with everything, so it would get past the test below.
    if not math.isfinite(value) or value <= 0:
        logger.warning("Setting %r is not a number above zero; using %s", text, default)
        return default
    return value


def hours_in_words(hours: float) -> str:
    """How long finished audiobooks are kept, as the page says it: "24 hours", "1 hour", "1.5 hours"."""
    number = f"{hours:g}"
    return f"{number} hour" if number == "1" else f"{number} hours"


def prepare_work_folder(parent: str) -> Path | None:
    """Make the server's working folder inside ``parent``, remove whatever a previous run left in it, and return it.

    Everything the server writes while it works belongs in this folder: a story being exported, an audiobook waiting to be downloaded, the scratch files of a render. All of it can only be reached through a job the server remembers, and it remembers none across a restart. So whatever is in the folder when the server starts can never be downloaded or reused. Before this, a render that was cut short (the container out of memory, the machine restarted) left its files behind until the container was replaced.

    One server process per folder: a second one starting would empty the folder under the first.

    Returns None, with the reason logged, when the folder cannot be used. The server then carries on with the system's own temporary folder.
    """
    folder = Path(parent) / WORK_FOLDER_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        leftovers = list(folder.iterdir())
    except OSError as error:
        logger.warning("Cannot use %s for working files (%s); using the system temporary folder", folder, error)
        return None
    if not os.access(folder, os.W_OK | os.X_OK):
        # It exists but belongs to someone else, as it would if an image build had made it as root.
        logger.warning("Cannot write to %s; using the system temporary folder", folder)
        return None
    for leftover in leftovers:
        _remove_leftover(leftover)
    still_there = [leftover.name for leftover in leftovers if os.path.lexists(leftover)]
    removed_count = len(leftovers) - len(still_there)
    if removed_count:
        logger.info("Removed %d item(s) a previous run left in %s", removed_count, folder)
    if still_there:
        logger.warning("Could not remove %d item(s) from %s: %s", len(still_there), folder, ", ".join(sorted(still_there)[:5]))
    return folder


def _remove_leftover(leftover: Path) -> None:
    """Remove one thing from the working folder. A link is removed as a link: what it points at is outside the folder and not the server's to delete."""
    try:
        if leftover.is_symlink() or not leftover.is_dir():
            leftover.unlink()
        else:
            shutil.rmtree(leftover, ignore_errors=True)
    except OSError:
        logger.warning("Could not remove %s", leftover, exc_info=True)


class JobRegistry:
    """The jobs the page knows about, and the workers that run them.

    ``run_job`` does the work of one job and must set its status to "done" or "error" itself (or raise, which counts as an error). At most ``max_running`` jobs run at a time; the rest wait their turn in the order they arrived. A finished job, and the folder its audiobook is in, is kept for ``keep_finished_seconds`` so it can be downloaded, then removed.
    """

    def __init__(
        self,
        run_job: Callable[[Job], None],
        *,
        max_running: int = DEFAULT_MAX_RUNNING_JOBS,
        keep_finished_seconds: float = DEFAULT_KEEP_FINISHED_HOURS * SECONDS_PER_HOUR,
        clock: Callable[[], float] = time.time,
        remove_folder: Callable[[str], None] | None = None,
        name: str = "audio",
    ) -> None:
        # Names the worker threads and the log lines: "audio" or "text".
        self._name = name
        self._run_job = run_job
        self._max_running = max(1, int(max_running))
        self._keep_finished_seconds = keep_finished_seconds
        self._clock = clock
        self._remove_folder = remove_folder or self._remove_folder_from_disk
        self._jobs: dict[str, Job] = {}
        self._waiting: "queue.Queue[Job]" = queue.Queue()
        self._waiting_ids: list[str] = []
        self._running_ids: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ── Looking jobs up ───────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs_ahead(self, job_id: str) -> int:
        """How many jobs have to finish or start before this one gets its turn: the ones running now plus the ones waiting in front of it. Zero once it is running or finished, and for a job that is not known."""
        with self._lock:
            if job_id not in self._waiting_ids:
                return 0
            return len(self._running_ids) + self._waiting_ids.index(job_id)

    def update(self, job: Job, **fields: Any) -> None:
        """Change a job's fields. Done under the lock so a poll never reads a job half-updated."""
        with self._lock:
            for name, value in fields.items():
                setattr(job, name, value)
            now = self._clock()
            job.updated = now
            if job.status in FINISHED_STATUSES and job.finished is None:
                job.finished = now

    # ── Adding and running ────────────────────────────────────────

    def add(self, job: Job) -> None:
        """Register a job and queue it. Also a convenient moment to clear out old ones."""
        self.sweep()
        with self._lock:
            # Stamped from the registry's own clock, so every time on a job is read off the same one.
            job.created = self._clock()
            job.updated = job.created
            self._jobs[job.id] = job
            self._waiting_ids.append(job.id)
            self._waiting.put(job)
            self._start_workers_if_needed()

    def _start_workers_if_needed(self) -> None:
        """Start worker threads up to the limit. Called with the lock held."""
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        while len(self._workers) < self._max_running:
            worker = threading.Thread(
                target=self._work,
                daemon=True,
                name=f"{self._name}-worker-{len(self._workers) + 1}",
            )
            self._workers.append(worker)
            worker.start()

    def _work(self) -> None:
        """One worker: take the next waiting job, run it, repeat. Never exits, so the count of workers stays at the limit."""
        while True:
            job = self._waiting.get()
            with self._lock:
                if job.id in self._waiting_ids:
                    self._waiting_ids.remove(job.id)
                self._running_ids.add(job.id)
            try:
                self._run_job(job)
            except Exception as error:  # noqa: BLE001 - a failed job must not take the worker down with it
                logger.exception("%s job %s failed", self._name, job.id)
                self.update(job, status=STATUS_ERROR, error=str(error))
            finally:
                with self._lock:
                    self._running_ids.discard(job.id)
            if job.status not in FINISHED_STATUSES:
                # The job function returned without saying how it ended. Treat that as a failure rather than leave the page waiting forever.
                self.update(job, status=STATUS_ERROR, error="The job stopped without a result.")
            if job.status == STATUS_ERROR:
                # Nothing to download from a failed job, so its half-written files go now.
                self._discard_folder(job)

    # ── Clearing out ──────────────────────────────────────────────

    def sweep(self) -> list[str]:
        """Remove finished jobs that have been kept long enough, with their folders. Returns the ids removed."""
        now = self._clock()
        with self._lock:
            expired = [
                job for job in self._jobs.values()
                if job.finished is not None and now - job.finished >= self._keep_finished_seconds
            ]
            for job in expired:
                del self._jobs[job.id]
        for job in expired:
            self._discard_folder(job)
        if expired:
            logger.info("Removed %d finished %s job(s) kept past their time", len(expired), self._name)
        return [job.id for job in expired]

    def _discard_folder(self, job: Job) -> None:
        folder = job.folder
        if not folder:
            return
        job.folder = None
        try:
            self._remove_folder(folder)
        except OSError:
            logger.warning("Could not remove %s", folder, exc_info=True)

    @staticmethod
    def _remove_folder_from_disk(folder: str) -> None:
        shutil.rmtree(folder, ignore_errors=True)

    def start_sweeping(
        self,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
    ) -> threading.Thread:
        """Sweep on a timer, in a background thread, so audiobooks are cleared out even when nobody visits the page. The server never stops it; ``stop`` is there so a test can."""
        stop_requested = stop if stop is not None else threading.Event()

        def sweep_until_stopped() -> None:
            # Event.wait returns False when the time runs out and True once the event is set.
            while not stop_requested.wait(interval_seconds):
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001 - the timer has to survive one bad pass
                    logger.exception("%s job sweep failed", self._name)

        thread = threading.Thread(target=sweep_until_stopped, daemon=True, name=f"{self._name}-job-sweep")
        thread.start()
        return thread


# ── Text files ────────────────────────────────────────────────────

# What a finished text file is sent as.
TEXT_MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "html": "text/html; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


def run_text_job(
    job: Job,
    *,
    registry: JobRegistry,
    download_story: Callable[[str, Callable[..., None]], Any],
    export: Callable[[str, Any, str], str],
    make_folder: Callable[[Job], Path],
) -> None:
    """Make one EPUB, HTML or TXT file: download the story, reporting each chapter, then export it into a folder of the job's own, where it waits to be collected.

    ``download_story(url, progress)`` and ``export(format, story, folder)`` are the real download and exporter in the server and stand-ins in a test. A failure is left to raise: the registry marks the job as an error with its message and removes its folder.
    """
    registry.update(job, status="downloading", stage="fetching story")

    def progress(current: int, total: int, title: str, cached: bool) -> None:
        registry.update(job, current=current, total=total, detail=f"{title}{' (cached)' if cached else ''}")

    story = download_story(job.url, progress)
    folder = make_folder(job)
    # Recorded before the file is written, so a failed export's folder goes with the job.
    registry.update(job, folder=str(folder), stage="making the file", detail="")
    path = Path(export(job.format, story, str(folder)))
    registry.update(job, status=STATUS_DONE, path=str(path), stage="complete", detail=path.name)
