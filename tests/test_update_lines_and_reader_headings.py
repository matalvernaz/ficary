"""What Matt chose after the visual walk (2026-11-24) for an update's lines and the reader's chapter list.

- Check All Stories for Updates: one summary line for the stories skipped as a matter of course (finished, abandoned, checked recently), not a line each: about 800 in his library, before anything useful, many naming command-line options. A skip that means something is wrong with a book still has its line.
- Update lines no longer start with the site's code name ("[royalroad]"); each names its story. Where a site is named, it is by the name it goes by ("Checking 12 Royal Road stories").
- The reader's chapter list shows each chapter's heading ("Chapter 3. The Inn"), not "Chapter 3".
"""

from __future__ import annotations

import os
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ficary import cli

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


# ── Skipped stories ──────────────────────────────────────────────


def a_library(tmp_path: Path) -> Path:
    """Six stories: one to check, one finished, one abandoned, one checked an hour ago, one whose file has gone, one an update can't check (an AO3 series)."""
    from ficary.library.index import LibraryIndex
    from ficary.library.scanner import scan

    from .library_fixtures import ficary_epub

    root = tmp_path / "library"
    root.mkdir()
    urls = {
        "to check": "https://www.fanfiction.net/s/201/1/",
        "finished": "https://www.fanfiction.net/s/202/1/",
        "abandoned": "https://www.fanfiction.net/s/203/1/",
        "recent": "https://www.fanfiction.net/s/204/1/",
        "gone": "https://www.fanfiction.net/s/205/1/",
        "series": "https://archiveofourown.org/series/777",
    }
    paths = {}
    for number, (kind, url) in enumerate(urls.items(), 1):
        paths[kind] = ficary_epub(root, title=f"Story {kind}", url=url, chapters=3, story_id=900 + number)
    scan(root)
    paths["gone"].unlink()
    index = LibraryIndex.load()
    stories = index.library_state(root.resolve())["stories"]
    recently = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for url, entry in stories.items():
        if "/s/202" in url:
            entry["status"] = "Complete"
        elif "/s/203" in url:
            entry["abandoned_at"] = "2026-01-02T00:00:00Z"
        elif "/s/204" in url:
            entry["last_probed"] = recently
    index.save()
    return root.resolve()


def lines_before_the_update(root: Path, monkeypatch, *, force=False) -> list[str]:
    pytest.importorskip("wx")
    from ficary.library.gui import LibraryFrame

    monkeypatch.setattr(cli, "_run_update_queue", lambda queue, *args, **options: None)
    posted: list[str] = []
    window = types.SimpleNamespace(_post_status=posted.append)
    LibraryFrame._probe_one_root(
        window, root, force=force, refetch_all=False,
        recheck_interval=0 if force else 6 * 60 * 60, cancel_event=threading.Event(),
    )
    return posted


def test_stories_skipped_as_a_matter_of_course_are_counted_not_listed(tmp_path, monkeypatch):
    posted = lines_before_the_update(a_library(tmp_path), monkeypatch)
    skip_lines = [line for line in posted if "[skip]" in line]
    assert len(skip_lines) == 1, skip_lines
    assert "file missing on disk" in skip_lines[0], "a skip that means a book has a problem keeps its line"
    assert (
        "1 to check; 3 skipped as finished, abandoned or checked in the last 6 hours "
        "(Force recheck checks all but the abandoned ones)."
    ) in posted
    assert not any("--" in line for line in posted), "no command-line options in the window"
    # The AO3 series is listed once, at the end, where it can be found.
    assert sum("Story series" in line for line in posted) == 1


def test_force_recheck_counts_the_abandoned(tmp_path, monkeypatch):
    posted = lines_before_the_update(a_library(tmp_path), monkeypatch, force=True)
    assert not any("[skip]" in line and "abandoned" in line for line in posted)
    assert "3 to check; 1 skipped as abandoned." in posted


def test_the_command_line_still_lists_each_skipped_story(tmp_path):
    """Only the Library window counts them; the command line's own update still prints a line each."""
    from ficary.library.refresh import build_refresh_queue

    printed: list[str] = []
    build_refresh_queue(a_library(tmp_path), recheck_interval_s=6 * 60 * 60, progress=printed.append)
    skip_lines = [line for line in printed if "[skip]" in line]
    assert len(skip_lines) == 5, skip_lines


