"""Stop: the Add Story window's button and File > Stop downloads.

The only stop button used to be "Cancel render", which stopped an audiobook being made and nothing else. Once a story download had started, nothing short of closing the program stopped it, and a long FanFiction.net story takes ten minutes or more. Stop now stops every download and audiobook in progress: a story at the end of the chapter it is on, a batch with it, downloads waiting their turn before they start, an audiobook after the piece it is speaking, and a running library update.
"""

from __future__ import annotations

import itertools
import os
import threading

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "these tests build real windows (run under xvfb-run -a)",
        allow_module_level=True,
    )

from ficary import gui as gui_module  # noqa: E402
from ficary.download_queue import DownloadQueues  # noqa: E402
from ficary.models import Chapter, Story  # noqa: E402

_numbers = itertools.count(1)


def royal_road(number: int) -> str:
    return f"https://www.royalroad.com/fiction/{number}/a-story"


class FakeScraper:
    """A site whose stories have ``chapters`` chapters, reported one by one as real scrapers do. ``on_chapter(url, number)`` runs just before each report, which is where a test presses Stop."""

    site_name = "royalroad"
    concurrency = 1

    def __init__(self, chapters: int = 6, on_chapter=None):
        self.chapters = chapters
        self.on_chapter = on_chapter or (lambda url, number: None)
        self.fetched: list[tuple[str, int]] = []

    def parse_story_id(self, url):
        return int(url.split("/")[4])

    def is_forum_url(self, url):
        return False

    def is_author_url(self, url):
        return False

    def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
        story = Story(id=self.parse_story_id(url), title=f"Story {url}", author="Nobody", summary="", url=url)
        for number in range(skip_chapters + 1, self.chapters + 1):
            self.fetched.append((url, number))
            story.chapters.append(Chapter(number=number, title=f"Chapter {number}", html=f"<p>{number}</p>"))
            self.on_chapter(url, number)
            if progress_callback:
                progress_callback(number, self.chapters, f"Chapter {number}", False)
        return story


def logged(window) -> str:
    with window._log_lock:
        return "".join(window._log_queue)


@pytest.fixture
def window(wx_app, monkeypatch, tmp_path):
    """A real main window whose exports are recorded instead of written."""
    from ficary.gui import MainFrame

    frame = MainFrame()
    frame.exported = []

    def export(story, params, update_path=None, **options):
        frame.exported.append(story.url)
        return str(tmp_path / f"{story.id}.epub")

    monkeypatch.setattr(frame, "_export_story", export)
    monkeypatch.setattr(frame, "_auto_index_download", lambda *args, **kwargs: None, raising=False)
    with frame._log_lock:
        frame._log_queue.clear()
    yield frame
    frame._clip_timer.Stop()
    frame.Destroy()


def params_for(window, **changes):
    """A download snapshot as a click would take it, without reading the form."""
    return gui_module._DownloadParams(
        fmt=changes.pop("fmt", "epub"), raw_output_dir="", filename_template="{title}",
        hr_as_stars=False, strip_notes=False, llm_strip_notes=False,
        stop_generation=window._stop_generation, **changes,
    )


# ── Where it is ──────────────────────────────────────────────────


class TestWhereStopIs:
    def test_the_add_story_button_is_called_stop(self, window):
        assert window.cancel_render_btn.GetLabel() == "St&op"
        assert window.cancel_render_btn.GetName() == "Stop downloads and audiobooks in progress"

    def test_the_button_is_always_there_to_tab_to(self, window):
        assert window.cancel_render_btn.IsEnabled()

    def test_the_file_menu_has_stop_downloads_on_ctrl_full_stop(self, window):
        menu = window.GetMenuBar().GetMenu(window.GetMenuBar().FindMenu("File"))
        labels = [item.GetItemLabel() for item in menu.GetMenuItems()]
        assert "&Stop downloads\tCtrl+." in labels

    def test_its_shortcut_letter_is_not_taken_in_the_add_story_window(self, window):
        """Alt+S is Save to there and Alt+P Update Existing File."""
        letters = []
        for child in window._add_story_panel.GetChildren():
            label = child.GetLabel() if hasattr(child, "GetLabel") else ""
            if "&" in label:
                letters.append(label[label.index("&") + 1].lower())
        assert letters.count("o") == 1

    def test_the_menu_item_runs_stop(self, window):
        menu_bar = window.GetMenuBar()
        item_id = menu_bar.FindMenuItem("File", "Stop downloads")
        assert item_id != wx.NOT_FOUND
        assert window._stop_generation == 0
        window.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item_id))
        assert window._stop_generation == 1
        assert "Nothing is downloading right now." in logged(window)

    def test_the_button_runs_stop(self, window):
        event = wx.CommandEvent(wx.wxEVT_BUTTON, window.cancel_render_btn.GetId())
        event.SetEventObject(window.cancel_render_btn)
        window.cancel_render_btn.ProcessEvent(event)
        assert window._stop_generation == 1


