"""A site that takes chapters down: the book keeps them, and updates add only the chapters it doesn't have.

Matt, 2026-11-24: "It should figure out that a book has had chapters removed and not remove them from the downloaded copy, only add new ones." Royal Road authors take chapters down, often most of a story when it goes to Kindle Unlimited, and go on posting new ones. Updates counted chapters by position, so after a takedown a new chapter could sit at a position the book already covered and be skipped, and a fresh copy dropped the chapters the site no longer had.

Each chapter now carries the site's own id for it where the site has one that stays put, the book records it, and an update matches the book's chapters to the site's by that id (by title for books made before ids were recorded).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ficary.exporters import EXPORTERS
from ficary.models import Chapter, Story
from ficary.updater import read_chapters


# ── The book records each chapter's site id ──────────────────────


@pytest.mark.parametrize("fmt", ["html", "epub"])
def test_a_book_keeps_each_chapters_site_id(tmp_path, fmt):
    story = Story(id=1, title="A Story", author="Someone", summary="", url="https://www.royalroad.com/fiction/1/a-story")
    for number, site_id in enumerate(["1001", "", "1003"], 1):
        story.chapters.append(Chapter(number=number, title=f"Chapter {number} - A Title", html=f"<p>Text {number}.</p>", site_id=site_id))
    book = Path(EXPORTERS[fmt](story, str(tmp_path), template="{title}"))
    back = read_chapters(book)
    assert [(chapter.title, chapter.site_id) for chapter in back] == [
        ("Chapter 1 - A Title", "1001"),
        ("Chapter 2 - A Title", ""),
        ("Chapter 3 - A Title", "1003"),
    ]


def test_a_chapter_without_one_writes_no_attribute(tmp_path):
    story = Story(id=1, title="A Story", author="Someone", summary="", url="https://www.fanfiction.net/s/1")
    story.chapters.append(Chapter(number=1, title="One", html="<p>Text.</p>"))
    book = Path(EXPORTERS["html"](story, str(tmp_path), template="{title}"))
    assert "data-site-id" not in book.read_text(encoding="utf-8")


def test_the_shared_chapter_loop_records_the_sites_ids(tmp_path):
    """Royal Road, ScribbleHub and MediaMiner list each chapter with the site's id."""
    from ficary.royalroad import RoyalRoadScraper

    scraper = RoyalRoadScraper(cache_dir=tmp_path)
    chapter_list = [
        {"id": 5001, "url": "https://www.royalroad.com/fiction/1/a/chapter/5001/one", "title": "One"},
        {"id": 5002, "url": "https://www.royalroad.com/fiction/1/a/chapter/5002/two", "title": "Two"},
    ]
    bodies = {item["url"]: f"<html><body><div class='chapter-content'><p>{item['title']}</p></div></body></html>" for item in chapter_list}
    scraper._fetch_batches = lambda urls: iter([[(bodies[url], None) for url in urls]])
    first = scraper._materialise_chapters(
        story_id=1, chapter_list=chapter_list, skip_chapters=0, chapter_spec=None,
        parse_chapter=lambda soup: str(soup.find("p")), progress_callback=None,
    )
    assert [chapter.site_id for chapter in first] == ["5001", "5002"]
    # From the cache the second time, still with their ids.
    scraper._fetch_batches = lambda urls: pytest.fail("everything should come from the cache")
    again = scraper._materialise_chapters(
        story_id=1, chapter_list=chapter_list, skip_chapters=0, chapter_spec=None,
        parse_chapter=lambda soup: str(soup.find("p")), progress_callback=None,
    )
    assert [chapter.site_id for chapter in again] == ["5001", "5002"]


# ── Matching a book's chapters to the site's ─────────────────────

from ficary import chapter_match  # noqa: E402
from ficary.chapter_match import CannotMatch, SiteChapter  # noqa: E402


def book_of(*names, ids=True):
    """A book whose chapters are named ``names``; each has the site id "id-<name>" unless ``ids`` is off."""
    return [
        Chapter(number=number, title=f"Chapter {name}", html=f"<p>{name}</p>", site_id=f"id-{name}" if ids else "")
        for number, name in enumerate(names, 1)
    ]


def site_of(*names):
    return [SiteChapter(position=position, site_id=f"id-{name}", title=f"Chapter {name}") for position, name in enumerate(names, 1)]


def downloaded(site, positions):
    by_position = {chapter.position: chapter for chapter in site}
    return {
        position: Chapter(number=position, title=by_position[position].title, html=f"<p>new {position}</p>", site_id=by_position[position].site_id)
        for position in positions
    }


def titles(chapters):
    """Each chapter's name; a chapter read back from a book titled just "Chapter N" has it in its heading."""
    return [(chapter.title or chapter.heading).removeprefix("Chapter ") for chapter in chapters]


