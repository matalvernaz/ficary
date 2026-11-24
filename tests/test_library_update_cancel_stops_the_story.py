"""Cancel in a library update stops the story being downloaded, not just the queue.

Pressing Cancel used to drop the stories still waiting and then report the update as cancelled at once. The story already downloading carried on to its last chapter, which on FanFiction.net's six seconds a chapter can be many minutes, posting chapter lines into the status pane after the window had said it was cancelled and turned its buttons back on, and then writing the file.

Cancelling also dropped a download of the same story that had been started by hand, when the update had joined it instead of queueing its own.
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import pytest

from ficary import cli
from ficary.download_queue import DownloadQueues, owns
from ficary.jobs import DownloadJob
from ficary.models import Chapter, Story

_site_numbers = itertools.count(1)


def a_site_name() -> str:
    """Each test gets its own site, so its queue is its own: the download queues are shared across the whole process."""
    return f"cancel-test-site-{next(_site_numbers)}"


class FakeStorySite:
    """A site whose stories have ``chapters`` chapters. Downloading one reports each chapter as it arrives, as every real scraper does. ``on_chapter`` runs just before a chapter is reported, which is where a test presses Cancel."""

    def __init__(self, site_name: str, chapters: int = 10, on_chapter=None):
        self.site_name = site_name
        self.concurrency = 1
        self.chapters = chapters
        self.on_chapter = on_chapter or (lambda url, number: None)
        self.fetched: list[tuple[str, int]] = []
        self.finished = threading.Event()
        self.lock = threading.Lock()

    def parse_story_id(self, url):
        return int(url.rsplit("/", 1)[1])

    def probe_chapter_count(self, url):
        raise AssertionError("every entry in these tests already knows its chapter count")

    def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
        try:
            story = Story(id=self.parse_story_id(url), title=f"Story {url}", author="Nobody", summary="", url=url)
            for number in range(skip_chapters + 1, self.chapters + 1):
                with self.lock:
                    self.fetched.append((url, number))
                story.chapters.append(Chapter(number=number, title=f"Chapter {number}", html=f"<p>Chapter {number}.</p>"))
                self.on_chapter(url, number)
                if progress_callback:
                    progress_callback(number, self.chapters, f"Chapter {number}", False)
            return story
        finally:
            self.finished.set()


def an_existing_file(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"the book as it was before the update")
    return path


def wait_until_idle(site_name: str, seconds: float = 15.0) -> None:
    """Wait until nothing is running or waiting on ``site_name``'s download queue."""
    import time

    deadline = time.monotonic() + seconds
    while site_name in DownloadQueues.snapshot():
        if time.monotonic() > deadline:
            raise AssertionError(f"{site_name} was still busy after {seconds} seconds")
        time.sleep(0.05)


def write_the_update(story, folder, **options):
    """Stands in for the EPUB export: writes the updated book over the file being updated."""
    target = Path(options.get("output_path") or Path(folder) / "story.epub")
    target.write_bytes(f"the updated book, {len(story.chapters)} chapters".encode("utf-8"))
    return target


@pytest.fixture
def no_real_reading(monkeypatch):
    """The update reads each existing file's chapters before downloading. These files are stand-ins, so the read is too."""
    monkeypatch.setattr(
        cli, "read_chapters",
        lambda path: [Chapter(number=1, title="Chapter 1", html="<p>Chapter 1.</p>")],
    )


def plug_in(monkeypatch, site: FakeStorySite) -> None:
    class SiteClass:
        site_name = site.site_name

    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: site)
    monkeypatch.setattr(cli, "_detect_site", lambda url: SiteClass)


def entries(folder: Path, count: int, *, remote: int = 10) -> list[dict]:
    made = []
    for number in range(1, count + 1):
        path = an_existing_file(folder, f"story-{number}.epub")
        made.append({
            "path": path, "rel": path.name,
            "url": f"https://example.invalid/s/{number}",
            "local": 1, "remote": remote,
        })
    return made


def run_update(queue, cancel, lines):
    return cli._run_update_queue(
        queue, DownloadJob(dry_run=False, skip_complete=False), workers=1,
        skipped_count=0, label="Library update",
        progress=lines.append, cancel_event=cancel,
    )


# ── One story ────────────────────────────────────────────────────