# ── Nothing running ──────────────────────────────────────────────


class TestWithNothingRunning:
    def test_it_says_so(self, window):
        window._on_stop()
        assert "Nothing is downloading right now." in logged(window)

    def test_a_download_asked_for_afterwards_is_not_stopped(self, window):
        window._on_stop()
        after = window._snapshot_download_params()
        assert not window._stop_requested(after)


# ── A story being downloaded ─────────────────────────────────────


class TestAStoryBeingDownloaded:
    def test_it_stops_at_the_end_of_the_chapter_it_is_on(self, window, monkeypatch):
        params = params_for(window)
        scraper = FakeScraper(on_chapter=lambda url, number: number == 3 and window._on_stop())
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        outcome = window._run_download(royal_road(next(_numbers)), params=params)
        assert [number for _url, number in scraper.fetched] == [1, 2, 3]
        assert outcome.ok is False
        assert outcome.reason == "stopped"
        assert window.exported == []
        text = logged(window)
        assert "Stopped part-way through this story. Nothing was saved for it" in text
        assert "Error:" not in text

    def test_the_summary_says_the_story_stops_at_its_chapter(self, window, monkeypatch):
        params = params_for(window)
        scraper = FakeScraper(on_chapter=lambda url, number: number == 2 and window._on_stop())
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        monkeypatch.setattr(window, "_global_busy", True)
        monkeypatch.setattr(window, "_busy_kind", "download")
        window._run_download(royal_road(next(_numbers)), params=params)
        assert "The story being downloaded stops at the end of the chapter it is on." in logged(window)

    def test_a_download_asked_for_after_stop_runs_to_the_end(self, window, monkeypatch):
        window._on_stop()
        params = params_for(window)
        scraper = FakeScraper(chapters=4)
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        outcome = window._run_download(royal_road(next(_numbers)), params=params)
        assert outcome.ok is True
        assert len(scraper.fetched) == 4

    def test_one_queued_before_stop_but_started_after_does_not_start(self, window, monkeypatch):
        params = params_for(window)
        window._on_stop()
        scraper = FakeScraper()
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        url = royal_road(next(_numbers))
        outcome = window._run_download(url, params=params)
        assert outcome.reason == "stopped"
        assert scraper.fetched == []
        assert f"Not started, downloads were stopped: {url}" in logged(window)

    def test_a_voice_preview_stops_too(self, window, monkeypatch):
        params = params_for(window)
        scraper = FakeScraper(on_chapter=lambda url, number: window._on_stop())
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        window._run_preview_voices(royal_road(next(_numbers)), "/tmp", params)
        text = logged(window)
        assert "Voice preview stopped." in text
        assert "Preview failed" not in text


# ── Downloads waiting their turn ─────────────────────────────────


def busy_site(name: str) -> threading.Event:
    """Keeps ``name``'s download queue busy until the returned event is set, so whatever is queued behind waits."""
    release = threading.Event()
    DownloadQueues.enqueue(name, lambda: release.wait(10))
    return release