@pytest.mark.parametrize("ids", [True, False], ids=["by id", "by title"])
class TestMatching:
    def test_chapters_taken_down_at_the_end_stay_and_new_ones_follow(self, ids):
        """Matt's Sky Pride: the book has 1 to 6; the site took 5 and 6 down and posted 7 and 8."""
        book = book_of("1", "2", "3", "4", "5", "6", ids=ids)
        site = site_of("1", "2", "3", "4", "7", "8")
        plan = chapter_match.plan_update(book, site)
        assert plan.new_positions == [5, 6]
        merged = chapter_match.apply_plan(book, downloaded(site, plan.new_positions), plan)
        assert titles(merged) == ["1", "2", "3", "4", "5", "6", "7", "8"]
        assert [chapter.number for chapter in merged] == list(range(1, 9))

    def test_most_of_a_story_taken_down_still_finds_the_new_chapters(self, ids):
        """Kindle Unlimited: a few chapters left as a sample, the rest gone, new ones going up. The site has fewer chapters than the book, so counting would never have seen them."""
        book = book_of(*[str(number) for number in range(1, 21)], ids=ids)
        site = site_of("1", "2", "3", "19", "20", "21", "22")
        plan = chapter_match.plan_update(book, site)
        assert plan.new_positions == [6, 7]
        merged = chapter_match.apply_plan(book, downloaded(site, plan.new_positions), plan)
        assert titles(merged) == [str(number) for number in range(1, 23)]

    def test_nothing_new_when_the_site_only_took_chapters_down(self, ids):
        book = book_of("1", "2", "3", "4", ids=ids)
        site = site_of("1", "2")
        assert chapter_match.plan_update(book, site).new_positions == []

    def test_a_book_with_none_of_the_sites_chapters_is_refused(self, ids):
        with pytest.raises(CannotMatch):
            chapter_match.plan_update(book_of("1", "2", ids=ids), site_of("8", "9"))


def test_by_id_a_chapter_inserted_mid_story_goes_where_the_site_has_it():
    book = book_of("1", "2", "3")
    site = site_of("1", "2", "2b", "3")
    plan = chapter_match.plan_update(book, site)
    merged = chapter_match.apply_plan(book, downloaded(site, plan.new_positions), plan)
    assert titles(merged) == ["1", "2", "2b", "3"]


def test_by_title_a_missing_title_further_back_is_taken_for_a_rename_not_a_new_chapter():
    book = book_of("1", "2", "3", ids=False)
    site = site_of("1", "2 renamed", "3", "4")
    plan = chapter_match.plan_update(book, site)
    assert plan.new_positions == [4]


def test_by_title_the_ids_of_matched_chapters_are_recorded_for_next_time():
    book = book_of("1", "2", ids=False)
    site = site_of("1", "2", "3")
    plan = chapter_match.plan_update(book, site)
    merged = chapter_match.apply_plan(book, downloaded(site, plan.new_positions), plan)
    assert [chapter.site_id for chapter in merged] == ["id-1", "id-2", "id-3"]


def test_a_fresh_copy_keeps_the_chapters_the_site_took_down():
    book = book_of("1", "2", "3", "4", "5")
    fresh = [Chapter(number=n, title=f"Chapter {name}", html=f"<p>fresh {name}</p>", site_id=f"id-{name}") for n, name in enumerate(["1", "2", "4", "6"], 1)]
    merged = chapter_match.keep_taken_down(book, fresh)
    assert titles(merged) == ["1", "2", "3", "4", "5", "6"]
    assert merged[0].html == "<p>fresh 1</p>", "the site's copy wins where it still has the chapter"
    assert merged[2].html == "<p>3</p>", "the book's copy is kept where the site took it down"


def test_the_guard_names_what_a_write_would_lose():
    old = book_of("1", "2", "3")
    assert chapter_match.dropped(old, book_of("1", "2", "3", "4")) == []
    lost = chapter_match.dropped(old, book_of("1", "3"))
    assert titles(lost) == ["2"]
    # A repeated title counts each copy.
    twice = book_of("A", "A", ids=False)
    assert titles(chapter_match.dropped(twice, book_of("A", ids=False))) == ["A"]


# ── Updating a Royal Road book ───────────────────────────────────

import threading  # noqa: E402

from ficary import cli  # noqa: E402
from ficary.jobs import DownloadJob  # noqa: E402


def royal_road_site(*names, fail_listing=False):
    """A Royal Road whose story lists chapters ``names`` (ids "id-<name>"), and hands out any of them by position. No network."""
    from ficary.royalroad import RoyalRoadScraper

    class MadeUpRoyalRoad(RoyalRoadScraper):
        site_name = "royalroad"
        concurrency = 1

        def __init__(self):
            super().__init__(use_cache=False)
            self.downloaded_positions: list[int] = []

        def _delay(self):
            pass

        def list_chapters(self, url_or_id):
            return site_of(*names)

        def probe_chapter_count(self, url_or_id):
            return len(names)

        def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
            from ficary.models import chapter_in_spec

            story = Story(id=1, title="Sky", author="Someone", summary="", url="https://www.royalroad.com/fiction/1/sky")
            for position, name in enumerate(names, 1):
                if position <= skip_chapters or not chapter_in_spec(position, chapters):
                    continue
                self.downloaded_positions.append(position)
                story.chapters.append(Chapter(number=position, title=f"Chapter {name}", html=f"<p>site {name}</p>", site_id=f"id-{name}"))
                if progress_callback:
                    progress_callback(position, len(names), f"Chapter {name}", False)
            return story

    return MadeUpRoyalRoad()


