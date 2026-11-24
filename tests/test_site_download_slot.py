"""Tests that a site only ever sees one story download at a time, whichever way the download was started.

A site's rate limit is kept by its scraper spacing out its own requests. The per-site queue kept queued downloads apart, but a batch picked from an author page, a series and a voice preview each ran on a thread of their own, beside the queue. A batch and an ordinary download of the same site then ran together and sent the site twice what its limit allows. Every path now holds the site's download slot around each story.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from ficary import download_queue
from ficary.download_queue import DownloadQueues, site_slot

SITE = "slot-test-site"
OTHER_SITE = "slot-test-other-site"


def slot_is_free(site_name: str) -> bool:
    """Whether a different thread could take ``site_name``'s slot right now."""
    result: list[bool] = []

    def try_it():
        with download_queue._site_slots_guard:
            slot = download_queue._site_slots.get(site_name)
        if slot is None:
            result.append(True)
            return
        got_it = slot.acquire(blocking=False)
        if got_it:
            slot.release()
        result.append(got_it)

    thread = threading.Thread(target=try_it)
    thread.start()
    thread.join(5)
    return result == [True]


class TestTheSlot:
    def test_another_thread_cannot_take_a_held_slot(self):
        with site_slot(SITE):
            assert slot_is_free(SITE) is False
        assert slot_is_free(SITE) is True

    def test_a_second_download_waits_its_turn(self):
        order: list[str] = []
        first_is_in = threading.Event()
        let_first_finish = threading.Event()

        def first():
            with site_slot(SITE):
                order.append("first starts")
                first_is_in.set()
                let_first_finish.wait(5)
                order.append("first ends")

        def second():
            first_is_in.wait(5)
            with site_slot(SITE):
                order.append("second starts")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        first_is_in.wait(5)
        time.sleep(0.2)
        assert order == ["first starts"]
        let_first_finish.set()
        for thread in threads:
            thread.join(5)
        assert order == ["first starts", "first ends", "second starts"]

    def test_different_sites_do_not_wait_for_each_other(self):
        """Checked by doing it, not by looking at the locks: a download from one site goes ahead while another site's slot is held."""
        went_ahead = threading.Event()

        def other_site_download():
            with site_slot(OTHER_SITE):
                went_ahead.set()

        with site_slot(SITE):
            thread = threading.Thread(target=other_site_download, daemon=True)
            thread.start()
            assert went_ahead.wait(2) is True
        thread.join(5)

    def test_the_same_thread_can_take_it_again(self):
        """A queued job holds the slot and then calls a helper that takes it too."""
        with site_slot(SITE):
            with site_slot(SITE):
                assert slot_is_free(SITE) is False
            assert slot_is_free(SITE) is False
        assert slot_is_free(SITE) is True

    def test_it_is_released_when_the_download_fails(self):
        with pytest.raises(RuntimeError):
            with site_slot(SITE):
                raise RuntimeError("download failed")
        assert slot_is_free(SITE) is True

    def test_waiting_is_announced_only_when_there_is_a_wait(self):
        announced: list[str] = []
        with site_slot(SITE, on_wait=lambda: announced.append("free")):
            pass
        assert announced == []

        holder_is_in = threading.Event()
        let_holder_finish = threading.Event()

        def holder():
            with site_slot(SITE):
                holder_is_in.set()
                let_holder_finish.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        holder_is_in.wait(5)

        def waiter():
            with site_slot(SITE, on_wait=lambda: announced.append("waiting")):
                pass

        waiting_thread = threading.Thread(target=waiter)
        waiting_thread.start()
        time.sleep(0.2)
        assert announced == ["waiting"]
        let_holder_finish.set()
        thread.join(5)
        waiting_thread.join(5)

    def test_a_broken_announcement_does_not_stop_the_download(self):
        holder_is_in = threading.Event()
        let_holder_finish = threading.Event()
        got_through: list[bool] = []

        def holder():
            with site_slot(SITE):
                holder_is_in.set()
                let_holder_finish.wait(5)

        def waiter():
            def broken():
                raise RuntimeError("status pane is gone")

            with site_slot(SITE, on_wait=broken):
                got_through.append(True)

        thread = threading.Thread(target=holder)
        thread.start()
        holder_is_in.wait(5)
        waiting_thread = threading.Thread(target=waiter)
        waiting_thread.start()
        time.sleep(0.2)
        let_holder_finish.set()
        thread.join(5)
        waiting_thread.join(5)
        assert got_through == [True]


