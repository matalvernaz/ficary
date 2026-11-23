"""Tests for the web page's audiobook job registry (docker/web_jobs.py).

The web page used to start every audiobook job on its own thread the moment it was asked for, and never removed anything: finished audiobooks stayed in the container's temporary folder and jobs stayed in memory until the container was replaced. On a small server two audiobooks at once can also need more memory than there is.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

# The module lives beside the web page, outside the package, so it is loaded by path. It has to be registered under its name before it runs: its dataclass looks its own module up by name while it is being defined.
MODULE_PATH = Path(__file__).resolve().parents[1] / "docker" / "web_jobs.py"
spec = importlib.util.spec_from_file_location("web_jobs", MODULE_PATH)
web_jobs = importlib.util.module_from_spec(spec)
sys.modules["web_jobs"] = web_jobs
spec.loader.exec_module(web_jobs)

HOUR = web_jobs.SECONDS_PER_HOUR


def make_job(job_id: str, folder: str | None = None):
    return web_jobs.Job(
        id=job_id, url="https://example.invalid/story", format="audio",
        attribution="builtin", model_size="", speech_rate=0,
        strip_notes=False, hr_as_stars=True, folder=folder,
    )


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class HeldJobs:
    """A job function that starts each job and then holds it until the test lets it go, so the test can look at what is running and what is waiting."""

    def __init__(self, registry_holder: list):
        self.registry_holder = registry_holder
        self.started: list[str] = []
        self.release = {}
        self.lock = threading.Lock()

    def gate(self, job_id: str) -> threading.Event:
        with self.lock:
            return self.release.setdefault(job_id, threading.Event())

    def __call__(self, job) -> None:
        with self.lock:
            self.started.append(job.id)
        self.gate(job.id).wait(10)
        self.registry_holder[0].update(job, status=web_jobs.STATUS_DONE)

    def finish(self, job_id: str) -> None:
        self.gate(job_id).set()


def wait_until(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def registry_with_held_jobs(**options):
    holder: list = []
    held = HeldJobs(holder)
    registry = web_jobs.JobRegistry(held, **options)
    holder.append(registry)
    return registry, held


class TestOneAtATime:
    def test_a_second_audiobook_waits_for_the_first(self):
        registry, held = registry_with_held_jobs()
        first, second = make_job("first"), make_job("second")
        registry.add(first)
        registry.add(second)
        assert wait_until(lambda: held.started == ["first"])
        time.sleep(0.15)
        assert held.started == ["first"]
        assert second.status == web_jobs.STATUS_QUEUED
        held.finish("first")
        assert wait_until(lambda: held.started == ["first", "second"])
        held.finish("second")
        assert wait_until(lambda: second.status == web_jobs.STATUS_DONE)

    def test_jobs_run_in_the_order_they_were_asked_for(self):
        registry, held = registry_with_held_jobs()
        for name in ("a", "b", "c"):
            registry.add(make_job(name))
        for name in ("a", "b", "c"):
            assert wait_until(lambda: name in held.started)
            held.finish(name)
        assert held.started == ["a", "b", "c"]

    def test_a_waiting_job_knows_how_many_are_ahead_of_it(self):
        registry, held = registry_with_held_jobs()
        for name in ("a", "b", "c"):
            registry.add(make_job(name))
        assert wait_until(lambda: held.started == ["a"])
        assert registry.jobs_ahead("a") == 0
        assert registry.jobs_ahead("b") == 1
        assert registry.jobs_ahead("c") == 2
        held.finish("a")
        assert wait_until(lambda: held.started == ["a", "b"])
        assert registry.jobs_ahead("c") == 1
        held.finish("b")
        held.finish("c")

    def test_a_server_with_room_can_run_two(self):
        registry, held = registry_with_held_jobs(max_running=2)
        for name in ("a", "b", "c"):
            registry.add(make_job(name))
        assert wait_until(lambda: sorted(held.started) == ["a", "b"])
        time.sleep(0.15)
        assert sorted(held.started) == ["a", "b"]
        assert registry.jobs_ahead("c") == 2
        for name in ("a", "b", "c"):
            held.finish(name)

    def test_a_limit_below_one_still_runs_jobs(self):
        registry, held = registry_with_held_jobs(max_running=0)
        registry.add(make_job("a"))
        assert wait_until(lambda: held.started == ["a"])
        held.finish("a")


class TestFailedJobs:
    def test_a_job_that_raises_is_an_error_and_the_next_one_still_runs(self):
        removed: list[str] = []
        ran: list[str] = []
        holder: list = []

        def run(job):
            ran.append(job.id)
            if job.id == "bad":
                raise RuntimeError("the story site refused")
            holder[0].update(job, status=web_jobs.STATUS_DONE)

        registry = web_jobs.JobRegistry(run, remove_folder=removed.append)
        holder.append(registry)
        bad, good = make_job("bad", folder="/tmp/bad-folder"), make_job("good", folder="/tmp/good-folder")
        registry.add(bad)
        registry.add(good)
        assert wait_until(lambda: good.status == web_jobs.STATUS_DONE)
        assert bad.status == web_jobs.STATUS_ERROR
        assert bad.error == "the story site refused"
        assert ran == ["bad", "good"]

    def test_a_failed_jobs_files_go_at_once_and_a_finished_ones_stay(self):
        removed: list[str] = []
        holder: list = []

        def run(job):
            if job.id == "bad":
                holder[0].update(job, status=web_jobs.STATUS_ERROR, error="no")
            else:
                holder[0].update(job, status=web_jobs.STATUS_DONE)

        registry = web_jobs.JobRegistry(run, remove_folder=removed.append)
        holder.append(registry)
        bad, good = make_job("bad", folder="/tmp/bad-folder"), make_job("good", folder="/tmp/good-folder")
        registry.add(bad)
        registry.add(good)
        assert wait_until(lambda: good.status == web_jobs.STATUS_DONE)
        assert removed == ["/tmp/bad-folder"]
        assert good.folder == "/tmp/good-folder"

    def test_a_job_that_ends_without_saying_how_is_an_error(self):
        registry = web_jobs.JobRegistry(lambda job: None)
        job = make_job("silent")
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_ERROR)
        assert "without a result" in job.error


class TestClearingOut:
    def finished_registry(self, clock, removed):
        holder: list = []

        def run(job):
            holder[0].update(job, status=web_jobs.STATUS_DONE)

        registry = web_jobs.JobRegistry(
            run, clock=clock, keep_finished_seconds=24 * HOUR, remove_folder=removed.append,
        )
        holder.append(registry)
        return registry

    def test_a_finished_audiobook_is_removed_once_it_has_been_kept_long_enough(self):
        clock, removed = Clock(), []
        registry = self.finished_registry(clock, removed)
        job = make_job("old", folder="/tmp/old-folder")
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        clock.now += 24 * HOUR
        assert registry.sweep() == ["old"]
        assert removed == ["/tmp/old-folder"]
        assert registry.get("old") is None

    def test_it_is_still_there_just_before_that(self):
        clock, removed = Clock(), []
        registry = self.finished_registry(clock, removed)
        job = make_job("recent", folder="/tmp/recent-folder")
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        clock.now += 24 * HOUR - 1
        assert registry.sweep() == []
        assert removed == []
        assert registry.get("recent") is job

    def test_the_time_is_counted_from_when_the_job_finished_not_when_it_was_asked_for(self):
        """A long render must not be thrown away the moment it completes."""
        clock, removed = Clock(), []
        registry, held = registry_with_held_jobs(
            clock=clock, keep_finished_seconds=24 * HOUR, remove_folder=removed.append,
        )
        job = make_job("slow", folder="/tmp/slow-folder")
        registry.add(job)
        assert wait_until(lambda: held.started == ["slow"])
        clock.now += 30 * HOUR
        held.finish("slow")
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        assert registry.sweep() == []
        clock.now += 24 * HOUR
        assert registry.sweep() == ["slow"]

    def test_a_job_still_running_or_waiting_is_never_removed(self):
        clock, removed = Clock(), []
        registry, held = registry_with_held_jobs(
            clock=clock, keep_finished_seconds=24 * HOUR, remove_folder=removed.append,
        )
        running, waiting = make_job("running"), make_job("waiting")
        registry.add(running)
        registry.add(waiting)
        assert wait_until(lambda: held.started == ["running"])
        clock.now += 1000 * HOUR
        assert registry.sweep() == []
        assert registry.get("running") is running
        assert registry.get("waiting") is waiting
        held.finish("running")
        held.finish("waiting")

    def test_asking_for_a_new_audiobook_clears_out_old_ones(self):
        clock, removed = Clock(), []
        registry = self.finished_registry(clock, removed)
        old = make_job("old", folder="/tmp/old-folder")
        registry.add(old)
        assert wait_until(lambda: old.status == web_jobs.STATUS_DONE)
        clock.now += 25 * HOUR
        registry.add(make_job("new"))
        assert registry.get("old") is None
        assert removed == ["/tmp/old-folder"]

    def test_the_real_folder_and_its_audiobook_are_deleted(self, tmp_path):
        clock = Clock()
        holder: list = []
        folder = tmp_path / "ficary-job"
        folder.mkdir()
        (folder / "book.m4b").write_bytes(b"audio")

        def run(job):
            holder[0].update(job, status=web_jobs.STATUS_DONE, path=str(folder / "book.m4b"))

        registry = web_jobs.JobRegistry(run, clock=clock, keep_finished_seconds=HOUR)
        holder.append(registry)
        job = make_job("real", folder=str(folder))
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        assert folder.exists()
        clock.now += HOUR
        registry.sweep()
        assert not folder.exists()

    def test_a_folder_that_cannot_be_removed_does_not_stop_the_sweep(self):
        clock = Clock()
        holder: list = []

        def run(job):
            holder[0].update(job, status=web_jobs.STATUS_DONE)

        def refuse(folder):
            raise OSError("device busy")

        registry = web_jobs.JobRegistry(run, clock=clock, keep_finished_seconds=HOUR, remove_folder=refuse)
        holder.append(registry)
        job = make_job("stuck", folder="/tmp/stuck")
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        clock.now += HOUR
        assert registry.sweep() == ["stuck"]


class TestTheTimer:
    """On a server nobody visits, the timer is the only thing that frees the disk: the other sweep only runs when somebody asks for a new audiobook."""

    def test_the_timer_removes_old_audiobooks_with_nobody_asking(self):
        clock, removed = Clock(), []
        holder: list = []

        def run(job):
            holder[0].update(job, status=web_jobs.STATUS_DONE)

        registry = web_jobs.JobRegistry(run, clock=clock, keep_finished_seconds=HOUR, remove_folder=removed.append)
        holder.append(registry)
        job = make_job("old", folder="/tmp/old-folder")
        registry.add(job)
        assert wait_until(lambda: job.status == web_jobs.STATUS_DONE)
        clock.now += HOUR
        stop = threading.Event()
        timer = registry.start_sweeping(interval_seconds=0.01, stop=stop)
        try:
            assert wait_until(lambda: registry.get("old") is None)
            assert removed == ["/tmp/old-folder"]
        finally:
            stop.set()
            timer.join(5)
        assert not timer.is_alive()

    def test_one_bad_pass_does_not_end_the_timer(self):
        clock, removed = Clock(), []
        holder: list = []

        def run(job):
            holder[0].update(job, status=web_jobs.STATUS_DONE)

        def remove(folder):
            if folder == "/tmp/breaks":
                raise RuntimeError("something nobody planned for")
            removed.append(folder)

        registry = web_jobs.JobRegistry(run, clock=clock, keep_finished_seconds=HOUR, remove_folder=remove)
        holder.append(registry)
        breaks = make_job("breaks", folder="/tmp/breaks")
        registry.add(breaks)
        assert wait_until(lambda: breaks.status == web_jobs.STATUS_DONE)
        clock.now += HOUR
        stop = threading.Event()
        timer = registry.start_sweeping(interval_seconds=0.01, stop=stop)
        try:
            assert wait_until(lambda: registry.get("breaks") is None)
            later = make_job("later", folder="/tmp/later")
            registry.add(later)
            assert wait_until(lambda: later.status == web_jobs.STATUS_DONE)
            clock.now += HOUR
            assert wait_until(lambda: registry.get("later") is None)
            assert removed == ["/tmp/later"]
        finally:
            stop.set()
            timer.join(5)

    def test_the_timer_waits_its_interval_before_the_first_pass(self):
        """A pass straight away would be harmless, but a timer that never waits would spin."""
        clock = Clock()
        sweeps: list[float] = []
        registry = web_jobs.JobRegistry(lambda job: None, clock=clock)
        registry.sweep = lambda: sweeps.append(time.monotonic())
        stop = threading.Event()
        timer = registry.start_sweeping(interval_seconds=30, stop=stop)
        time.sleep(0.05)
        stop.set()
        timer.join(5)
        assert not timer.is_alive()
        assert sweeps == []


class TestSettings:
    @pytest.mark.parametrize(
        "text, expected",
        [("3", 3.0), ("1.5", 1.5), (" 48 ", 48.0)],
    )
    def test_a_number_above_zero_is_used(self, text, expected):
        assert web_jobs.positive_number(text, 24.0) == expected

    @pytest.mark.parametrize("text", [None, "", "  ", "many", "0", "-2"])
    def test_anything_else_gives_the_default(self, text):
        assert web_jobs.positive_number(text, 24.0) == 24.0

    @pytest.mark.parametrize("text", ["inf", "-inf", "nan", "NaN", "Infinity", "1e400"])
    def test_a_number_that_cannot_be_counted_with_gives_the_default(self, text):
        """float() takes all of these. As a count of jobs, int() of any of them stopped the server starting."""
        assert web_jobs.positive_number(text, 24.0) == 24.0
        assert int(web_jobs.positive_number(text, 1)) == 1


class TestLookingUp:
    def test_an_unknown_job_is_none_and_has_nothing_ahead_of_it(self):
        registry = web_jobs.JobRegistry(lambda job: None)
        assert registry.get("nope") is None
        assert registry.jobs_ahead("nope") == 0

    def test_every_time_on_a_job_comes_from_the_registrys_clock(self):
        clock = Clock()
        registry, held = registry_with_held_jobs(clock=clock)
        job = make_job("j")
        assert job.created != clock.now
        registry.add(job)
        assert job.created == clock.now
        assert job.updated == clock.now
        held.finish("j")

    def test_the_finish_time_is_set_once(self):
        clock = Clock()
        registry = web_jobs.JobRegistry(lambda job: None, clock=clock)
        job = make_job("j")
        registry.update(job, status=web_jobs.STATUS_DONE)
        first = job.finished
        clock.now += 500
        registry.update(job, detail="still done")
        assert job.finished == first
        assert job.updated == clock.now


# ── The page and the image around the registry ───────────────────

DOCKER_FOLDER = MODULE_PATH.parent


class TestThePageAndTheImage:
    """The web framework is not installed where these tests run, so the page itself is checked as text. A run in a real browser is what showed the page working; these keep its properties from being edited away."""

    def page_source(self) -> str:
        return (DOCKER_FOLDER / "app.py").read_text(encoding="utf-8")

    def test_the_image_includes_the_job_module(self):
        """The page imports it. An image built without it would not start."""
        dockerfile = (DOCKER_FOLDER / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY docker/app.py docker/web_jobs.py /app/" in dockerfile

    def test_jobs_go_through_the_registry_not_a_thread_each(self):
        source = self.page_source()
        assert "JOBS.add(job)" in source
        assert "threading.Thread(target=_run_audio_job" not in source

    def test_the_server_starts_the_timer(self):
        lines = [line.strip() for line in self.page_source().splitlines()]
        assert "JOBS.start_sweeping()" in lines

    def test_there_is_one_status_region_and_it_is_in_the_page_from_the_start(self):
        """A screen reader announces a status region when its text changes, and only reliably if the region was already in the page."""
        source = self.page_source()
        assert source.count('role="status"') == 1
        assert '<p id="announce" role="status" class="visually-hidden"></p>' in source

    def test_the_area_rebuilt_on_every_poll_is_not_a_live_region(self):
        """It is rewritten every two seconds. As a live region that would be speech every two seconds for an hour."""
        source = self.page_source()
        assert '<div id="status"></div>' in source
        assert "aria-live" not in source

    def test_the_same_sentence_is_not_announced_twice_running(self):
        source = self.page_source()
        assert "if (text === lastAnnounced) return;" in source

    def test_the_hidden_region_is_hidden_from_the_eye_only(self):
        """display:none or visibility:hidden would hide it from a screen reader as well."""
        source = self.page_source()
        rule = source[source.index(".visually-hidden {"):]
        rule = rule[: rule.index("}")]
        assert "display" not in rule
        assert "visibility" not in rule
        assert "clip" in rule
