"""Chapters fetched through the shared chapter loop are saved and reported as they arrive.

Royal Road, ScribbleHub, FicWad and MediaMiner download through ``BaseScraper._materialise_chapters``. It used to fetch every chapter before saving or reporting any of them. A long story showed nothing for minutes and then printed every chapter line at once, and one chapter that failed near the end threw away all the others, so the next try started again from chapter one.

Every test here drives the real batching code with a faked ``_fetch`` and reads the order of events off one shared list. Nothing is timed.
"""

from __future__ import annotations

import threading

import pytest

from ficary.scraper import BaseScraper

BATCH = 3


def descriptor(number: int) -> dict:
    return {"url": f"https://example.invalid/chapter/{number}", "title": f"Chapter {number}"}


def body_of(soup) -> str:
    return soup.find(id="ct").decode_contents()


class RecordingScraper(BaseScraper):
    """A scraper whose network is a list: every fetch, pause and report is written down in the order it happened."""

    site_name = "batchsite"

    def __init__(self, cache_dir, *, concurrency: int = BATCH, failing: tuple = (), unparseable: tuple = ()):
        super().__init__(use_cache=True, cache_dir=cache_dir, concurrency=concurrency)
        self.events: list[tuple] = []
        self.events_lock = threading.Lock()
        self.failing = set(failing)
        self.unparseable = set(unparseable)
        self.rate_limited_on: int | None = None

    def note(self, *event) -> None:
        with self.events_lock:
            self.events.append(event)

    def _fetch(self, url, session=None, **kwargs):
        number = int(url.rsplit("/", 1)[1])
        self.note("fetch", number)
        if number == self.rate_limited_on:
            # What _fetch does to the scraper when the site answers 429.
            with self._state_lock:
                self._current_delay += 5.0
        if number in self.failing:
            raise ConnectionResetError(f"chapter {number}: connection reset")
        if number in self.unparseable:
            return "<p>The site sent something that is not a chapter.</p>"
        return f"<div id='ct'>Body {number}</div>"

    def _delay(self, fetches: int = 1) -> None:
        self.note("pause", fetches)

    def report(self, number, total, title, cached) -> None:
        self.note("report", number, "cached" if cached else "fetched", threading.current_thread().name)

    def download_chapters(self, count: int, **options):
        return self._materialise_chapters(
            story_id=77,
            chapter_list=[descriptor(number) for number in range(1, count + 1)],
            skip_chapters=options.pop("skip_chapters", 0),
            chapter_spec=options.pop("chapter_spec", None),
            parse_chapter=body_of,
            progress_callback=options.pop("progress_callback", self.report),
            **options,
        )

    def position(self, *event_start) -> int:
        """Where the first event starting with these values is in the list."""
        for index, event in enumerate(self.events):
            if event[: len(event_start)] == event_start:
                return index
        raise AssertionError(f"{event_start} never happened; events were {self.events}")

    def fetched_numbers(self) -> list[int]:
        return sorted(event[1] for event in self.events if event[0] == "fetch")

    def reported_numbers(self) -> list[int]:
        return [event[1] for event in self.events if event[0] == "report"]

    def cached_numbers(self) -> list[int]:
        from ficary.scraper import chapter_cache_key

        return [
            number for number in range(1, 40)
            if self._load_chapter_cache(77, number, cache_key=chapter_cache_key(descriptor(number)["url"])) is not None
        ]


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