def a_book(folder: Path, *names, ids=True, fmt="html") -> Path:
    story = Story(id=1, title="Sky", author="Someone", summary="", url="https://www.royalroad.com/fiction/1/sky")
    story.chapters = book_of(*names, ids=ids)
    return Path(EXPORTERS[fmt](story, str(folder), template="{title}"))


def plug_in(monkeypatch, site) -> None:
    class SiteClass:
        site_name = "royalroad"

    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: site)
    monkeypatch.setattr(cli, "_detect_site", lambda url: SiteClass)
    monkeypatch.setattr(cli, "_auto_index_exported", lambda path: None)


def update_one(book: Path, site, *, existing=None, **job):
    lines: list[str] = []
    nothing_new = []
    ok = cli._download_one(
        "https://www.royalroad.com/fiction/1/sky", DownloadJob(format=book.suffix.lstrip("."), **job), book.parent,
        update_path=book, existing_chapters=existing if existing is not None else len(read_chapters(book)),
        status_callback=lines.append, brief=cli.BriefReport("Sky", lambda now: None),
        on_nothing_new=lambda: nothing_new.append(True),
    )
    return ok, lines, nothing_new


@pytest.mark.parametrize("ids", [True, False], ids=["book with ids", "book from before ids"])
class TestUpdatingARoyalRoadBook:
    def test_new_chapters_after_a_takedown_are_added_and_the_book_keeps_the_rest(self, tmp_path, monkeypatch, ids):
        book = a_book(tmp_path, "1", "2", "3", "4", "5", "6", ids=ids)
        site = royal_road_site("1", "2", "3", "4", "7", "8")
        plug_in(monkeypatch, site)
        ok, lines, _nothing_new = update_one(book, site)
        assert ok
        assert site.downloaded_positions == [5, 6], "only the two new chapters"
        assert titles(read_chapters(book)) == ["1", "2", "3", "4", "5", "6", "7", "8"]
        assert 'Updated "Sky": 8 chapters, 2 new.' in lines

    def test_a_takedown_alone_downloads_nothing_and_changes_nothing(self, tmp_path, monkeypatch, ids):
        book = a_book(tmp_path, "1", "2", "3", "4", ids=ids)
        before = book.read_bytes()
        site = royal_road_site("1", "2")
        plug_in(monkeypatch, site)
        ok, lines, nothing_new = update_one(book, site)
        assert ok and nothing_new
        assert site.downloaded_positions == []
        assert book.read_bytes() == before

    def test_a_fresh_copy_keeps_the_chapters_the_site_took_down(self, tmp_path, monkeypatch, ids):
        book = a_book(tmp_path, "1", "2", "3", "4", ids=ids)
        site = royal_road_site("1", "2", "4", "5")
        plug_in(monkeypatch, site)
        ok, lines, _nothing_new = update_one(book, site, refetch_all=True)
        assert ok
        chapters = read_chapters(book)
        assert titles(chapters) == ["1", "2", "3", "4", "5"]
        assert chapters[0].html.strip() == "<p>site 1</p>"
        assert chapters[2].html.strip() == "<p>3</p>"
        assert '"Sky" keeps 1 chapter the site no longer has.' in lines


def test_an_old_book_gets_the_sites_ids_written_in_once_matched(tmp_path, monkeypatch):
    book = a_book(tmp_path, "1", "2", "3", ids=False)
    site = royal_road_site("1", "2", "3", "4")
    plug_in(monkeypatch, site)
    update_one(book, site)
    assert [chapter.site_id for chapter in read_chapters(book)] == ["id-1", "id-2", "id-3", "id-4"]


def test_a_book_none_of_whose_chapters_can_be_found_updates_by_count_as_before(tmp_path, monkeypatch):
    """Nothing in the book matches the site (titles written some other way, every chapter renamed out of recognition): the update goes by chapter count, as every update did before, and doesn't fail every check."""
    book = a_book(tmp_path, "1", "2")
    site = royal_road_site("8", "9", "10")
    plug_in(monkeypatch, site)
    ok, _lines, _nothing_new = update_one(book, site)
    assert ok
    assert site.downloaded_positions == [3]
    assert titles(read_chapters(book)) == ["1", "2", "10"]


def test_the_check_of_such_a_book_counts_as_before(tmp_path, monkeypatch):
    lines, _site = check_lines(tmp_path, monkeypatch, ["1", "2"], ["8", "9", "10"])
    assert '  1 of 1: "Sky", 1 new chapter, 3 on the site' in lines


def test_the_guard_stops_any_write_that_would_lose_a_chapter(tmp_path, monkeypatch):
    """Whatever an update does, a book is never written with fewer of its chapters."""
    book = a_book(tmp_path, "1", "2", "3")
    before = book.read_bytes()
    site = royal_road_site("1", "2", "3", "4")
    plug_in(monkeypatch, site)
    real_apply = chapter_match.apply_plan
    monkeypatch.setattr(chapter_match, "apply_plan", lambda book, new, plan: real_apply(book, new, plan)[1:])
    ok, lines, _nothing_new = update_one(book, site)
    assert not ok
    assert 'Could not update "Sky": writing it would have lost 1 chapter the book has ("Chapter 1"), so it was left as it was' in lines
    assert book.read_bytes() == before


