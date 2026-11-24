"""Tests for the web page's audiobook job registry (docker/web_jobs.py).

The web page used to start every audiobook job on its own thread the moment it was asked for, and never removed anything: finished audiobooks stayed in the container's temporary folder and jobs stayed in memory until the container was replaced. On a small server two audiobooks at once can also need more memory than there is.
"""

from __future__ import annotations

import importlib.util
import os
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


class TestTheWorkFolder:
    """Whatever is in the working folder when the server starts belongs to jobs the server has forgotten, so nobody can ever download it."""

    def test_it_is_made_inside_the_folder_the_setting_names(self, tmp_path):
        folder = web_jobs.prepare_work_folder(str(tmp_path))
        assert folder == tmp_path / web_jobs.WORK_FOLDER_NAME
        assert folder.is_dir()

    def test_what_a_previous_run_left_is_removed(self, tmp_path):
        folder = tmp_path / web_jobs.WORK_FOLDER_NAME
        (folder / "ficary-abc123-xyz" / "ffn-audiobook-1").mkdir(parents=True)
        (folder / "ficary-abc123-xyz" / "book.m4b").write_bytes(b"audio")
        (folder / "ffn-tts-123").mkdir()
        (folder / "ffn-tts-123" / "segment.mp3").write_bytes(b"audio")
        (folder / "stray.txt").write_text("x", encoding="utf-8")
        assert web_jobs.prepare_work_folder(str(tmp_path)) == folder
        assert list(folder.iterdir()) == []

    def test_nothing_beside_the_working_folder_is_touched(self, tmp_path):
        """The setting names the folder the working folder is made in. Pointed at a folder of models, it must cost the models nothing."""
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "big.bin").write_bytes(b"model")
        (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
        web_jobs.prepare_work_folder(str(tmp_path))
        assert (tmp_path / "models" / "big.bin").read_bytes() == b"model"
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "keep"
        assert sorted(entry.name for entry in tmp_path.iterdir()) == [web_jobs.WORK_FOLDER_NAME, "models", "notes.txt"]

    def test_a_link_is_removed_but_not_what_it_points_at(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        folder = tmp_path / web_jobs.WORK_FOLDER_NAME
        folder.mkdir()
        (folder / "link-to-folder").symlink_to(outside, target_is_directory=True)
        (folder / "link-to-file").symlink_to(outside / "precious.txt")
        (folder / "link-to-nothing").symlink_to(tmp_path / "nowhere")
        web_jobs.prepare_work_folder(str(tmp_path))
        assert list(folder.iterdir()) == []
        assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep"

    def test_a_folder_that_cannot_be_made_gives_none_and_does_not_raise(self, tmp_path):
        a_file = tmp_path / "a-file"
        a_file.write_text("x", encoding="utf-8")
        assert web_jobs.prepare_work_folder(str(a_file)) is None

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a user that file permissions apply to")
    def test_a_folder_the_server_cannot_write_to_gives_none(self, tmp_path):
        """What an image build leaves if it makes the folder as root: there, but not the server's to write in."""
        folder = tmp_path / web_jobs.WORK_FOLDER_NAME
        folder.mkdir()
        folder.chmod(0o500)
        try:
            assert web_jobs.prepare_work_folder(str(tmp_path)) is None
        finally:
            folder.chmod(0o700)

    def test_starting_again_with_nothing_left_is_fine(self, tmp_path):
        first = web_jobs.prepare_work_folder(str(tmp_path))
        second = web_jobs.prepare_work_folder(str(tmp_path))
        assert first == second
        assert second.is_dir()


class TestSettings:
    @pytest.mark.parametrize(
        "hours, words",
        [(24.0, "24 hours"), (1.0, "1 hour"), (1.5, "1.5 hours"), (0.5, "0.5 hours"), (72.0, "72 hours")],
    )
    def test_the_keep_time_in_words(self, hours, words):
        assert web_jobs.hours_in_words(hours) == words

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

    def test_the_working_folder_is_set_up_when_the_server_starts_and_not_on_import(self):
        """The image build imports the page as root. A folder made then belongs to root, and every job the real server ran would fail to write."""
        lines = self.page_source().splitlines()
        calls = [line for line in lines if line.strip() == "use_work_folder()"]
        assert len(calls) == 1
        assert calls[0].startswith("    "), "the call must sit inside the start-up function, not at the top level"
        assert "lifespan=_lifespan" in self.page_source()

    def test_the_image_names_the_working_folder_after_switching_user(self):
        dockerfile = (DOCKER_FOLDER / "Dockerfile").read_text(encoding="utf-8")
        assert "FICARY_WEB_WORK_DIR=/tmp" in dockerfile
        assert dockerfile.index("USER ficary") < dockerfile.index("FICARY_WEB_WORK_DIR=/tmp")
        import_step = dockerfile.index('RUN python -c "import app')
        assert import_step < dockerfile.index("FICARY_WEB_WORK_DIR=/tmp"), "the build's import of the page must not see the setting"

    def test_only_not_found_means_the_job_is_gone(self):
        """A dropped connection or a server error in the middle of an hour-long render used to end the page's checks and tell the user to start again."""
        source = self.page_source()
        assert 'if (r.status === 404) return "gone";' in source
        assert "if (!r.ok) return null;" in source
        assert "Lost job" not in source

    def test_the_page_keeps_checking_after_an_unanswered_check(self):
        source = self.page_source()
        unanswered = source[source.index("if (j === null) {"):]
        unanswered = unanswered[: unanswered.index("unanswered = 0;")]
        assert "continue;" in unanswered
        assert "return;" not in unanswered

    def test_the_page_picks_its_audiobook_back_up_after_a_reload(self):
        source = self.page_source()
        assert "rememberJob(id);" in source
        assert "const earlierJob = rememberedJob();" in source
        assert "poll(earlierJob);" in source

    def test_the_keep_time_on_the_page_is_the_servers_own(self):
        source = self.page_source()
        assert 'LANDING = LANDING.replace("__KEEP_HOURS__", web_jobs.hours_in_words(KEEP_AUDIOBOOKS_HOURS))' in source

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


# ── Text files (EPUB, HTML, TXT) as jobs ─────────────────────────


def _folder_maker(tmp_path):
    def make(job):
        folder = tmp_path / f"job-{job.id}"
        folder.mkdir()
        return folder
    return make


class TestTextFiles:
    """The page used to make a text file while its request waited, and a
    long story could outlast Cloudflare's 100 seconds for one request.
    It is a job now, in a queue of its own."""

    def test_the_story_is_downloaded_then_the_file_made_in_a_folder_of_its_own(self, tmp_path):
        registry = web_jobs.JobRegistry(lambda job: None, name="text")
        job = web_jobs.Job(id="t1", url="https://example.invalid/s/1", format="epub")
        seen = []

        def download_story(url, progress):
            seen.append(("download", url, job.status, job.stage))
            progress(1, 2, "One", False)
            progress(2, 2, "Two", True)
            seen.append(("progress", job.current, job.total, job.detail))
            return "the story"

        def export(format, story, folder):
            # The folder is on the job before the file is written, so a
            # failed export's folder is removed with the job.
            seen.append(("export", format, story, job.folder == folder, job.stage))
            path = Path(folder) / "Story - Author.epub"
            path.write_bytes(b"epub")
            return str(path)

        web_jobs.run_text_job(job, registry=registry, download_story=download_story, export=export, make_folder=_folder_maker(tmp_path))
        assert seen == [
            ("download", "https://example.invalid/s/1", "downloading", "fetching story"),
            ("progress", 2, 2, "Two (cached)"),
            ("export", "epub", "the story", True, "making the file"),
        ]
        assert job.status == "done"
        assert job.path == str(tmp_path / "job-t1" / "Story - Author.epub")
        assert job.detail == "Story - Author.epub"

    def test_a_failed_export_is_an_error_and_its_folder_goes(self, tmp_path):
        removed = []
        holder = []

        def run(job):
            def export(format, story, folder):
                raise OSError("No space left on device")

            web_jobs.run_text_job(job, registry=holder[0], download_story=lambda url, progress: "the story", export=export, make_folder=_folder_maker(tmp_path))

        registry = web_jobs.JobRegistry(run, name="text", remove_folder=removed.append)
        holder.append(registry)
        job = web_jobs.Job(id="t2", url="https://example.invalid/s/2", format="txt")
        registry.add(job)
        assert wait_until(lambda: job.status == "error")
        assert "No space left on device" in job.error
        assert wait_until(lambda: removed == [str(tmp_path / "job-t2")])

    def test_its_workers_are_named_for_their_kind(self):
        started = threading.Event()
        names = []

        def run(job):
            names.append(threading.current_thread().name)
            job.status = "done"
            started.set()

        registry = web_jobs.JobRegistry(run, name="text")
        registry.add(web_jobs.Job(id="t3", url="https://example.invalid/s/3", format="html"))
        assert started.wait(5)
        assert names == ["text-worker-1"]

    def test_every_text_format_is_sent_as_what_it_is(self):
        assert web_jobs.TEXT_MEDIA_TYPES == {
            "epub": "application/epub+zip",
            "html": "text/html; charset=utf-8",
            "txt": "text/plain; charset=utf-8",
        }


class TestThePageFollowsTextDownloads:
    """Checked as text like the rest of the page; the real browser run is in the commit message."""

    def page_source(self) -> str:
        return (DOCKER_FOLDER / "app.py").read_text(encoding="utf-8")

    def test_text_files_have_a_queue_of_their_own_that_the_server_sweeps(self):
        source = self.page_source()
        assert "TEXT_JOBS = web_jobs.JobRegistry(" in source
        assert "TEXT_JOBS.start_sweeping()" in [line.strip() for line in source.splitlines()]
        assert "for registry in (JOBS, TEXT_JOBS):" in source

    def test_the_jobs_endpoint_takes_text_formats(self):
        source = self.page_source()
        start = source[source.index("def start_job("):]
        start = start[: start.index("@app.get")]
        assert "if format in TEXT_FORMATS:" in start
        assert "TEXT_JOBS.add(text_job)" in start

    def test_the_form_follows_a_job_and_still_works_without_the_script(self):
        source = self.page_source()
        assert '<form id="text-form" method="post" action="/download" autocomplete="off">' in source
        handler = source[source.index('$("#text-form").addEventListener("submit"'):]
        handler = handler[: handler.index("\n});")]
        assert "e.preventDefault();" in handler
        assert 'fetch("/jobs", { method: "POST", body: fd })' in handler
        assert "followTextJob(id);" in handler

    def test_a_finished_file_is_handed_over_once(self):
        """Forgotten before the browser is sent for it, so a reload never downloads it twice."""
        source = self.page_source()
        follow = source[source.index("async function followTextJob(id) {"):]
        done = follow[follow.index('if (j.status === "done") {'):]
        done = done[: done.index("return;")]
        assert done.index("forgetTextJob();") < done.index("window.location.assign(result);")

    def test_only_not_found_means_a_download_is_gone(self):
        source = self.page_source()
        follow = source[source.index("async function followTextJob(id) {"):]
        assert "const j = await askAboutJob(id);" in follow
        unanswered = follow[follow.index("if (j === null) {"):]
        unanswered = unanswered[: unanswered.index("unanswered = 0;")]
        assert "continue;" in unanswered and "return;" not in unanswered

    def test_its_status_area_is_not_a_live_region_and_there_is_still_one_announcer(self):
        source = self.page_source()
        assert '<div id="text-status"></div>' in source
        assert source.count('role="status"') == 1
        follow = source[source.index("async function followTextJob(id) {"):]
        assert "announce(textSentence(j));" in follow