class TestProgressComesAsTheChaptersDo:
    def test_a_batch_is_reported_before_the_next_one_is_fetched(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        chapters = scraper.download_chapters(9)
        assert [chapter.number for chapter in chapters] == list(range(1, 10))
        for last_of_batch, first_of_next in ((3, 4), (6, 7)):
            assert scraper.position("report", last_of_batch) < scraper.position("fetch", first_of_next)

    def test_every_chapter_is_reported_once_and_in_order(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(8)
        assert scraper.reported_numbers() == list(range(1, 9))

    def test_a_chapter_is_in_the_cache_before_it_is_reported(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        seen_in_cache: dict[int, bool] = {}

        def report(number, total, title, cached):
            seen_in_cache[number] = number in scraper.cached_numbers()

        scraper.download_chapters(5, progress_callback=report)
        assert seen_in_cache == {1: True, 2: True, 3: True, 4: True, 5: True}

    def test_one_at_a_time_reports_each_chapter_before_fetching_the_next(self, cache_dir):
        """What a site falls back to after a rate limit, and what any site does with concurrency 1."""
        scraper = RecordingScraper(cache_dir, concurrency=1)
        scraper.download_chapters(4)
        for number in (1, 2, 3):
            assert scraper.position("report", number) < scraper.position("fetch", number + 1)

    def test_reports_are_made_on_the_thread_that_asked_for_the_download(self, cache_dir):
        """The status pane takes a line's site prefix from the name of the thread that posts it, so a report from a fetch thread would lose it."""
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(6)
        threads = {event[3] for event in scraper.events if event[0] == "report"}
        assert threads == {threading.current_thread().name}


class TestAFailureKeepsWhatArrived:
    def test_the_chapters_before_the_failure_are_kept(self, cache_dir):
        scraper = RecordingScraper(cache_dir, failing=(10,))
        with pytest.raises(ConnectionResetError, match="chapter 10"):
            scraper.download_chapters(12)
        assert set(range(1, 10)) <= set(scraper.cached_numbers())

    def test_the_next_try_fetches_only_what_is_missing(self, cache_dir):
        first = RecordingScraper(cache_dir, failing=(10,))
        with pytest.raises(ConnectionResetError):
            first.download_chapters(12)
        second = RecordingScraper(cache_dir)
        chapters = second.download_chapters(12)
        assert [chapter.number for chapter in chapters] == list(range(1, 13))
        assert second.fetched_numbers() == [10]

    def test_chapters_that_arrived_beside_the_failed_one_are_kept_too(self, cache_dir):
        """Chapters 10, 11 and 12 are fetched together. 11 and 12 arrived; fetching them again would only add load."""
        scraper = RecordingScraper(cache_dir, failing=(10,))
        with pytest.raises(ConnectionResetError):
            scraper.download_chapters(12)
        assert scraper.cached_numbers() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12]

    def test_only_the_unbroken_run_before_the_failure_is_reported(self, cache_dir):
        """A line for chapter 12 with chapter 11 missing above it would read as if 11 had been done."""
        scraper = RecordingScraper(cache_dir, failing=(11,))
        with pytest.raises(ConnectionResetError):
            scraper.download_chapters(12)
        assert scraper.reported_numbers() == list(range(1, 11))
        assert 12 in scraper.cached_numbers()

    def test_nothing_is_fetched_after_the_batch_that_failed(self, cache_dir):
        scraper = RecordingScraper(cache_dir, failing=(5,))
        with pytest.raises(ConnectionResetError):
            scraper.download_chapters(12)
        assert scraper.fetched_numbers() == [1, 2, 3, 4, 5, 6]

    def test_the_fetch_error_is_the_one_raised_when_a_later_chapter_cannot_be_read_either(self, cache_dir):
        """Chapter 12 is only being kept for next time. That it cannot be parsed must not hide why the download stopped."""
        scraper = RecordingScraper(cache_dir, failing=(10,), unparseable=(12,))
        with pytest.raises(ConnectionResetError, match="chapter 10"):
            scraper.download_chapters(12)
        assert 11 in scraper.cached_numbers()
        assert 12 not in scraper.cached_numbers()

    def test_a_chapter_that_cannot_be_read_still_stops_the_download(self, cache_dir):
        scraper = RecordingScraper(cache_dir, unparseable=(5,))
        with pytest.raises(AttributeError):
            scraper.download_chapters(9)
        assert set(scraper.cached_numbers()) >= {1, 2, 3, 4}
        assert 7 not in scraper.fetched_numbers()

    def test_a_failure_with_one_at_a_time_keeps_the_ones_before_it(self, cache_dir):
        scraper = RecordingScraper(cache_dir, concurrency=1, failing=(3,))
        with pytest.raises(ConnectionResetError):
            scraper.download_chapters(5)
        assert scraper.cached_numbers() == [1, 2]
        assert scraper.fetched_numbers() == [1, 2, 3]
        assert scraper.reported_numbers() == [1, 2]


class TestCachedAndFetchedTogether:
    def test_cached_and_fetched_chapters_are_reported_in_story_order(self, cache_dir):
        earlier = RecordingScraper(cache_dir, failing=(4, 7))
        # Leaves chapters 1 to 3, 5 and 6 in the cache: the batch of 4, 5, 6 fails on 4.
        with pytest.raises(ConnectionResetError):
            earlier.download_chapters(9)
        assert earlier.cached_numbers() == [1, 2, 3, 5, 6]

        scraper = RecordingScraper(cache_dir)
        chapters = scraper.download_chapters(9)
        assert [chapter.number for chapter in chapters] == list(range(1, 10))
        assert scraper.reported_numbers() == list(range(1, 10))
        kinds = {event[1]: event[2] for event in scraper.events if event[0] == "report"}
        assert kinds == {1: "cached", 2: "cached", 3: "cached", 4: "fetched", 5: "cached", 6: "cached", 7: "fetched", 8: "fetched", 9: "fetched"}
        assert scraper.fetched_numbers() == [4, 7, 8, 9]
        assert [chapter.html for chapter in chapters] == [f"Body {number}" for number in range(1, 10)]

    def test_a_cached_chapter_is_reported_without_waiting_for_a_fetch(self, cache_dir):
        RecordingScraper(cache_dir).download_chapters(2)
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(6)
        assert scraper.position("report", 2) < scraper.position("fetch", 3)

    def test_an_update_that_skips_the_chapters_it_has_reports_the_new_ones_as_they_come(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        chapters = scraper.download_chapters(10, skip_chapters=4)
        assert [chapter.number for chapter in chapters] == [5, 6, 7, 8, 9, 10]
        assert scraper.fetched_numbers() == [5, 6, 7, 8, 9, 10]
        assert scraper.position("report", 7) < scraper.position("fetch", 8)

    def test_a_chapter_range_is_honoured(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        chapters = scraper.download_chapters(10, chapter_spec=[(2, 3), (8, 9)])
        assert [chapter.number for chapter in chapters] == [2, 3, 8, 9]
        assert scraper.fetched_numbers() == [2, 3, 8, 9]

    def test_with_everything_cached_nothing_is_fetched_or_paused_for(self, cache_dir):
        RecordingScraper(cache_dir).download_chapters(5)
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(5)
        assert scraper.fetched_numbers() == []
        assert [event for event in scraper.events if event[0] == "pause"] == []


class TestPacingCarriesAcrossBatches:
    def test_there_is_one_pause_between_batches_counted_in_chapters(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(8)
        assert [event for event in scraper.events if event[0] == "pause"] == [("pause", 3), ("pause", 3)]

    def test_the_pause_comes_after_the_batch_is_reported_and_before_the_next_fetch(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        scraper.download_chapters(6)
        assert scraper.position("report", 3) < scraper.position("pause") < scraper.position("fetch", 4)

    def test_a_rate_limit_halves_the_batches_for_the_rest_of_the_story(self, cache_dir):
        """The back-off lives in one pass over the whole story. Fetching each batch with a fresh call would forget it."""
        scraper = RecordingScraper(cache_dir, concurrency=4)
        scraper.rate_limited_on = 2
        scraper.download_chapters(12)
        pauses = [event[1] for event in scraper.events if event[0] == "pause"]
        # First batch of 4, then batches of 2 for the remaining 8; each pause is the size of the batch before it.
        assert pauses == [4, 2, 2, 2]
        assert scraper.reported_numbers() == list(range(1, 13))

    def test_one_at_a_time_pauses_before_every_chapter_but_the_first(self, cache_dir):
        scraper = RecordingScraper(cache_dir, concurrency=1)
        scraper.download_chapters(4)
        assert [event for event in scraper.events if event[0] == "pause"] == [("pause", 1)] * 3


class TestStoppingFromTheProgressCallback:
    """A caller can stop a download by raising from its progress callback, which is what a Cancel would do."""

    def test_raising_stops_the_fetching_and_keeps_what_arrived(self, cache_dir):
        scraper = RecordingScraper(cache_dir)

        class Stop(Exception):
            pass

        def report(number, total, title, cached):
            if number == 4:
                raise Stop()

        with pytest.raises(Stop):
            scraper.download_chapters(12, progress_callback=report)
        assert scraper.fetched_numbers() == [1, 2, 3, 4, 5, 6]
        assert scraper.cached_numbers() == [1, 2, 3, 4, 5, 6]

    def test_the_download_can_be_picked_up_afterwards(self, cache_dir):
        first = RecordingScraper(cache_dir)

        def report(number, total, title, cached):
            if number == 2:
                raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            first.download_chapters(9, progress_callback=report)
        second = RecordingScraper(cache_dir)
        chapters = second.download_chapters(9)
        assert [chapter.number for chapter in chapters] == list(range(1, 10))
        assert second.fetched_numbers() == [4, 5, 6, 7, 8, 9]


class TestFewerChaptersThanAskedFor:
    def test_it_is_an_error_and_not_a_shorter_book(self, cache_dir):
        """If the fetching ever hands over fewer chapters than the story has, saying so beats writing a book with the end missing."""
        scraper = RecordingScraper(cache_dir)
        real_batches = scraper._fetch_batches

        def one_batch_only(urls):
            for batch in real_batches(urls):
                yield batch
                return

        scraper._fetch_batches = one_batch_only
        with pytest.raises(RuntimeError, match="batchsite: fewer chapters came back"):
            scraper.download_chapters(7)


class TestFetchParallelKeepsItsContract:
    """``_fetch_parallel`` is the all-at-once form: a list in, a list out, and the error raised once the batch has settled."""

    def test_bodies_come_back_in_the_order_asked_for(self, cache_dir):
        scraper = RecordingScraper(cache_dir)
        urls = [descriptor(number)["url"] for number in range(1, 8)]
        assert scraper._fetch_parallel(urls) == [f"<div id='ct'>Body {number}</div>" for number in range(1, 8)]

    def test_a_failure_is_raised_after_its_batch_has_settled(self, cache_dir):
        scraper = RecordingScraper(cache_dir, failing=(4,))
        urls = [descriptor(number)["url"] for number in range(1, 10)]
        with pytest.raises(ConnectionResetError, match="chapter 4"):
            scraper._fetch_parallel(urls)
        assert scraper.fetched_numbers() == [1, 2, 3, 4, 5, 6]

    def test_no_urls_is_an_empty_list(self, cache_dir):
        assert RecordingScraper(cache_dir)._fetch_parallel([]) == []

    def test_a_batch_with_a_failure_is_the_last_one_handed_over(self, cache_dir):
        scraper = RecordingScraper(cache_dir, failing=(5,))
        urls = [descriptor(number)["url"] for number in range(1, 10)]
        batches = list(scraper._fetch_batches(urls))
        assert len(batches) == 2
        assert [error is None for _body, error in batches[0]] == [True, True, True]
        assert [error is None for _body, error in batches[1]] == [True, False, True]
        assert batches[1][0][0] == "<div id='ct'>Body 4</div>"
        assert batches[1][2][0] == "<div id='ct'>Body 6</div>"