class TestOneStory:
    def test_it_stops_at_the_chapter_it_is_on(self, tmp_path, monkeypatch, no_real_reading):
        cancel = threading.Event()
        site = FakeStorySite(a_site_name(), on_chapter=lambda url, number: number == 4 and cancel.set())
        plug_in(monkeypatch, site)
        path = an_existing_file(tmp_path, "story.epub")
        with pytest.raises(cli.DownloadCancelled):
            cli._download_one(
                "https://example.invalid/s/7", DownloadJob(format="epub"), tmp_path,
                update_path=path, existing_chapters=1,
                status_callback=lambda line: None, cancel_event=cancel,
            )
        assert [number for _url, number in site.fetched] == [2, 3, 4]
        assert path.read_bytes() == b"the book as it was before the update"

    def test_a_cancel_before_it_starts_makes_no_request(self, tmp_path, monkeypatch, no_real_reading):
        cancel = threading.Event()
        cancel.set()
        site = FakeStorySite(a_site_name())
        plug_in(monkeypatch, site)
        with pytest.raises(cli.DownloadCancelled):
            cli._download_one(
                "https://example.invalid/s/7", DownloadJob(format="epub"), tmp_path,
                update_path=an_existing_file(tmp_path, "story.epub"), existing_chapters=1,
                status_callback=lambda line: None, cancel_event=cancel,
            )
        assert site.fetched == []

    def test_a_story_whose_chapters_have_all_arrived_is_still_saved(self, tmp_path, monkeypatch, no_real_reading):
        """The cancel came after the last chapter. Writing the file costs nothing more from the site, and the update is real."""
        cancel = threading.Event()
        site = FakeStorySite(a_site_name(), chapters=3)
        plug_in(monkeypatch, site)
        saved = []
        monkeypatch.setitem(cli.EXPORTERS, "epub", lambda story, folder, **options: saved.append(len(story.chapters)) or str(tmp_path / "story.epub"))
        monkeypatch.setattr(cli, "_merge_chapter_lists", lambda old, new: (list(old) + list(new), 0))
        ok = cli._download_one(
            "https://example.invalid/s/7", DownloadJob(format="epub"), tmp_path,
            update_path=an_existing_file(tmp_path, "story.epub"), existing_chapters=1,
            status_callback=lambda line: None, cancel_event=cancel,
        )
        # Pressed after the last chapter was reported, while the file was being written.
        cancel.set()
        assert ok
        assert saved == [3]

    def test_without_a_cancel_event_nothing_changes(self, tmp_path, monkeypatch, no_real_reading):
        site = FakeStorySite(a_site_name(), chapters=4)
        plug_in(monkeypatch, site)
        monkeypatch.setitem(cli.EXPORTERS, "epub", lambda story, folder, **options: str(tmp_path / "story.epub"))
        monkeypatch.setattr(cli, "_merge_chapter_lists", lambda old, new: (list(old) + list(new), 0))
        assert cli._download_one(
            "https://example.invalid/s/7", DownloadJob(format="epub"), tmp_path,
            update_path=an_existing_file(tmp_path, "story.epub"), existing_chapters=1,
            status_callback=lambda line: None,
        )
        assert [number for _url, number in site.fetched] == [2, 3, 4]

    def test_an_audiobook_is_told_about_the_cancel_and_its_stop_counts_as_one(self, tmp_path, monkeypatch, no_real_reading):
        from ficary import tts

        cancel = threading.Event()
        site = FakeStorySite(a_site_name(), chapters=2)
        plug_in(monkeypatch, site)
        monkeypatch.setattr(cli, "check_format_deps", lambda format: None)
        handed = {}

        def render(story, folder, **options):
            handed.update(options)
            raise tts.AudiobookCancelled("Audiobook render cancelled during chapter 1 of 2.")

        monkeypatch.setattr(tts, "generate_audiobook", render)
        with pytest.raises(cli.DownloadCancelled, match="cancelled during chapter 1"):
            cli._download_one(
                "https://example.invalid/s/7", DownloadJob(format="audio"), tmp_path,
                status_callback=lambda line: None, cancel_event=cancel,
            )
        assert handed["cancel_event"] is cancel

    def test_a_story_handed_back_by_a_site_that_swallowed_the_stop_is_not_saved(self, tmp_path, monkeypatch, no_real_reading):
        """A scraper whose chapter loop sits inside a broad ``except`` catches the stop and returns the chapters it had. Saved, that short story would replace the book."""
        cancel = threading.Event()

        class SwallowingSite(FakeStorySite):
            def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
                story = Story(id=self.parse_story_id(url), title="Story", author="Nobody", summary="", url=url)
                try:
                    for number in range(skip_chapters + 1, self.chapters + 1):
                        self.fetched.append((url, number))
                        story.chapters.append(Chapter(number=number, title=f"Chapter {number}", html="<p>x</p>"))
                        if number == 3:
                            cancel.set()
                        progress_callback(number, self.chapters, f"Chapter {number}", False)
                except Exception:  # noqa: BLE001 - this is the scraper the test is about
                    pass
                return story

        site = SwallowingSite(a_site_name())
        plug_in(monkeypatch, site)
        saved = []
        monkeypatch.setitem(cli.EXPORTERS, "epub", lambda story, folder, **options: saved.append(len(story.chapters)) or str(tmp_path / "story.epub"))
        path = an_existing_file(tmp_path, "story.epub")
        with pytest.raises(cli.DownloadCancelled):
            cli._download_one(
                "https://example.invalid/s/7", DownloadJob(format="epub"), tmp_path,
                update_path=path, existing_chapters=1,
                status_callback=lambda line: None, cancel_event=cancel,
            )
        assert saved == []
        assert path.read_bytes() == b"the book as it was before the update"

    def test_the_cancel_is_not_mistaken_for_a_failure_by_the_handlers_that_report_one(self):
        """It must get past every except clause in the download and reach the update's own."""
        assert not issubclass(cli.DownloadCancelled, cli._DOWNLOAD_EXPECTED_ERRORS)
        assert not issubclass(cli.DownloadCancelled, RuntimeError)