class TestDownloadsWaitingTheirTurn:
    def test_they_are_dropped_and_never_run(self, window, monkeypatch):
        site = "royalroad"
        release = busy_site(site)
        ran = []
        try:
            futures = [
                window._enqueue_site_job(royal_road(next(_numbers)), lambda: ran.append(True))
                for _ in range(2)
            ]
            window._on_stop()
            assert all(future.cancelled() for future in futures)
        finally:
            release.set()
        assert ran == []
        assert "2 downloads waiting their turn were dropped." in logged(window)

    def test_one_the_window_only_joined_is_left_alone(self, window):
        """A library update had queued this story already. Stop in the main window is not the library update's Cancel."""
        from ficary.sites import canonical_url

        site = "royalroad"
        release = busy_site(site)
        url = royal_road(next(_numbers))
        ran = threading.Event()
        try:
            theirs = DownloadQueues.enqueue(site, ran.set, dedupe_key=canonical_url(url) or url)
            joined = window._enqueue_site_job(url, lambda: None)
            assert joined is theirs
            window._on_stop()
            assert not theirs.cancelled()
        finally:
            release.set()
        assert ran.wait(5)

    def test_a_finished_job_is_forgotten(self, window):
        future = window._enqueue_site_job(royal_road(next(_numbers)), lambda: None)
        future.result(5)
        assert future not in window._own_download_futures

    def test_a_click_after_stop_is_a_new_job_not_joined_to_one_stopping(self):
        before = gui_module._DownloadParams(
            fmt="epub", raw_output_dir="", filename_template="{title}",
            hr_as_stars=False, strip_notes=False, llm_strip_notes=False, stop_generation=0,
        )
        after = gui_module._DownloadParams(
            fmt="epub", raw_output_dir="", filename_template="{title}",
            hr_as_stars=False, strip_notes=False, llm_strip_notes=False, stop_generation=1,
        )
        assert gui_module._job_intent(before) != gui_module._job_intent(after)
        # Never stopped: the same identity as before Stop existed.
        assert "after-stop" not in gui_module._job_intent(before)


# ── Batches and series ───────────────────────────────────────────


class TestBatches:
    def test_a_batch_stops_with_its_story_and_starts_no_more(self, window):
        params = params_for(window)
        urls = [royal_road(next(_numbers)) for _ in range(3)]
        scraper = FakeScraper(on_chapter=lambda url, number: url == urls[1] and number == 2 and window._on_stop())
        window._batch_download(urls, scraper, params=params)
        assert {url for url, _number in scraper.fetched} == {urls[0], urls[1]}
        assert window.exported == [urls[0]]
        assert "Batch stopped: 1 saved, 0 failed, 1 stopped part-way, 1 not started, out of 3." in logged(window)

    def test_a_picked_batch_does_the_same(self, window, monkeypatch):
        params = params_for(window)
        urls = [royal_road(next(_numbers)) for _ in range(3)]
        scraper = FakeScraper(on_chapter=lambda url, number: url == urls[0] and number == 4 and window._on_stop())
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: scraper)
        window._run_picked_batch(urls, "author", params=params)
        assert {url for url, _number in scraper.fetched} == {urls[0]}
        assert window.exported == []
        assert "Author batch stopped: 0 saved, 0 failed, 1 stopped part-way, 2 not started, out of 3." in logged(window)

    def test_stop_between_two_stories_starts_no_more(self, window, monkeypatch):
        """Pressed while the first story was being saved: it is saved, and the next one does not start."""
        params = params_for(window)
        urls = [royal_road(next(_numbers)) for _ in range(3)]
        real_export = window._export_story

        def export_then_stop(story, params, update_path=None, **options):
            result = real_export(story, params, update_path=update_path)
            window._on_stop()
            return result

        monkeypatch.setattr(window, "_export_story", export_then_stop)
        scraper = FakeScraper(chapters=2)
        window._batch_download(urls, scraper, params=params)
        assert {url for url, _number in scraper.fetched} == {urls[0]}
        assert window.exported == [urls[0]]
        assert "Batch stopped: 1 saved, 0 failed, 2 not started, out of 3." in logged(window)

    def test_a_batch_that_is_not_stopped_reads_as_before(self, window):
        params = params_for(window)
        urls = [royal_road(next(_numbers)) for _ in range(2)]
        window._batch_download(urls, FakeScraper(chapters=2), params=params)
        assert "Batch complete: 2 succeeded, 0 failed out of 2." in logged(window)

    def test_a_series_merge_saves_nothing_when_stopped(self, window, monkeypatch):
        from ficary.ao3 import AO3Scraper

        params = params_for(window)
        parts = [f"https://archiveofourown.org/works/{next(_numbers)}" for _ in range(3)]

        class FakeSeries(AO3Scraper):
            def __init__(self):
                self.inner = FakeScraper(on_chapter=lambda url, number: url == parts[1] and number == 2 and window._on_stop())

            def scrape_series_works(self, url):
                return "A Series", list(parts)

            def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
                return self.inner.download(url, progress_callback=progress_callback)

        series = FakeSeries()
        monkeypatch.setattr(window, "_scraper_for", lambda *args, **kwargs: series)
        window._run_series_merge_download("https://archiveofourown.org/series/9", params=params, manage_busy=False)
        assert window.exported == []
        assert {url for url, _number in series.inner.fetched} == {parts[0], parts[1]}
        text = logged(window)
        assert "Series download stopped. Nothing was saved" in text
        # The stopped part is not reported as a part that failed.
        assert "Error:" not in text
        assert "failed and were left out" not in text