# ── Site names ───────────────────────────────────────────────────


def test_every_site_has_a_name_it_goes_by():
    from ficary import sites

    site_names = {
        getattr(value, "site_name", None)
        for value in vars(sites).values()
        if isinstance(value, type)
    } - {None, "unknown"}
    assert site_names, "the sites module names its scrapers"
    assert site_names <= set(sites.SITE_NAMES), site_names - set(sites.SITE_NAMES)
    assert sites.site_display_name("royalroad") == "Royal Road"
    assert sites.site_display_name("ffn") == "FanFiction.net"


@needs_display
def test_library_update_lines_no_longer_start_with_the_sites_code_name(wx_app):
    from ficary import gui
    from ficary.download_queue import WORKER_THREAD_PREFIX
    from ficary.library.gui import LibraryFrame

    frame = gui.MainFrame()
    library = LibraryFrame(frame, frame.prefs)
    try:
        sent: list[str] = []
        library.status_ctrl.post_line = sent.append
        worker = threading.Thread(target=library._post_status, args=('Updating 1 of 2: "Sky", 2 new chapters.',), name=f"{WORKER_THREAD_PREFIX}royalroad")
        worker.start()
        worker.join(5)
        assert sent == ['Updating 1 of 2: "Sky", 2 new chapters.']
    finally:
        library.Destroy()
        frame._clip_timer.Stop()
        frame.Destroy()


@needs_display
def test_the_main_windows_download_lines_name_the_site_as_it_goes_by(wx_app):
    """Its download lines often don't name the story, so they keep the site, by the name it goes by."""
    from ficary import gui
    from ficary.download_queue import WORKER_THREAD_PREFIX

    frame = gui.MainFrame()
    try:
        with frame._log_lock:
            frame._log_queue.clear()
        worker = threading.Thread(target=frame._log, args=("Found 2 new chapter(s).",), name=f"{WORKER_THREAD_PREFIX}royalroad")
        worker.start()
        worker.join(5)
        with frame._log_lock:
            queued = list(frame._log_queue)
        assert queued == ["[Royal Road] Found 2 new chapter(s).\n"]
    finally:
        frame._clip_timer.Stop()
        frame.Destroy()


# ── The reader's chapter list ────────────────────────────────────


def a_book(tmp_path: Path, titles):
    from ficary.exporters import export_epub
    from ficary.models import Chapter, Story

    story = Story(id=1, title="Lanterns", author="Someone", summary="", url="https://www.fanfiction.net/s/1/1/")
    story.chapters = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(titles, 1)]
    return Path(export_epub(story, str(tmp_path)))


def test_the_reader_knows_each_chapters_heading_without_loading_it(tmp_path):
    from ficary.reader.source import StorySource

    source = StorySource.from_file(a_book(tmp_path, ["Prologue", "The Inn", "", "Chapter 3 - Low Water"]))
    source._loader = lambda number: pytest.fail("a heading needs no chapter text")
    headings = [source.chapter_heading(n) for n in range(1, source.chapter_count() + 1)]
    assert headings == ["Prologue", "Chapter 1. The Inn", "Chapter 2", "Chapter 3 - Low Water"]


@needs_display
def test_the_readers_chapter_list_and_jump_to_show_the_headings(wx_app, tmp_path):
    from ficary import gui
    from ficary.reader.gui import ReaderFrame
    from ficary.reader.source import StorySource

    frame = gui.MainFrame()
    reader = ReaderFrame(frame, frame.prefs, StorySource.from_file(a_book(tmp_path, ["The Gate", "The Inn"])))
    try:
        listed = [reader.chapter_list.GetItemText(row) for row in range(reader.chapter_list.GetItemCount())]
        assert listed == ["Chapter 1. The Gate", "Chapter 2. The Inn"]
        assert reader.jump.GetStrings() == listed
    finally:
        reader.Destroy()
        frame._clip_timer.Stop()
        frame.Destroy()