def test_a_book_that_cant_be_read_is_not_shrunk_by_a_fresh_copy(tmp_path, monkeypatch):
    book = tmp_path / "Sky.html"
    book.write_text("<html><body><p>not a book Ficary can read chapters from</p></body></html>", encoding="utf-8")
    site = royal_road_site("1", "2")
    plug_in(monkeypatch, site)
    ok, lines, _nothing_new = update_one(book, site, existing=5, refetch_all=True)
    assert not ok
    assert any("fewer than the 5 in the book" in line for line in lines)


# ── The check before an update ───────────────────────────────────


def check_lines(tmp_path, monkeypatch, book_names, site_names, *, dry_run=True):
    book = a_book(tmp_path, *book_names)
    site = royal_road_site(*site_names)
    plug_in(monkeypatch, site)
    lines: list[str] = []
    queue = [{"path": book, "rel": book.name, "url": "https://www.royalroad.com/fiction/1/sky", "local": len(book_names), "title": "Sky"}]
    cli._run_update_queue(
        queue, DownloadJob(skip_complete=False, dry_run=dry_run), workers=1, skipped_count=0,
        label="Library update", progress=lines.append, cancel_event=threading.Event(),
    )
    return lines, site


def test_the_check_sees_new_chapters_when_the_site_has_fewer_than_the_book(tmp_path, monkeypatch):
    lines, _site = check_lines(tmp_path, monkeypatch, ["1", "2", "3", "4", "5", "6"], ["1", "2", "3", "7"])
    assert '  1 of 1: "Sky", 1 new chapter, 4 on the site; your copy keeps the 3 the site took down' in lines


def test_the_check_says_the_book_keeps_what_the_site_took_down(tmp_path, monkeypatch):
    lines, _site = check_lines(tmp_path, monkeypatch, ["1", "2", "3", "4", "5", "6"], ["1", "2", "3", "4"])
    assert '  1 of 1: "Sky", up to date, 6 chapters; your copy keeps the 2 the site took down' in lines


# ── The main window's own update (File > Update Existing File) ───

import os  # noqa: E402

needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


@needs_display
class TestTheMainWindowsUpdate:
    @pytest.fixture
    def window(self, wx_app, monkeypatch):
        import wx
        from ficary import announce, gui

        frame = gui.MainFrame()
        frame.written = []
        monkeypatch.setattr(announce, "announce", lambda text: None)
        monkeypatch.setattr(
            frame, "_export_story",
            lambda story, params, update_path=None, job=None, doing=None: frame.written.append(list(story.chapters)) or update_path,
        )
        yield frame
        frame._clip_timer.Stop()
        frame.Destroy()
        wx.SafeYield()

    def update(self, window, monkeypatch, book, site, *, refetch_all=False):
        from ficary import gui

        monkeypatch.setattr(window, "_scraper_for", lambda *a, **k: site)
        params = gui._DownloadParams(
            fmt="html", raw_output_dir="", filename_template="{title}",
            hr_as_stars=False, strip_notes=False, llm_strip_notes=False,
            stop_generation=window._stop_generation,
        )
        return window._run_download(
            "https://www.royalroad.com/fiction/1/sky", skip_chapters=len(read_chapters(book)),
            is_update=True, update_path=book, refetch_all=refetch_all, params=params,
        )

    def logged(self, window):
        with window._log_lock:
            return "".join(window._log_queue)

    def test_new_chapters_after_a_takedown_are_added_and_the_book_keeps_the_rest(self, window, monkeypatch, tmp_path):
        book = a_book(tmp_path, "1", "2", "3", "4", "5", "6")
        site = royal_road_site("1", "2", "3", "4", "7", "8")
        outcome = self.update(window, monkeypatch, book, site)
        assert outcome.ok
        assert site.downloaded_positions == [5, 6]
        (written,) = window.written
        assert titles(written) == ["1", "2", "3", "4", "5", "6", "7", "8"]

    def test_a_takedown_alone_is_up_to_date(self, window, monkeypatch, tmp_path):
        book = a_book(tmp_path, "1", "2", "3", "4")
        site = royal_road_site("1", "2")
        outcome = self.update(window, monkeypatch, book, site)
        assert outcome.ok
        assert window.written == []
        assert "Up to date. No new chapters." in self.logged(window)

    def test_a_fresh_copy_keeps_the_chapters_the_site_took_down(self, window, monkeypatch, tmp_path):
        book = a_book(tmp_path, "1", "2", "3", "4")
        site = royal_road_site("1", "2", "4")
        self.update(window, monkeypatch, book, site, refetch_all=True)
        (written,) = window.written
        assert titles(written) == ["1", "2", "3", "4"]
        assert "Kept 1 chapter the site no longer has." in self.logged(window)

    def test_a_fresh_copy_of_a_book_from_before_ids_keeps_the_chapters_the_site_took_down(self, window, monkeypatch, tmp_path):
        """Royal Road's "Chapter 3" is the author's own: the book's chapter 3 is the one the site took down, not a made-up title that moved."""
        book = a_book(tmp_path, "1", "2", "3", "4", ids=False)
        site = royal_road_site("1", "2", "4")
        outcome = self.update(window, monkeypatch, book, site, refetch_all=True)
        assert outcome.ok
        (written,) = window.written
        assert titles(written) == ["1", "2", "3", "4"]

    def test_a_book_none_of_whose_chapters_can_be_found_updates_by_count(self, window, monkeypatch, tmp_path):
        book = a_book(tmp_path, "1", "2")
        site = royal_road_site("8", "9", "10")
        outcome = self.update(window, monkeypatch, book, site)
        assert outcome.ok
        assert site.downloaded_positions == [3]
        (written,) = window.written
        assert titles(written) == ["1", "2", "10"]

    def test_the_guard_stops_a_write_that_would_lose_a_chapter(self, window, monkeypatch, tmp_path):
        book = a_book(tmp_path, "1", "2", "3")
        site = royal_road_site("1", "2", "3", "4")
        real_apply = chapter_match.apply_plan
        monkeypatch.setattr(chapter_match, "apply_plan", lambda book, new, plan: real_apply(book, new, plan)[1:])
        outcome = self.update(window, monkeypatch, book, site)
        assert not outcome.ok
        assert window.written == []
        assert "writing it would have lost 1 chapter the book has" in self.logged(window)