# ── Audiobooks and the library update ────────────────────────────


class TestAudiobooksAndTheLibraryUpdate:
    def test_an_audiobook_being_made_is_asked_to_stop(self, window):
        cancel = threading.Event()
        with window._render_cancel_lock:
            window._render_cancels.append(cancel)
        try:
            window._on_stop()
        finally:
            with window._render_cancel_lock:
                window._render_cancels.remove(cancel)
        assert cancel.is_set()
        assert "The audiobook being made stops after the piece it is speaking." in logged(window)

    def test_an_audiobook_whose_download_was_asked_for_before_stop_does_not_start(self, window, monkeypatch, tmp_path):
        """Stop came between the end of the download and the start of the audiobook."""
        from ficary import tts

        params = params_for(window, fmt="audio", audio_backend="builtin", speech_rate=0)
        window._on_stop()
        seen = {}

        def render(story, output_dir, **options):
            seen["stopped"] = options["cancel_event"].is_set()
            raise tts.AudiobookCancelled("Audiobook render cancelled.")

        monkeypatch.setattr(tts, "generate_audiobook", render)
        monkeypatch.setattr(window, "_resolve_output_dir", lambda story, params: str(tmp_path))
        story = Story(id=5, title="T", author="A", summary="", url=royal_road(5), chapters=[Chapter(1, "One", "<p>x</p>")])
        with pytest.raises(gui_module._RenderCancelled):
            gui_module.MainFrame._export_story(window, story, params)
        assert seen == {"stopped": True}

    def test_a_library_update_in_progress_is_cancelled(self, window):
        class LibraryWindow:
            asked = 0

            def cancel_running_update(self):
                type(self).asked += 1
                return True

        window._library_frame = LibraryWindow()
        try:
            window._on_stop()
        finally:
            window._library_frame = None
        assert LibraryWindow.asked == 1
        assert "The library update stops too." in logged(window)

    def test_the_library_windows_own_cancel_says_whether_there_was_anything(self, wx_app):
        from ficary.library.gui import LibraryFrame

        class Stand:
            _update_cancel_event = None
            _update_running = False

        assert LibraryFrame.cancel_running_update(Stand()) is False


# ── The words ────────────────────────────────────────────────────


class TestTheWords:
    def test_everything_at_once(self):
        text = gui_module._stop_summary(running=1, batch_running=False, dropped=3, renders=2, library_update=True)
        assert text == (
            "\nStopping downloads. The story being downloaded stops at the end of the chapter it is on. "
            "3 downloads waiting their turn were dropped. "
            "The 2 audiobooks being made stop after the piece each is speaking. "
            "The library update stops too. "
            "Chapters already fetched are kept, so trying again carries on from there."
        )

    def test_one_waiting_download(self):
        assert "1 download waiting its turn was dropped." in gui_module._stop_summary(
            running=0, batch_running=False, dropped=1, renders=0, library_update=False,
        )

    def test_a_batch_cut_short_between_stories(self):
        assert gui_module._batch_summary("Batch", total=5, succeeded=2, failed=1, started=3, stopped=True) == (
            "\nBatch stopped: 2 saved, 1 failed, 2 not started, out of 5."
        )