# ── The whole update ─────────────────────────────────────────────


class TestTheUpdate:
    def cancel_part_way(self, tmp_path, monkeypatch, *, stories=3, chapters=10, cancel_at=4, lag=0.8):
        """Runs an update of ``stories`` stories on one site and presses Cancel while the first story's chapter ``cancel_at`` is being fetched. The fetch then takes ``lag`` seconds more, long enough for the update to notice the cancel before the story does."""
        import time

        cancel = threading.Event()

        def on_chapter(url, number):
            if url.endswith("/1") and number == cancel_at:
                cancel.set()
                time.sleep(lag)

        site = FakeStorySite(a_site_name(), chapters=chapters, on_chapter=on_chapter)
        plug_in(monkeypatch, site)
        # A story that gets to the end is written back to its file, as a real export would.
        monkeypatch.setitem(cli.EXPORTERS, "epub", write_the_update)
        monkeypatch.setattr(cli, "_merge_chapter_lists", lambda old, new: (list(old) + list(new), 0))
        lines: list[str] = []
        code = run_update(entries(tmp_path, stories), cancel, lines)
        # On the old code the story was still running here, and went on to write its file. Wait until the site's queue has nothing left at all, so a line posted late, or a late write, is caught.
        wait_until_idle(site.site_name)
        return site, lines, code

    def test_the_story_stops_at_its_next_chapter(self, tmp_path, monkeypatch, no_real_reading):
        site, _lines, _code = self.cancel_part_way(tmp_path, monkeypatch)
        assert [number for url, number in site.fetched if url.endswith("/1")] == [2, 3, 4]

    def test_the_stories_waiting_their_turn_never_start(self, tmp_path, monkeypatch, no_real_reading):
        site, _lines, _code = self.cancel_part_way(tmp_path, monkeypatch)
        assert {url for url, _number in site.fetched} == {"https://example.invalid/s/1"}

    def test_nothing_is_posted_after_the_summary(self, tmp_path, monkeypatch, no_real_reading):
        _site, lines, _code = self.cancel_part_way(tmp_path, monkeypatch)
        summary = next(index for index, line in enumerate(lines) if line.startswith("Library update cancelled"))
        after = [line for line in lines[summary + 1:] if "Chapter" in line or "Stopped" in line]
        assert after == []

    def test_the_story_is_reported_as_stopped_and_not_as_failed(self, tmp_path, monkeypatch, no_real_reading):
        _site, lines, code = self.cancel_part_way(tmp_path, monkeypatch)
        assert any("Stopped part-way through this story; its file was not changed" in line for line in lines)
        summary = next(line for line in lines if line.startswith("Library update cancelled"))
        assert "0 failed" in summary
        assert "1 stopped part-way" in summary
        assert not any("Download failed" in line for line in lines)
        assert code == 0

    def test_the_stories_left_are_counted_without_the_stopped_one(self, tmp_path, monkeypatch, no_real_reading):
        _site, lines, _code = self.cancel_part_way(tmp_path, monkeypatch)
        assert any("2 of 3 stories were not checked" in line for line in lines)

    def test_the_wait_for_the_story_is_said_out_loud(self, tmp_path, monkeypatch, no_real_reading):
        _site, lines, _code = self.cancel_part_way(tmp_path, monkeypatch)
        waiting = next(index for index, line in enumerate(lines) if line.startswith("Waiting for the story being downloaded"))
        stopped = next(index for index, line in enumerate(lines) if "Stopped part-way" in line)
        assert waiting < stopped

    def test_its_file_is_left_as_it_was(self, tmp_path, monkeypatch, no_real_reading):
        self.cancel_part_way(tmp_path, monkeypatch)
        assert (tmp_path / "story-1.epub").read_bytes() == b"the book as it was before the update"

    def test_an_update_that_is_not_cancelled_says_nothing_about_stopping(self, tmp_path, monkeypatch, no_real_reading):
        site = FakeStorySite(a_site_name(), chapters=3)
        plug_in(monkeypatch, site)
        monkeypatch.setitem(cli.EXPORTERS, "epub", lambda story, folder, **options: str(Path(folder) / "out.epub"))
        monkeypatch.setattr(cli, "_merge_chapter_lists", lambda old, new: (list(old) + list(new), 0))
        lines: list[str] = []
        run_update(entries(tmp_path, 2, remote=3), threading.Event(), lines)
        summary = next(line for line in lines if line.startswith("Library update complete"))
        assert "stopped" not in summary
        assert not any(line.startswith("Waiting for the story") for line in lines)


