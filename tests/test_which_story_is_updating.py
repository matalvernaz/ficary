"""Which story an update is on, said where it can be found.

Matt, 2026-11-24: "When a bunch of stories are updating, it's pretty impossible to find the name of the story that's currently being updated in the status window." Each story got a line naming its file, then a line for every chapter (one story that night got 88) and a block of its details, and the line that started the download named the story by number. The checking before it said each story's answer three times over.

Now each story gets one line in the library window's status pane when it starts and one when it ends, both naming it by its title. What it is doing in between, the chapter it is on and then saving, goes to a status bar on the library and main windows, which NVDA+End reads without moving focus. The checking gives each story one line. The command line, with no status bar, keeps its chapter lines.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import types
from pathlib import Path

import pytest

from ficary import activity, cli
from ficary.jobs import DownloadJob
from ficary.models import Chapter, Story
from ficary.scraper import RateLimitError
from ficary.updater import ChaptersNotReadableError

from .test_library_update_cancel_stops_the_story import wait_until_idle

_site_numbers = itertools.count(1)

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


# ── What's happening now ─────────────────────────────────────────


class TestTheRecord:
    def test_a_job_says_what_it_is_doing_until_it_ends(self):
        job = activity.Job("Checking.")
        assert activity.current() == "Checking."
        job.say("Checking more.")
        assert activity.current() == "Checking more."
        job.end()
        assert activity.current() == ""

    def test_two_jobs_are_both_said_and_one_ending_leaves_the_other(self):
        first = activity.Job("Updating one.")
        second = activity.Job("Updating two.")
        assert activity.current() == "Updating one. Updating two."
        first.end()
        assert activity.current() == "Updating two."
        second.end()

    def test_a_job_ends_however_its_block_ends(self):
        with pytest.raises(ValueError):
            with activity.Job("Updating."):
                raise ValueError("the site broke")
        assert activity.current() == ""

    def test_a_late_report_does_not_bring_an_ended_job_back(self):
        job = activity.Job("Updating.")
        job.end()
        job.say("Updating, chapter 9 of 9.")
        assert activity.current() == ""

    def test_the_last_result_shows_until_another_job_starts(self):
        activity.finished("Library update complete.")
        assert activity.current() == "Library update complete."
        job = activity.Job("Downloading.")
        assert activity.current() == "Downloading."
        job.end()
        # Old news by now; it must not come back.
        assert activity.current() == ""

    def test_a_job_can_end_with_its_result(self):
        job = activity.Job("Downloading.")
        job.end("Downloaded.")
        assert activity.current() == "Downloaded."


# ── A fake site and library ──────────────────────────────────────


class TitledSite:
    """A site whose stories have ``chapters`` chapters. It notes what the status bar would say right after each chapter is reported, and ``on_chapter`` runs just before each is reported."""

    def __init__(self, chapters: int = 4, on_chapter=None, fail_with: Exception | None = None):
        self.site_name = f"which-story-site-{next(_site_numbers)}"
        self.concurrency = 1
        self.chapters = chapters
        self.on_chapter = on_chapter or (lambda url, number: None)
        self.fail_with = fail_with
        self.status_bar_said: list[str] = []
        self.counts: dict[str, object] = {}
        self.checking_said: list[str] = []

    def parse_story_id(self, url):
        return int(url.rsplit("/", 1)[1])

    def probe_chapter_count(self, url):
        self.checking_said.append(activity.current())
        answer = self.counts[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
        if self.fail_with is not None:
            raise self.fail_with
        story = Story(id=self.parse_story_id(url), title="The site's title", author="Nobody", summary="", url=url)
        for number in range(skip_chapters + 1, self.chapters + 1):
            story.chapters.append(Chapter(number=number, title=f"Chapter {number}", html=f"<p>Chapter {number}.</p>"))
            self.on_chapter(url, number)
            if progress_callback:
                progress_callback(number, self.chapters, f"Chapter {number}", False)
            self.status_bar_said.append(activity.current())
        return story


def plug_in(monkeypatch, site: TitledSite) -> None:
    class SiteClass:
        site_name = site.site_name

    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: site)
    monkeypatch.setattr(cli, "_detect_site", lambda url: SiteClass)


@pytest.fixture
def saving(monkeypatch):
    """Stands in for reading the old book (one chapter) and writing the new one. Returns what the status bar said while the book was being saved."""
    said: list[str] = []

    def export(story, folder, **options):
        said.append(activity.current())
        return Path(options.get("output_path") or Path(folder) / "story.epub")

    monkeypatch.setattr(cli, "read_chapters", lambda path: [Chapter(number=1, title="Chapter 1", html="<p>Chapter 1.</p>")])
    monkeypatch.setattr(cli, "_merge_chapter_lists", lambda old, new: (list(old) + list(new), 0))
    monkeypatch.setitem(cli.EXPORTERS, "epub", export)
    return said


def stories(tmp_path: Path, *titles, local: int = 1, remote: int | None = 4) -> list[dict]:
    made = []
    for number, title in enumerate(titles, 1):
        path = tmp_path / f"Fandom/story-{number}.epub"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"the book before the update")
        entry = {
            "path": path, "rel": f"Fandom/story-{number}.epub",
            "url": f"https://example.invalid/s/{number}",
            "local": local, "title": title,
        }
        if remote is not None:
            entry["remote"] = remote
        made.append(entry)
    return made


def update(queue, site, *, in_status_bar=True, cancel=None, **job):
    """Runs a library update of ``queue`` and returns its status pane lines."""
    lines: list[str] = []
    cli._run_update_queue(
        queue, DownloadJob(skip_complete=False, **job), workers=1,
        skipped_count=0, label="Library update",
        progress=lines.append, cancel_event=cancel or threading.Event(),
        chapters_in_status_bar=in_status_bar,
    )
    # A story runs on its site's queue; let it finish posting.
    wait_until_idle(site.site_name)
    return lines


# ── Each story's lines ───────────────────────────────────────────


class TestEachStory:
    def test_it_gets_a_line_when_it_starts_and_one_when_it_ends_naming_it(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story", "Second Story"), site)
        assert 'Updating 1 of 2: "First Story", 3 new chapters.' in lines
        assert 'Updated "First Story": 4 chapters, 3 new.' in lines
        assert 'Updating 2 of 2: "Second Story", 3 new chapters.' in lines
        assert 'Updated "Second Story": 4 chapters, 3 new.' in lines

    def test_its_chapters_and_details_stay_out_of_the_pane(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story", "Second Story"), site)
        first = lines.index('Updating 1 of 2: "First Story", 3 new chapters.')
        summary = next(index for index, line in enumerate(lines) if line.startswith("\n====="))
        assert lines[first:summary] == [
            'Updating 1 of 2: "First Story", 3 new chapters.',
            'Updated "First Story": 4 chapters, 3 new.',
            'Updating 2 of 2: "Second Story", 3 new chapters.',
            'Updated "Second Story": 4 chapters, 3 new.',
        ]
        # Two lines a story in all: an earlier check found these, so
        # nothing is asked and nothing said about them before they start.
        for title in ("First Story", "Second Story"):
            assert len([line for line in lines if f'"{title}"' in line]) == 2, title
        assert not any("Checking" in line for line in lines)

    def test_the_status_bar_says_which_story_and_chapter(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        update(stories(tmp_path, "First Story"), site)
        assert site.status_bar_said == [
            'Updating 1 of 1: "First Story", chapter 2 of 4.',
            'Updating 1 of 1: "First Story", chapter 3 of 4.',
            'Updating 1 of 1: "First Story", chapter 4 of 4.',
        ]

    def test_then_saving_and_then_how_it_went(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        update(stories(tmp_path, "First Story"), site)
        assert saving == ['Updating 1 of 1: "First Story", saving.']
        assert activity.current().startswith("Library update complete — 1 updated, 0 up to date, 0 failed")

    def test_no_new_chapters_after_all_is_not_called_updated(self, tmp_path, monkeypatch, saving):
        """The check said 4; the site gave nothing past the one already here."""
        site = TitledSite(chapters=1)
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert '"First Story" had no new chapters after all.' in lines
        assert not any(line.startswith('Updated "First Story"') for line in lines)

    def test_fresh_copies_say_every_chapter_came_again(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site, refetch_all=True)
        assert 'Updating 1 of 1: "First Story", 3 new chapters, downloading every chapter again.' in lines
        assert 'Updated "First Story": downloaded all 4 chapters again.' in lines

    def test_an_old_file_written_again_does_not_claim_every_chapter_is_new(self, tmp_path, monkeypatch, saving):
        def unreadable(path):
            raise ChaptersNotReadableError("not one of Ficary's own files")

        monkeypatch.setattr(cli, "read_chapters", unreadable)
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert 'Updated "First Story": 4 chapters.' in lines
        assert any("[legacy-format]" in line for line in lines), "why every chapter was fetched is still said"

    def test_a_story_that_fails_is_named(self, tmp_path, monkeypatch, saving):
        site = TitledSite(fail_with=ValueError("The site said no."))
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert 'Could not update "First Story": The site said no.' in lines
        failed = lines.index("Failed:")
        assert lines[failed + 1:failed + 3] == ["  First Story", "    → download failed (see log above)"]

    def test_a_story_that_breaks_is_named(self, tmp_path, monkeypatch, saving):
        site = TitledSite(fail_with=RuntimeError("the connection dropped"))
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert 'Could not update "First Story": the connection dropped' in lines
        assert not any("Download failed" in line for line in lines)

    def test_a_rate_limit_is_named_without_command_line_advice(self, tmp_path, monkeypatch, saving):
        site = TitledSite(fail_with=RateLimitError("too many requests"))
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert 'Could not update "First Story". Rate limited: too many requests' in lines
        assert not any("--delay-min" in line for line in lines)

    def test_a_cancelled_story_is_named_and_its_sentence_goes(self, tmp_path, monkeypatch, saving):
        cancel = threading.Event()
        site = TitledSite(chapters=6, on_chapter=lambda url, number: number == 3 and cancel.set())
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story", remote=6), site, cancel=cancel)
        assert 'Stopped part-way through "First Story"; its file was not changed. The next update carries on with it.' in lines
        # It stopped at the chapter it was on, as on the command line.
        assert site.status_bar_said == ['Updating 1 of 1: "First Story", chapter 2 of 6.']
        assert '"First Story"' not in activity.current()
        assert activity.current().startswith("Library update cancelled")

    def test_without_a_title_the_file_name_names_it(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        queue = stories(tmp_path, None)
        queue[0]["rel"] = "Fandom/Some Story - Someone.epub"
        lines = update(queue, site)
        assert 'Updating 1 of 1: "Some Story - Someone", 3 new chapters.' in lines

    def test_a_title_over_several_lines_is_said_on_one(self, tmp_path, monkeypatch, saving):
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First\n   Story"), site)
        assert 'Updating 1 of 1: "First Story", 3 new chapters.' in lines

    def test_the_command_line_keeps_its_chapter_lines(self, tmp_path, monkeypatch, saving):
        """No status bar there: a line per chapter is how it shows it is still going."""
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site, in_status_bar=False)
        assert 'Updating 1 of 1: "First Story", 3 new chapters.' in lines
        assert "  [2/4] Chapter 2" in lines
        assert "  [4/4] Chapter 4" in lines
        assert any(line.startswith("\nSaved to:") for line in lines)
        assert not any(line.startswith('Updated "First Story"') for line in lines)

    def test_the_command_lines_cancel_and_failure_lines_are_unchanged(self, tmp_path, monkeypatch, saving):
        site = TitledSite(fail_with=RuntimeError("the connection dropped"))
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site, in_status_bar=False)
        assert "  Download failed: the connection dropped" in lines


# ── The checking before it ───────────────────────────────────────


class TestTheChecking:
    def checked(self, tmp_path, monkeypatch, counts, *, local=1, dry_run=True, **options):
        site = TitledSite()
        plug_in(monkeypatch, site)
        queue = stories(tmp_path, *[f"Story {letter}" for letter in "ABC"[:len(counts)]], local=local, remote=None)
        for entry, count in zip(queue, counts):
            site.counts[entry["url"]] = count
        for entry, extra in zip(queue, options.get("extra", [{}] * len(queue))):
            entry.update(extra)
        lines = update(queue, site, dry_run=dry_run)
        return site, lines

    def test_each_story_gets_one_line_with_its_answer(self, tmp_path, monkeypatch):
        _site, lines = self.checked(tmp_path, monkeypatch, [1, 4, 0])
        assert '  1 of 3: "Story A", up to date, 1 chapter' in lines
        assert '  2 of 3: "Story B", 3 new chapters, 4 on the site' in lines
        assert '  3 of 3: "Story C", the site has 0 chapters, fewer than the 1 here, so it is left alone' in lines
        for title in ("Story A", "Story B", "Story C"):
            assert len([line for line in lines if f'"{title}"' in line]) == 1, title
        assert not any("local /" in line or "[1/3]" in line for line in lines)

    def test_the_status_bar_counts_them(self, tmp_path, monkeypatch):
        # The first is up to date, so it must not count as one with new chapters.
        site, _lines = self.checked(tmp_path, monkeypatch, [1, 4, 4])
        assert site.checking_said == [
            "Checking for new chapters: 0 of 3 stories checked.",
            "Checking for new chapters: 1 of 3 stories checked.",
            "Checking for new chapters: 2 of 3 stories checked, 1 with new chapters so far.",
        ]
        # The checking is over; a dry run has no result of its own to show.
        assert activity.current() == ""

    def test_one_already_known_to_have_new_chapters_is_not_asked_or_listed(self, tmp_path, monkeypatch):
        """Download New Chapters, or a story an earlier check found: it is named when it downloads."""
        site, lines = self.checked(tmp_path, monkeypatch, [None, 2], extra=[{"remote": 4}, {}])
        assert "\nChecking 1 story for new chapters..." in lines
        assert '  1 of 1: "Story B", 1 new chapter, 2 on the site' in lines
        assert not any('"Story A"' in line for line in lines[:lines.index("")])
        assert site.checking_said == ["Checking for new chapters: 0 of 1 story checked."]
        # The dry run's summary still lists it.
        assert "  Story A  (1 -> 4)" in lines

    def test_one_that_could_not_be_checked_is_named_and_listed_at_the_end(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.WARNING, logger="ficary.cli")
        _site, lines = self.checked(tmp_path, monkeypatch, [RateLimitError("slow down")], dry_run=False)
        assert '  1 of 1: "Story A", could not be checked: slow down' in lines
        failed = lines.index("Failed:")
        assert lines[failed + 1:failed + 3] == ["  Story A", "    → could not be checked: slow down"]
        # The log names the file, to find it by.
        assert "Fandom/story-1.epub failed: could not be checked: slow down" in caplog.text

    def test_a_cancelled_check_lists_nothing_story_by_story(self, tmp_path, monkeypatch):
        site = TitledSite()
        plug_in(monkeypatch, site)
        queue = stories(tmp_path, "Story A", "Story B", remote=None)
        cancel = threading.Event()
        cancel.set()
        lines = update(queue, site, cancel=cancel, dry_run=False)
        assert not any('"Story A"' in line or '"Story B"' in line for line in lines)
        assert any("2 of 2 entries were not checked" in line for line in lines)
        assert activity.current().startswith("Library update cancelled")


# ── One story on its own ─────────────────────────────────────────


class TestOneStoryOnItsOwn:
    def test_the_brief_report_names_a_failure(self, tmp_path, monkeypatch, saving):
        site = TitledSite(fail_with=ValueError("The site said no."))
        plug_in(monkeypatch, site)
        lines: list[str] = []
        path = tmp_path / "story.epub"
        path.write_bytes(b"the book")
        ok = cli._download_one(
            "https://example.invalid/s/1", DownloadJob(format="epub"), tmp_path,
            update_path=path, existing_chapters=1, status_callback=lines.append,
            brief=cli.BriefReport("A Story", lambda now: None),
        )
        assert ok is False
        assert lines == ['Could not update "A Story": The site said no.']

    def test_with_the_ai_check_on_the_status_bar_says_so(self, tmp_path, monkeypatch, saving):
        """The check can take minutes, which "saving" alone would not explain."""
        monkeypatch.setattr(cli, "_llm_strip_notes_config", lambda args: {"provider": "ollama", "model": "a-model"})
        site = TitledSite()
        plug_in(monkeypatch, site)
        update(stories(tmp_path, "First Story"), site)
        assert saving == ['Updating 1 of 1: "First Story", checking for author\'s notes, then saving.']

    def test_the_ai_note_checks_line_per_chapter_stays_out_of_the_pane(self, tmp_path, monkeypatch, saving):
        """Its tally at the end still reaches it."""
        def export(story, folder, **options):
            options["progress"]("  [AI notes] chapter 1: cache hit")
            options["progress"]("  [AI notes] chapter 2: no A/N paragraphs found")
            options["progress"]("  AI note check: 2 chapters checked, notes removed from 0.")
            return Path(options["output_path"])

        monkeypatch.setitem(cli.EXPORTERS, "epub", export)
        site = TitledSite()
        plug_in(monkeypatch, site)
        lines = update(stories(tmp_path, "First Story"), site)
        assert not any("[AI notes]" in line for line in lines)
        assert "  AI note check: 2 chapters checked, notes removed from 0." in lines


# ── The library window ───────────────────────────────────────────


def test_the_library_window_asks_for_the_status_bar(tmp_path, monkeypatch):
    pytest.importorskip("wx")
    from ficary.library.gui import LibraryFrame

    from .library_fixtures import ficary_epub
    from ficary.library.scanner import scan

    root = tmp_path / "library"
    root.mkdir()
    ficary_epub(root, title="Story 101", url="https://www.fanfiction.net/s/101/1/", chapters=3, story_id=101)
    scan(root)
    asked = {}
    monkeypatch.setattr(cli, "_run_update_queue", lambda queue, *args, **options: asked.update(options, queue=queue))
    window = types.SimpleNamespace(_post_status=lambda line: None)
    LibraryFrame._probe_one_root(
        window, root.resolve(), force=True, refetch_all=False, recheck_interval=0,
        cancel_event=threading.Event(),
    )
    assert asked["chapters_in_status_bar"] is True
    assert [entry["title"] for entry in asked["queue"]] == ["Story 101"]


@needs_display
class TestTheStatusBars:
    def test_the_main_window_has_one_that_follows_what_is_happening(self, wx_app):
        import wx
        from ficary import gui
        from ficary.gui_status_bar import ActivityStatusBar, IDLE_TEXT

        frame = gui.MainFrame()
        try:
            bar = frame.GetStatusBar()
            assert isinstance(bar, ActivityStatusBar)
            assert bar.GetStatusText() == IDLE_TEXT
            with activity.Job('Updating 3 of 12: "First Story", chapter 21 of 25.'):
                bar.refresh()
                assert bar.GetStatusText() == 'Updating 3 of 12: "First Story", chapter 21 of 25.'
            bar.refresh()
            assert bar.GetStatusText() == IDLE_TEXT
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_it_keeps_up_on_its_own(self, wx_app):
        import time

        import wx
        from ficary import gui

        frame = gui.MainFrame()
        try:
            bar = frame.GetStatusBar()
            with activity.Job("Checking for new chapters: 4 of 9 stories checked."):
                deadline = time.monotonic() + 5
                while bar.GetStatusText() != "Checking for new chapters: 4 of 9 stories checked." and time.monotonic() < deadline:
                    wx.Yield()
                    time.sleep(0.02)
                assert bar.GetStatusText() == "Checking for new chapters: 4 of 9 stories checked."
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_it_never_shortens_a_long_sentence(self, wx_app):
        """wx's default style puts "..." in place of what doesn't fit, and on Windows the screen reader reads the shortened text."""
        import wx
        from ficary import gui

        frame = gui.MainFrame()
        try:
            bar = frame.GetStatusBar()
            for flag in (wx.STB_ELLIPSIZE_END, wx.STB_ELLIPSIZE_MIDDLE, wx.STB_ELLIPSIZE_START, wx.STB_SHOW_TIPS):
                assert not bar.HasFlag(flag)
            sentence = 'Updating 1 of 2: "' + "A Very Long Title " * 20 + '", chapter 1 of 9.'
            with activity.Job(sentence):
                bar.refresh()
                assert bar.GetStatusText() == sentence
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_the_library_window_has_one_and_says_where_it_is_once(self, wx_app, monkeypatch, tmp_path):
        import wx
        from ficary import gui
        from ficary.gui_status_bar import ActivityStatusBar
        from ficary.library import gui as library_gui

        monkeypatch.setattr(library_gui.threading, "Thread", lambda *a, **k: type("NotStarted", (), {"start": lambda self: None})())
        frame = gui.MainFrame()
        library = library_gui.LibraryFrame(frame, frame.prefs)
        try:
            assert isinstance(library.GetStatusBar(), ActivityStatusBar)
            monkeypatch.setattr(library, "_current_path", lambda: tmp_path)
            library._on_check_updates(None)
            library._update_running = False
            library._set_busy(False)
            library._on_check_updates(None)
            library.status_ctrl.flush()
            assert library.status_ctrl.GetValue().count(library_gui.STATUS_BAR_HINT) == 1
        finally:
            library._update_running = False
            library.Destroy()
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_the_library_windows_result_stays_in_the_bar(self, wx_app, monkeypatch):
        import wx
        from ficary import gui
        from ficary.library import gui as library_gui

        frame = gui.MainFrame()
        library = library_gui.LibraryFrame(frame, frame.prefs)
        try:
            library._update_summaries = [
                "Library update complete — 2 updated, 5 up to date, 0 failed, 1 skipped.",
                "Library update complete — 0 updated, 3 up to date, 0 failed, 0 skipped.",
            ]
            library._update_finished()
            assert activity.current() == " ".join([
                "Library update complete — 2 updated, 5 up to date, 0 failed, 1 skipped.",
                "Library update complete — 0 updated, 3 up to date, 0 failed, 0 skipped.",
            ])
        finally:
            library.Destroy()
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()


# ── The main window's downloads ──────────────────────────────────


class NotingSite:
    """A site whose stories have ``chapters`` chapters. It notes what the status bar would say right after each chapter is reported."""

    site_name = "royalroad"
    concurrency = 1

    def __init__(self, chapters: int = 3, fail_with: Exception | None = None):
        self.chapters = chapters
        self.fail_with = fail_with
        self.status_bar_said: list[str] = []

    def parse_story_id(self, url):
        return int(url.split("/")[4])

    def is_forum_url(self, url):
        return False

    def is_author_url(self, url):
        return False

    def download(self, url, progress_callback=None, skip_chapters=0, chapters=None):
        if self.fail_with is not None:
            raise self.fail_with
        story = Story(id=self.parse_story_id(url), title=f"Story {self.parse_story_id(url)}", author="Nobody", summary="", url=url)
        for number in range(skip_chapters + 1, self.chapters + 1):
            story.chapters.append(Chapter(number=number, title=f"Chapter {number}", html=f"<p>{number}</p>"))
            if progress_callback:
                progress_callback(number, self.chapters, f"Chapter {number}", False)
            self.status_bar_said.append(activity.current())
        return story


def royal_road(number: int) -> str:
    return f"https://www.royalroad.com/fiction/{number}/a-story"


@pytest.fixture
def main_window(wx_app, monkeypatch, tmp_path):
    """A real main window whose exports are noted, with what the status bar said at the time, instead of written."""
    from ficary import announce, gui

    frame = gui.MainFrame()
    frame.saving_said = []

    def export(story, params, update_path=None, job=None, doing=None):
        frame.saving_said.append(activity.current())
        return tmp_path / f"{story.id}.epub"

    monkeypatch.setattr(frame, "_export_story", export)
    monkeypatch.setattr(announce, "announce", lambda text: None)
    with frame._log_lock:
        frame._log_queue.clear()
    yield frame
    frame._clip_timer.Stop()
    frame.Destroy()


def download_params(window, **changes):
    from ficary import gui

    return gui._DownloadParams(
        fmt=changes.pop("fmt", "epub"), raw_output_dir="", filename_template="{title}",
        hr_as_stars=False, strip_notes=False, llm_strip_notes=changes.pop("llm_strip_notes", False),
        stop_generation=window._stop_generation, **changes,
    )


def logged(window) -> str:
    with window._log_lock:
        return "".join(window._log_queue)


@needs_display
class TestTheMainWindowsDownloads:
    def test_a_download_keeps_its_chapters_in_the_status_bar(self, main_window, monkeypatch):
        site = NotingSite()
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: site)
        outcome = main_window._run_download(royal_road(7), params=download_params(main_window))
        assert outcome.ok
        assert site.status_bar_said == [
            "Downloading from Royal Road, chapter 1 of 3.",
            "Downloading from Royal Road, chapter 2 of 3.",
            "Downloading from Royal Road, chapter 3 of 3.",
        ]
        assert "Chapter 2" not in logged(main_window)
        assert "Title:    Story 7" in logged(main_window)

    def test_once_done_the_bar_says_how_it_went(self, main_window, monkeypatch):
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: NotingSite())
        main_window._run_download(royal_road(7), params=download_params(main_window))
        assert activity.current() == "Downloaded Story 7."

    def test_a_failed_download_says_so_in_the_bar(self, main_window, monkeypatch):
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: NotingSite(fail_with=RuntimeError("HTTP 500")))
        main_window._run_download(royal_road(7), params=download_params(main_window))
        assert activity.current() == "A download failed. The status pane says why."

    def test_an_update_names_its_file(self, main_window, monkeypatch, tmp_path):
        from ficary import updater

        site = NotingSite()
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: site)
        monkeypatch.setattr(updater, "read_chapters", lambda path: [Chapter(number=1, title="Chapter 1", html="<p>1</p>")])
        book = tmp_path / "Some Story - Someone.epub"
        book.write_bytes(b"the book")
        main_window._run_download(
            royal_road(7), skip_chapters=1, is_update=True, update_path=book,
            params=download_params(main_window),
        )
        assert site.status_bar_said == [
            'Updating "Some Story - Someone", chapter 2 of 3.',
            'Updating "Some Story - Someone", chapter 3 of 3.',
        ]

    def test_where_progress_went_is_said_once(self, main_window, monkeypatch):
        from ficary.gui_status_bar import HINT

        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: NotingSite())
        main_window._run_download(royal_road(7), params=download_params(main_window))
        main_window._run_download(royal_road(8), params=download_params(main_window))
        assert logged(main_window).count(HINT) == 1

    def test_a_picked_batch_names_each_story(self, main_window, monkeypatch):
        site = NotingSite(chapters=2)
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: site)
        urls = [royal_road(11), royal_road(12)]
        main_window._run_picked_batch(
            urls, "author", params=download_params(main_window),
            titles={urls[0]: "The First One", urls[1]: "The\n  Second One"},
        )
        assert '\nDownloading 1 of 2: "The First One"\n' in logged(main_window)
        assert '\nDownloading 2 of 2: "The Second One"\n' in logged(main_window)
        assert site.status_bar_said == [
            'Downloading 1 of 2: "The First One", chapter 1 of 2.',
            'Downloading 1 of 2: "The First One", chapter 2 of 2.',
            'Downloading 2 of 2: "The Second One", chapter 1 of 2.',
            'Downloading 2 of 2: "The Second One", chapter 2 of 2.',
        ]
        assert 'Saved "Story 11":' in logged(main_window)
        assert "Chapter 1" not in logged(main_window)

    def test_a_picked_story_without_a_title_is_named_by_its_site(self, main_window, monkeypatch):
        site = NotingSite(chapters=1)
        monkeypatch.setattr(main_window, "_scraper_for", lambda *a, **k: site)
        main_window._run_picked_batch([royal_road(11)], "author", params=download_params(main_window))
        assert site.status_bar_said == ["Downloading 1 of 1 from Royal Road, chapter 1 of 1."]
        assert f"\n[1/1] {royal_road(11)}\n" in logged(main_window)

    def test_a_series_batch_names_the_series(self, main_window):
        site = NotingSite(chapters=1)
        main_window._batch_download(
            [royal_road(21), royal_road(22)], site, params=download_params(main_window),
            series_name="The Long Saga",
        )
        assert site.status_bar_said == [
            'Downloading part 1 of 2 of "The Long Saga", chapter 1 of 1.',
            'Downloading part 2 of 2 of "The Long Saga", chapter 1 of 1.',
        ]

    def test_a_batch_leaves_no_sentence_behind(self, main_window):
        site = NotingSite(chapters=1, fail_with=RuntimeError("HTTP 500"))
        main_window._batch_download([royal_road(21)], site, params=download_params(main_window))
        assert activity.current() == ""

    def test_the_picker_hands_the_batch_each_storys_title(self, main_window, monkeypatch):
        from ficary import gui

        handed = {}

        class Thread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, **rest):
                handed.update(kwargs or {})

            def start(self):
                pass

        class AuthorPage(NotingSite):
            def is_author_url(self, url):
                return True

            def scrape_author_works(self, url, **options):
                return "Someone", [
                    {"title": "The First One", "url": royal_road(11), "author": "Someone", "section": "own"},
                    {"title": royal_road(12), "url": royal_road(12), "author": "Someone", "section": "own"},
                ]

        monkeypatch.setattr(gui.threading, "Thread", Thread)
        monkeypatch.setattr(gui.wx, "CallAfter", lambda function, *args: function(*args))
        monkeypatch.setattr(main_window, "_open_picker", lambda title, works, on_ok: on_ok([royal_road(11), royal_road(12)]))
        main_window._run_picker_download("https://www.royalroad.com/profile/1", AuthorPage(), kind="author", params=download_params(main_window))
        # A title that is only the address says nothing, so it is left out.
        assert handed["titles"] == {royal_road(11): "The First One"}


@needs_display
class TestTheMainWindowsSaving:
    @pytest.fixture
    def window(self, wx_app, monkeypatch):
        from ficary import gui

        frame = gui.MainFrame()
        monkeypatch.setattr(frame, "_auto_index_download", lambda path: None)
        with frame._log_lock:
            frame._log_queue.clear()
        yield frame
        frame._clip_timer.Stop()
        frame.Destroy()

    def a_story(self):
        story = Story(id=7, title="The Loaf", author="Nobody", summary="", url=royal_road(7))
        story.chapters.append(Chapter(number=1, title="Chapter 1", html="<p>1</p>"))
        return story

    def test_saving_is_said_in_the_bar_and_the_ai_checks_chapter_lines_stay_out(self, window, monkeypatch, tmp_path):
        from ficary import exporters

        said = []

        def export(story, folder, **options):
            said.append(activity.current())
            options["progress"]("  [AI notes] chapter 1: cache hit")
            options["progress"]("  AI note check: 1 chapter checked, notes removed from 0.")
            return Path(options["output_path"])

        monkeypatch.setitem(exporters.EXPORTERS, "epub", export)
        with activity.Job("Downloading from Royal Road.") as job:
            window._export_story(
                self.a_story(), download_params(window), update_path=tmp_path / "The Loaf.epub",
                job=job, doing="Downloading from Royal Road",
            )
        assert said == ["Downloading from Royal Road, saving."]
        assert "[AI notes]" not in logged(window)
        assert "AI note check: 1 chapter checked" in logged(window)

    def test_with_the_ai_check_on_the_bar_says_so(self, window, monkeypatch, tmp_path):
        from ficary import exporters

        said = []
        monkeypatch.setitem(exporters.EXPORTERS, "epub", lambda story, folder, **options: said.append(activity.current()) or Path(options["output_path"]))
        params = download_params(
            window, llm_strip_notes=True,
            llm_render_config={"provider": "ollama", "model": "a-model"},
        )
        with activity.Job("Downloading from Royal Road.") as job:
            window._export_story(
                self.a_story(), params, update_path=tmp_path / "The Loaf.epub",
                job=job, doing="Downloading from Royal Road",
            )
        assert said == ["Downloading from Royal Road, checking for author's notes, then saving."]

    def test_an_audiobooks_chapters_are_said_in_the_bar(self, window, monkeypatch, tmp_path):
        from ficary import tts

        said = []

        def render(story, folder, **options):
            options["progress_callback"](1, 2, "Chapter 1")
            said.append(activity.current())
            return tmp_path / "The Loaf.m4b"

        monkeypatch.setattr(tts, "generate_audiobook", render)
        window._export_story(self.a_story(), download_params(window, fmt="audio"), update_path=tmp_path / "The Loaf.m4b")
        assert said == ['Making the audiobook of "The Loaf", chapter 1 of 2.']
        assert "Synthesizing" not in logged(window)
        # Its own sentence goes when the audiobook is done.
        assert activity.current() == ""

    def test_an_audiobook_in_a_download_uses_the_downloads_sentence(self, window, monkeypatch, tmp_path):
        from ficary import tts

        def render(story, folder, **options):
            options["progress_callback"](2, 2, "Chapter 2")
            return tmp_path / "The Loaf.m4b"

        monkeypatch.setattr(tts, "generate_audiobook", render)
        with activity.Job("Downloading from Royal Road.") as job:
            window._export_story(
                self.a_story(), download_params(window, fmt="audio"), update_path=tmp_path / "The Loaf.m4b",
                job=job, doing="Downloading from Royal Road",
            )
            assert activity.current() == 'Making the audiobook of "The Loaf", chapter 2 of 2.'


@needs_display
def test_the_library_windows_controls_all_fit_in_it(wx_app):
    """The buttons were one row far wider than the window, so most of them were drawn past its right-hand edge; and the status bar made the window shorter still."""
    import wx
    from ficary import gui
    from ficary.library.gui import LibraryFrame

    frame = gui.MainFrame()
    library = LibraryFrame(frame, frame.prefs)
    try:
        library.Show()
        library.Layout()
        for _ in range(5):
            wx.SafeYield()
        width = library.GetClientSize().width
        controls = [
            library.scan_btn, library.reorg_btn, library.download_new_btn, library.update_btn,
            library.force_recheck_chk, library.refetch_all_chk, library.review_btn,
            library.browse_stories_btn, library.cancel_btn, library.FindWindowById(wx.ID_CLOSE),
        ]
        for control in controls:
            # Squeezed is as bad as off the edge: GTK shrinks what does
            # not fit, down to nothing.
            assert control.GetSize().width >= control.GetBestSize().width - 2, control.GetLabel()
            assert control.GetRect().GetRight() <= width, control.GetLabel()
        # Room left for the status pane to be read.
        assert library.status_ctrl.GetSize().height >= 80
    finally:
        library.Destroy()
        frame._clip_timer.Stop()
        frame.Destroy()
        wx.SafeYield()


def test_the_line_before_an_update_counts_what_it_will_do(tmp_path, monkeypatch):
    """It said "TTL 6.0h: N recently-probed story(ies) skipped, M to probe", counting finished stories as recently checked and stories already known to have new chapters as ones to ask about."""
    pytest.importorskip("wx")
    from datetime import datetime, timedelta, timezone

    from ficary.library.gui import LibraryFrame
    from ficary.library.index import LibraryIndex
    from ficary.library.scanner import scan

    from .library_fixtures import ficary_epub

    root = tmp_path / "library"
    root.mkdir()
    for number in (101, 102, 103, 104):
        ficary_epub(root, title=f"Story {number}", url=f"https://www.fanfiction.net/s/{number}/1/", chapters=3, story_id=number)
    scan(root)
    index = LibraryIndex.load()
    stories = index.library_state(root.resolve())["stories"]
    recently = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for url, entry in stories.items():
        if "/s/102" in url:
            entry["remote_chapter_count"] = 5
        elif "/s/103" in url:
            entry["last_probed"] = recently
        elif "/s/104" in url:
            entry["status"] = "Complete"
    index.save()
    monkeypatch.setattr(cli, "_run_update_queue", lambda queue, *args, **options: None)
    posted: list[str] = []
    window = types.SimpleNamespace(_post_status=posted.append)
    LibraryFrame._probe_one_root(
        window, root.resolve(), force=False, refetch_all=False, recheck_interval=6 * 60 * 60,
        cancel_event=threading.Event(),
    )
    assert (
        "1 to check; 1 already known to have new chapters; 2 skipped as finished, "
        "abandoned or checked in the last 6 hours (Force recheck includes them)."
    ) in posted
    # Nor is the known one listed by the queue builder: it is named when it downloads.
    assert not any("[resume]" in line for line in posted)