def test_a_fresh_copy_that_matches_nothing_in_the_book_is_refused_not_doubled():
    """Titles an older version wrote differently, or an author renamed every chapter: keeping every "taken down" chapter would put the story in twice."""
    book = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(["Alpha", "Beta", "Gamma"], 1)]
    fresh = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(["One", "Two", "Three"], 1)]
    with pytest.raises(CannotMatch):
        chapter_match.keep_taken_down(book, fresh)


def test_a_fresh_copy_where_chapter_n_became_part_n_takes_them_for_the_same_chapters():
    book = book_of("1", "2", "3", ids=False)
    fresh = [Chapter(number=n, title=f"Part {n}", html=f"<p>{n}</p>") for n in (1, 2, 3)]
    assert [chapter.title for chapter in chapter_match.keep_taken_down(book, fresh)] == ["Part 1", "Part 2", "Part 3"]


# ── Every shape of title, through a real book, and the update after ──
#
# From a review of the first version (2026-11-24): a renamed chapter
# skipped by one update looked new to the next, once the chapters
# around it had ids; and a title doesn't always come back from a book
# as it went in ("Chapter 3. Foo" reads back as "Foo", a bare "3" is
# written as "Chapter 3").


def a_real_book(folder: Path, chapter_titles, *, fmt="html", ids=None):
    """The chapters of a book written by the real exporter with these titles, read back: what an update really matches against."""
    story = Story(id=1, title="Sky", author="Someone", summary="", url="https://www.royalroad.com/fiction/1/sky")
    story.chapters = [
        Chapter(number=number, title=title, html=f"<p>text {number}</p>", site_id=ids[number - 1] if ids else "")
        for number, title in enumerate(chapter_titles, 1)
    ]
    return read_chapters(Path(EXPORTERS[fmt](story, str(folder), template="{title}")))


def listed(chapter_titles, ids):
    return [SiteChapter(position=position, site_id=site_id, title=title) for position, (title, site_id) in enumerate(zip(chapter_titles, ids), 1)]


def update(book, site):
    """One update by plan: (new site positions, the book after)."""
    plan = chapter_match.plan_update(book, site)
    new = {
        chapter.position: Chapter(number=chapter.position, title=chapter.title, html=f"<p>new {chapter.position}</p>", site_id=chapter.site_id)
        for chapter in site if chapter.position in plan.new_positions
    }
    return plan.new_positions, chapter_match.apply_plan(book, new, plan)


EVERY_SHAPE = [
    "Prologue",              # structural, written as it is
    "Chapter 1. Arrival",    # reads back from the book as "Arrival"
    "Chapter 2 - The Road",  # written as it is
    "The Inn",               # written "Chapter 3. The Inn"
    "Chapter 4",             # reads back with no title
    "5",                     # a bare number, written "Chapter 5"
    "",                      # no title, written "Chapter 6"
    "Interlude",
    "Chapter 7: Home",
]
SHAPE_IDS = [f"id-{n}" for n in range(1, len(EVERY_SHAPE) + 1)]


@pytest.mark.parametrize("fmt", ["html", "epub"])
@pytest.mark.parametrize("book_ids", [False, True], ids=["book from before ids", "book with ids"])
class TestEveryShapeOfTitle:
    def test_the_same_list_is_nothing_new_twice(self, tmp_path, fmt, book_ids):
        book = a_real_book(tmp_path, EVERY_SHAPE, fmt=fmt, ids=SHAPE_IDS if book_ids else None)
        site = listed(EVERY_SHAPE, SHAPE_IDS)
        new, after = update(book, site)
        assert new == []
        assert [chapter.site_id for chapter in after] == SHAPE_IDS, "every chapter's id is recorded"
        assert chapter_match.plan_update(after, site).new_positions == []

    def test_a_takedown_and_a_new_chapter(self, tmp_path, fmt, book_ids):
        book = a_real_book(tmp_path, EVERY_SHAPE, fmt=fmt, ids=SHAPE_IDS if book_ids else None)
        kept = [index for index, title in enumerate(EVERY_SHAPE) if title != "The Inn"]
        site = listed([EVERY_SHAPE[index] for index in kept] + ["Epilogue"], [SHAPE_IDS[index] for index in kept] + ["id-new"])
        new, after = update(book, site)
        assert new == [9]
        assert len(after) == len(EVERY_SHAPE) + 1
        assert after[3].html == book[3].html, "the chapter the site took down stays where it was"
        assert after[-1].title == "Epilogue"
        assert chapter_match.plan_update(after, site).new_positions == []

    def test_a_renamed_chapter_is_not_new_now_or_next_time(self, tmp_path, fmt, book_ids):
        book = a_real_book(tmp_path, EVERY_SHAPE, fmt=fmt, ids=SHAPE_IDS if book_ids else None)
        renamed = [title.replace("The Road", "The Long Road") for title in EVERY_SHAPE]
        site = listed(renamed + ["Epilogue"], SHAPE_IDS + ["id-new"])
        new, after = update(book, site)
        assert new == [10]
        assert chapter_match.plan_update(after, site).new_positions == []