# ── A download started by hand ───────────────────────────────────


def press_cancel_once_queued(monkeypatch, cancel: threading.Event, how_many: int) -> list:
    """Presses Cancel the moment the update has queued ``how_many`` downloads, before any of them can start: the site is kept busy by the test. Returns the futures the update was handed, in order."""
    handed = []
    real_enqueue = DownloadQueues.enqueue

    def enqueue(name, job, dedupe_key=None):
        future = real_enqueue(name, job, dedupe_key=dedupe_key)
        handed.append(future)
        if len(handed) == how_many:
            cancel.set()
        return future

    monkeypatch.setattr(DownloadQueues, "enqueue", enqueue)
    return handed


class TestADownloadStartedByHand:
    def test_the_update_does_not_cancel_a_download_it_only_joined(self, tmp_path, monkeypatch, no_real_reading):
        """The same story was already waiting in the queue, started by hand. The update joined that download instead of queueing its own. Its Cancel must leave it alone."""
        from ficary.sites import canonical_url

        site_name = a_site_name()
        site = FakeStorySite(site_name, chapters=3)
        plug_in(monkeypatch, site)
        release_the_site = threading.Event()
        DownloadQueues.enqueue(site_name, lambda: release_the_site.wait(10))
        story_url = "https://example.invalid/s/1"
        ran_by_hand = threading.Event()
        by_hand = DownloadQueues.enqueue(
            site_name, ran_by_hand.set,
            dedupe_key=canonical_url(story_url) or story_url,
        )

        cancel = threading.Event()
        handed = press_cancel_once_queued(monkeypatch, cancel, 1)
        lines: list[str] = []
        try:
            run_update(entries(tmp_path, 1), cancel, lines)
            assert handed == [by_hand], "the update should have joined the download started by hand"
            assert not by_hand.cancelled()
        finally:
            release_the_site.set()
        assert ran_by_hand.wait(5), "the download started by hand never ran"

    def test_the_update_still_drops_its_own_waiting_downloads(self, tmp_path, monkeypatch, no_real_reading):
        site_name = a_site_name()
        site = FakeStorySite(site_name, chapters=3)
        plug_in(monkeypatch, site)
        release_the_site = threading.Event()
        DownloadQueues.enqueue(site_name, lambda: release_the_site.wait(10))
        cancel = threading.Event()
        handed = press_cancel_once_queued(monkeypatch, cancel, 2)
        try:
            run_update(entries(tmp_path, 2), cancel, [])
            assert len(handed) == 2
            assert all(future.cancelled() for future in handed)
        finally:
            release_the_site.set()
        assert site.fetched == []


class TestTellingAJoinedDownloadFromOnesOwn:
    def test_a_new_download_is_owned_by_the_job_that_queued_it(self):
        site_name = a_site_name()
        job = lambda: None  # noqa: E731
        future = DownloadQueues.enqueue(site_name, job)
        future.result(5)
        assert owns(future, job)

    def test_a_joined_download_is_not(self):
        site_name = a_site_name()
        release = threading.Event()
        DownloadQueues.enqueue(site_name, lambda: release.wait(10))
        first_job = lambda: "first"  # noqa: E731
        second_job = lambda: "second"  # noqa: E731
        try:
            first = DownloadQueues.enqueue(site_name, first_job, dedupe_key="the-same-story")
            second = DownloadQueues.enqueue(site_name, second_job, dedupe_key="the-same-story")
            assert second is first
            assert owns(first, first_job)
            assert not owns(second, second_job)
        finally:
            release.set()
        assert first.result(5) == "first"