class TestTheQueue:
    def test_a_queued_job_holds_its_sites_slot(self):
        seen: list[bool] = []
        future = DownloadQueues.enqueue(SITE, lambda: seen.append(slot_is_free(SITE)))
        future.result(timeout=10)
        assert seen == [False]

    def test_a_queued_job_waits_for_a_download_that_was_not_queued(self):
        ran = threading.Event()
        with site_slot(SITE):
            future = DownloadQueues.enqueue(SITE, ran.set)
            assert ran.wait(0.4) is False
        future.result(timeout=10)
        assert ran.is_set()

    def test_a_queued_job_on_another_site_does_not_wait(self):
        ran = threading.Event()
        with site_slot(SITE):
            future = DownloadQueues.enqueue(OTHER_SITE, ran.set)
            future.result(timeout=10)
        assert ran.is_set()

    def test_the_slot_is_free_again_after_a_job_that_raised(self):
        def job():
            raise RuntimeError("scrape failed")

        future = DownloadQueues.enqueue(SITE, job)
        with pytest.raises(RuntimeError):
            future.result(timeout=10)
        assert slot_is_free(SITE) is True


wx = pytest.importorskip("wx")
if not os.environ.get("DISPLAY"):
    pytestmark_gui = pytest.mark.skip(reason="needs a display (run under xvfb-run -a)")
else:
    pytestmark_gui = pytest.mark.usefixtures("wx_app")

FFN_STORY = "https://www.fanfiction.net/s/12345/1/Some-Story"


@pytestmark_gui
class TestDownloadsThatAreNotQueued:
    """The paths that download on their own thread. Each must hold the slot while it downloads, and let it go before it exports: an export can be an hours-long audiobook render, and nothing in it talks to the site."""

    def frame_with_stub_downloads(self, monkeypatch, observed: dict):
        from ficary.gui import MainFrame
        from ficary.models import Chapter, Story

        frame = MainFrame()

        class StubScraper:
            """A scraper for one plain story. The predicates say no on purpose: an ordinary download asks them first, and a stub that lacks them, or one that answers yes to everything, is routed away before it ever downloads."""

            def parse_story_id(self, url):
                return 12345

            def is_forum_url(self, url):
                return False

            def is_author_url(self, url):
                return False

            def download(self, url, progress_callback=None, **options):
                observed.setdefault("free during download", []).append(slot_is_free("ffn"))
                story = Story(id=12345, title="T", author="A", summary="", url=url)
                story.chapters.append(Chapter(number=1, title="One", html="<p>Text.</p>"))
                return story

        monkeypatch.setattr(frame, "_scraper_for", lambda url, **options: StubScraper())

        def export(story, params, update_path=None, **options):
            observed.setdefault("free during export", []).append(slot_is_free("ffn"))
            return "/tmp/book.epub"

        monkeypatch.setattr(frame, "_export_story", export)
        monkeypatch.setattr(frame, "_set_busy", lambda *args, **kwargs: None)
        return frame

    def test_a_picked_batch_holds_the_slot_for_each_story(self, monkeypatch):
        observed: dict = {}
        frame = self.frame_with_stub_downloads(monkeypatch, observed)
        try:
            params = frame._snapshot_download_params()
            frame._run_picked_batch([FFN_STORY, FFN_STORY], "author", params=params)
        finally:
            frame.Destroy()
        assert observed["free during download"] == [False, False]
        assert observed["free during export"] == [True, True]

    def test_a_series_download_holds_the_slot_for_each_story(self, monkeypatch):
        observed: dict = {}
        frame = self.frame_with_stub_downloads(monkeypatch, observed)
        try:
            params = frame._snapshot_download_params()
            frame._batch_download([FFN_STORY], frame._scraper_for(FFN_STORY), params=params)
        finally:
            frame.Destroy()
        assert observed["free during download"] == [False]
        assert observed["free during export"] == [True]

    def test_an_ordinary_download_on_its_own_thread_holds_the_slot(self, monkeypatch):
        observed: dict = {}
        frame = self.frame_with_stub_downloads(monkeypatch, observed)
        try:
            params = frame._snapshot_download_params()
            frame._run_download(FFN_STORY, params=params)
        finally:
            frame.Destroy()
        assert observed["free during download"] == [False]

    def test_a_wait_is_said_in_the_status_pane(self, monkeypatch):
        observed: dict = {}
        frame = self.frame_with_stub_downloads(monkeypatch, observed)
        lines: list[str] = []
        monkeypatch.setattr(frame, "_log", lines.append)
        holder_is_in = threading.Event()
        let_holder_finish = threading.Event()

        def holder():
            with site_slot("ffn"):
                holder_is_in.set()
                let_holder_finish.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        holder_is_in.wait(5)
        try:
            def waiter():
                with frame._site_slot_for(FFN_STORY):
                    pass

            waiting_thread = threading.Thread(target=waiter)
            waiting_thread.start()
            time.sleep(0.2)
            assert lines == ["  Waiting for another ffn download to finish..."]
            let_holder_finish.set()
            waiting_thread.join(5)
        finally:
            let_holder_finish.set()
            thread.join(5)
            frame.Destroy()