@pytest.mark.parametrize("fmt", ["html", "epub"])
def test_titles_that_lose_their_chapter_number_in_the_book_still_match(tmp_path, fmt):
    """Every title "Chapter N. Something": the book reads them back as "Something"."""
    chapter_titles = [f"Chapter {n}. Day {n}" for n in range(1, 6)]
    book = a_real_book(tmp_path, chapter_titles, fmt=fmt)
    assert [chapter.title for chapter in book] == [f"Day {n}" for n in range(1, 6)]
    site = listed(chapter_titles + ["Chapter 6. Day 6"], [f"id-{n}" for n in range(1, 7)])
    new, after = update(book, site)
    assert new == [6]
    assert chapter_match.plan_update(after, site).new_positions == []


def test_a_chapter_inserted_among_titles_that_lose_their_numbers_is_the_only_new_one(tmp_path):
    """The book reads "Chapter 2. Middle" back as "Middle"; matched in order by that alone, the inserted chapter would be taken for chapter 2 and chapter 2 for one the site took down."""
    chapter_titles = ["Chapter 1. Start", "Chapter 2. Middle", "Chapter 3. End"]
    book = a_real_book(tmp_path, chapter_titles)
    site = listed(["Chapter 1. Start", "Chapter 1.5. Interlude", "Chapter 2. Middle", "Chapter 3. End"], ["a", "x", "b", "c"])
    new, after = update(book, site)
    assert new == [2]
    assert [chapter.site_id for chapter in after] == ["a", "x", "b", "c"]


def test_bare_numbers_after_an_unnumbered_first_chapter_go_by_place_not_by_number(tmp_path):
    """A book writes a bare-number title with its own count: after "Synopsis", the author's "1" is the book's "Chapter 2". Matching by number would take the site's "2" for it."""
    chapter_titles = ["Synopsis", "1", "2", "3", "4"]
    book = a_real_book(tmp_path, chapter_titles)
    assert [chapter.heading for chapter in book][1:3] == ["Chapter 2", "Chapter 3"]
    site = listed(chapter_titles + ["5"], [f"id-{n}" for n in range(1, 7)])
    new, after = update(book, site)
    assert new == [6]
    assert [chapter.site_id for chapter in after] == [f"id-{n}" for n in range(1, 7)]
    assert chapter_match.plan_update(after, site).new_positions == []


def test_by_title_a_chapter_the_book_has_no_counterpart_for_between_two_it_has_is_new():
    book = book_of("1", "3", ids=False)
    site = site_of("1", "2", "3")
    assert chapter_match.plan_update(book, site).new_positions == [2]


def test_by_id_an_unmatched_chapter_where_the_book_has_an_unmatched_one_is_not_added():
    """The book's chapter there has no id (an update before ids skipped it), and two the site has there don't look like it: either could be it, renamed. Adding both could put it in twice."""
    book = [
        Chapter(number=1, title="Beginning", html="<p>1</p>", site_id="id-1"),
        Chapter(number=2, title="Middle", html="<p>2</p>"),
        Chapter(number=3, title="End", html="<p>3</p>", site_id="id-3"),
    ]
    site = [SiteChapter(1, "id-1", "Beginning"), SiteChapter(2, "id-x", "Crossroads"), SiteChapter(3, "id-y", "Detour"), SiteChapter(4, "id-3", "End")]
    assert chapter_match.plan_update(book, site).new_positions == []


def test_by_id_a_renamed_chapter_beside_a_new_one_leaves_just_the_new_one():
    book = book_of("1", "2", "3")
    book[1] = Chapter(number=2, title="Chapter 2", html="<p>2</p>", site_id="")
    site = site_of("1", "2: Renamed", "2b", "3")
    plan = chapter_match.plan_update(book, site)
    assert plan.new_positions == [3]
    assert plan.book_site_ids == {1: "id-2: Renamed"}


def test_a_titled_chapter_taken_down_before_untitled_ones_doesnt_hide_them(tmp_path):
    """After the last matched chapter: the book's titled chapter the site took down is passed over, and the untitled ones after it still go by place."""
    book = a_real_book(tmp_path, ["Synopsis", "Side Story", "1", "2"])
    site = listed(["Synopsis", "1", "2", "3"], ["s", "a", "b", "c"])
    assert chapter_match.plan_update(book, site).new_positions == [4]


def test_repeated_titles_pair_in_order():
    """Two chapters called "Interlude", the first taken down: the site's is the book's second, not its first."""
    book = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(["Interlude", "Battle", "Interlude", "Aftermath"], 1)]
    site = [SiteChapter(1, "b", "Battle"), SiteChapter(2, "i2", "Interlude"), SiteChapter(3, "a", "Aftermath"), SiteChapter(4, "n", "Next")]
    plan = chapter_match.plan_update(book, site)
    assert plan.new_positions == [4]
    assert plan.book_site_ids == {1: "b", 2: "i2", 3: "a"}


def test_by_id_a_new_chapter_titled_like_one_taken_down_is_still_new():
    book = [
        Chapter(number=1, title="Battle", html="<p>1</p>", site_id="b"),
        Chapter(number=2, title="Interlude", html="<p>2</p>", site_id="i1"),
        Chapter(number=3, title="Aftermath", html="<p>3</p>", site_id="a"),
    ]
    site = [SiteChapter(1, "b", "Battle"), SiteChapter(2, "a", "Aftermath"), SiteChapter(3, "i2", "Interlude")]
    assert chapter_match.plan_update(book, site).new_positions == [3]


def test_a_new_chapter_whose_title_starts_like_a_taken_down_ones_is_still_new():
    """"Chapter 90" starts with "Chapter 9", but they are different chapters."""
    book = book_of(*[str(n) for n in range(1, 10)], ids=False)
    site = site_of(*[str(n) for n in range(1, 9)], "90")
    assert chapter_match.plan_update(book, site).new_positions == [9]


def test_a_typo_fixed_in_the_last_chapters_title_is_not_a_new_chapter():
    book = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(["Alpha", "Beta", "The Grand Batle"], 1)]
    site = [SiteChapter(n, f"id-{n}", title) for n, title in enumerate(["Alpha", "Beta", "The Grand Battle", "Next"], 1)]
    assert chapter_match.plan_update(book, site).new_positions == [4]


REPOSTED = ["Chapter 1 - Dawn", "Chapter 2 - Road", "Chapter 3 - Inn", "Chapter 4 - Storm", "Chapter 5 - Ambush", "Chapter 6 - Retreat"]
REPOSTED_ON_THE_SITE = REPOSTED[:4] + ["Chapter 5 - Rewritten", "Chapter 6 - Aftermath"]


def test_chapters_taken_down_and_new_ones_posted_under_the_same_numbers_are_new(tmp_path):
    """The author takes "Chapter 5 - Ambush" and "Chapter 6 - Retreat" down and posts "Chapter 5 - Rewritten" and "Chapter 6 - Aftermath". The same numbers don't make them the same chapters: taken for renames, the new ones would never be downloaded, and their ids would be written onto the old ones for good."""
    book = a_real_book(tmp_path, REPOSTED)
    site = listed(REPOSTED_ON_THE_SITE, ["a", "b", "c", "d", "x5", "x6"])
    new, after = update(book, site)
    assert new == [5, 6]
    assert [chapter.title for chapter in after][4:] == ["Chapter 5 - Ambush", "Chapter 6 - Retreat", "Chapter 5 - Rewritten", "Chapter 6 - Aftermath"]
    assert chapter_match.plan_update(after, site).new_positions == []


def test_a_fresh_copy_keeps_chapters_reposted_under_the_same_numbers(tmp_path):
    book = a_real_book(tmp_path, REPOSTED)
    fresh = fresh_chapters(REPOSTED_ON_THE_SITE, ids=["a", "b", "c", "d", "x5", "x6"])
    merged = chapter_match.keep_taken_down(book, fresh, titles_from_author=True)
    assert [chapter.title for chapter in merged][4:] == ["Chapter 5 - Ambush", "Chapter 6 - Retreat", "Chapter 5 - Rewritten", "Chapter 6 - Aftermath"]
    assert chapter_match.dropped(book, merged, titles_from_author=True) == []


def test_titles_that_differ_only_in_their_number_are_different_chapters(tmp_path):
    book = a_real_book(tmp_path, ["Prologue", "Day 1", "Day 2", "Day 3"])
    site = listed(["Prologue", "Day 1", "Day 2", "Day 4"], ["p", "a", "b", "d"])
    assert chapter_match.plan_update(book, site).new_positions == [4]


def test_a_subtitle_added_to_the_last_chapter_is_not_a_new_chapter():
    book = [Chapter(number=n, title=title, html=f"<p>{n}</p>") for n, title in enumerate(["Alpha", "Beta"], 1)]
    site = [SiteChapter(n, f"id-{n}", title) for n, title in enumerate(["Alpha", "Beta (revised)", "Gamma"], 1)]
    assert chapter_match.plan_update(book, site).new_positions == [3]


# ── Fresh copies: titles a site makes up from the chapter's place ──


def fresh_chapters(chapter_titles, ids=None):
    return [Chapter(number=n, title=title, html=f"<p>fresh {n}</p>", site_id=ids[n - 1] if ids else "") for n, title in enumerate(chapter_titles, 1)]


def test_a_fresh_copy_of_untitled_chapters_with_the_same_count_keeps_nothing_extra(tmp_path):
    book = a_real_book(tmp_path, [f"Chapter {n}" for n in range(1, 6)])
    fresh = fresh_chapters([f"Chapter {n}" for n in range(1, 6)])
    merged = chapter_match.keep_taken_down(book, fresh)
    assert [chapter.html for chapter in merged] == [chapter.html for chapter in fresh]
    assert chapter_match.dropped(book, merged) == []


def test_a_fresh_copy_of_untitled_chapters_one_short_is_refused_not_guessed(tmp_path):
    """"Chapter N" made up from the place: once one is gone every number after it moves, and nothing says which went. Keeping the last would double one and lose another."""
    book = a_real_book(tmp_path, [f"Chapter {n}" for n in range(1, 6)])
    with pytest.raises(CannotMatch, match="no titles of their own"):
        chapter_match.keep_taken_down(book, fresh_chapters([f"Chapter {n}" for n in range(1, 5)]))


def test_a_fresh_copy_keeps_a_titled_chapter_taken_down_from_between_untitled_ones(tmp_path):
    """The site numbers its untitled chapters by their place: once "Battle" went, the book's "Chapter 3" became the site's "Chapter 2"."""
    book = a_real_book(tmp_path, ["Chapter 1", "Battle", "Chapter 3"])
    fresh = fresh_chapters(["Chapter 1", "Chapter 2"])
    merged = chapter_match.keep_taken_down(book, fresh)
    assert [chapter.html for chapter in merged] == [fresh[0].html, book[1].html, fresh[1].html]


def test_a_fresh_copy_takes_a_renamed_chapter_for_the_same_one(tmp_path):
    book = a_real_book(tmp_path, ["Alpha", "Beta", "Gamma"])
    fresh = fresh_chapters(["Alpha", "Beta, revised", "Gamma"])
    merged = chapter_match.keep_taken_down(book, fresh)
    assert [chapter.title for chapter in merged] == ["Alpha", "Beta, revised", "Gamma"]
    assert chapter_match.dropped(book, merged) == []


def test_a_fresh_copy_keeps_a_titled_chapter_the_site_took_down_in_its_place(tmp_path):
    book = a_real_book(tmp_path, ["Alpha", "Beta", "Gamma"])
    fresh = fresh_chapters(["Alpha", "Gamma", "Delta"])
    merged = chapter_match.keep_taken_down(book, fresh)
    assert [chapter.title for chapter in merged] == ["Alpha", "Beta", "Gamma", "Delta"]
    assert merged[1].html == book[1].html


def test_a_royal_road_fresh_copy_knows_chapter_n_is_the_authors_own(tmp_path):
    book = a_real_book(tmp_path, [f"Chapter {n}" for n in range(1, 5)])
    fresh = fresh_chapters(["Chapter 1", "Chapter 2", "Chapter 4", "Chapter 5"], ids=["a", "b", "d", "e"])
    merged = chapter_match.keep_taken_down(book, fresh, titles_from_author=True)
    assert [chapter.title or chapter.heading for chapter in merged] == ["Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4", "Chapter 5"]
    assert merged[2].html == book[2].html


def test_the_guard_still_sees_a_chapter_swapped_for_a_different_one(tmp_path):
    book = a_real_book(tmp_path, ["Alpha", "Beta", "Gamma"])
    swapped = [book[0], Chapter(number=2, title="Omega", html="<p>other</p>"), book[2]]
    assert [chapter.title for chapter in chapter_match.dropped(book, swapped)] == ["Beta"]


# ── Royal Road's own download, by place ──────────────────────────


def test_royal_road_downloads_just_the_chapters_an_update_asks_for(monkeypatch):
    """The update asks Royal Road for chapters by their place in its list; every other test stands a made-up site in for it."""
    from ficary.royalroad import RoyalRoadScraper

    page = (Path(__file__).parent / "fixtures" / "royalroad_fiction.html").read_text(encoding="utf-8")
    scraper = RoyalRoadScraper(use_cache=False)
    monkeypatch.setattr(scraper, "_fetch", lambda url, session=None: page)
    asked: list[str] = []

    def chapter_pages(urls):
        asked.extend(urls)
        yield [(f'<div class="chapter-inner"><p>page {url}</p></div>', None) for url in urls]

    monkeypatch.setattr(scraper, "_fetch_batches", chapter_pages)
    listing = scraper.list_chapters("https://www.royalroad.com/fiction/1/sky")
    assert len(listing) > 40
    wanted = [listing[2], listing[40]]
    story = scraper.download("https://www.royalroad.com/fiction/1/sky", chapters=[(chapter.position, chapter.position) for chapter in wanted])
    assert [chapter.number for chapter in story.chapters] == [chapter.position for chapter in wanted]
    assert [chapter.site_id for chapter in story.chapters] == [chapter.site_id for chapter in wanted]
    assert [chapter.title for chapter in story.chapters] == [chapter.title for chapter in wanted]
    assert len(asked) == 2


def test_the_check_names_the_site_as_it_goes_by(tmp_path, monkeypatch):
    """It said "Checking 1 royalroad story", the site's code name."""
    lines, _site = check_lines(tmp_path, monkeypatch, ["1", "2"], ["1", "2"])
    assert "  Checking 1 Royal Road story, 1 at a time..." in lines
