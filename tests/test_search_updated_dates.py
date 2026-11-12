"""Check that search results carry each story's last-updated date, read from pages and API replies saved from the real sites."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from ficary import cli, search
from ficary.erotica import search as erotica_search


FIXTURES = Path(__file__).parent / "fixtures"
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def rows_by_title(rows):
    return {row["title"]: row for row in rows}


def rows_by_url(rows):
    return {row["url"]: row for row in rows}


def assert_every_row_is_dated(rows):
    undated = [row["title"] for row in rows if not ISO_DATE.fullmatch(row.get("updated") or "")]
    assert not undated, f"rows with no last-updated date: {undated}"


# ── FanFiction.Net ────────────────────────────────────────────────


def test_ffn_rows_carry_the_update_date_or_else_the_publish_date():
    all_rows = search._parse_results(read_fixture("ffn_search.html"))
    rows = rows_by_title(all_rows)
    # Never touched after it went up, so its only date is the publish date: 1619878274 is 2021-05-01 14:11 UTC.
    assert rows["harry potter and the attack of the harry potter"]["updated"] == "2021-05-01"
    # Lists both dates. Updated (2009-11-14) is the one to show, not Published (2009-10-09).
    assert rows["Harry Potter: The Potters' Notes"]["updated"] == "2009-11-14"
    assert rows["Harry Potter the Blonde!"]["updated"] == "2016-08-11"
    assert len(all_rows) == 50
    assert_every_row_is_dated(all_rows)


def test_ffn_row_with_a_broken_timestamp_is_left_undated():
    # FFN has served an empty timestamp during outages. That row loses its date; the page still parses.
    saved_page = read_fixture("ffn_search.html")
    page = saved_page.replace("data-xutime='1619878274'", "data-xutime=''")
    assert page != saved_page, "the timestamp to blank out was not found in the saved page"
    all_rows = search._parse_results(page)
    rows = rows_by_title(all_rows)
    assert rows["harry potter and the attack of the harry potter"]["updated"] == ""
    assert rows["Harry Potter: The Potters' Notes"]["updated"] == "2009-11-14"
    assert len(all_rows) == 50


def test_command_line_listing_prints_the_ffn_date(capsys):
    first_row = search._parse_results(read_fixture("ffn_search.html"))[:1]
    cli._print_search_results(first_row)
    printed = capsys.readouterr().out
    assert "updated 2021-05-01" in printed


# ── Archive of Our Own ────────────────────────────────────────────


def test_ao3_rows_carry_the_date_shown_on_each_work():
    all_rows = search._parse_ao3_results(read_fixture("ao3_search.html"))
    rows = rows_by_title(all_rows)
    # The page prints these as "12 Nov 2018", "08 Apr 2022" and "17 Feb 2020".
    assert rows["All the Young Dudes"]["updated"] == "2018-11-12"
    assert rows["Draco Malfoy and the Mortifying Ordeal of Being in Love"]["updated"] == "2022-04-08"
    assert rows["Evitative"]["updated"] == "2020-02-17"
    assert len(all_rows) == 20
    assert_every_row_is_dated(all_rows)


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("01 Jan 2020", "2020-01-01"),
        ("9 Mar 2021", "2021-03-09"),
        ("31 May 2019", "2019-05-31"),
        ("15 Oct 2023", "2023-10-15"),
        ("25 Dec 2024", "2024-12-25"),
        ("", ""),
        ("31 Feb 2020", ""),
        ("12 Foo 2020", ""),
        ("yesterday", ""),
    ],
)
def test_ao3_date_text_becomes_a_plain_date(printed, expected):
    assert search._iso_date_from_day_month_year(printed) == expected


# ── Royal Road ────────────────────────────────────────────────────


def test_royalroad_search_rows_carry_the_card_date():
    all_rows = search._parse_rr_results(read_fixture("royalroad_search.html"))
    rows = rows_by_title(all_rows)
    # Checked against the story's own page when this was saved: its last chapter went up 2015-08-26.
    assert rows["Dragon?"]["updated"] == "2015-08-26"
    assert len(all_rows) == 20
    assert_every_row_is_dated(all_rows)


def test_royalroad_latest_updates_rows_take_the_newest_chapter_time():
    all_rows = search._parse_rr_results(read_fixture("royalroad_latest_updates.html"))
    rows = rows_by_title(all_rows)
    # This card lists three chapters, oldest first: 2026-11-11, then two on 2026-11-12. The newest is the answer.
    assert rows["Bloodline Echo Through Ages"]["updated"] == "2026-11-12"
    assert len(all_rows) == 20
    assert_every_row_is_dated(all_rows)


# ── Wattpad ───────────────────────────────────────────────────────


class FakeWattpadSession:
    """Stands in for the Wattpad API. Like the real one, it sends back only the story fields the request asked for."""

    def __init__(self, **options):
        pass

    def get(self, url, timeout=30):
        query = parse_qs(urlparse(url).query)
        requested_fields = query["fields"][0]
        saved_reply = json.loads(read_fixture("wattpad_search.json"))
        stories = []
        for saved_story in saved_reply["stories"]:
            story = dict(saved_story)
            if "modifyDate" not in requested_fields:
                story.pop("modifyDate")
            if "lastPublishedPart" not in requested_fields:
                story.pop("lastPublishedPart")
            stories.append(story)
        return SimpleNamespace(status_code=200, text=json.dumps({"stories": stories}))


def test_wattpad_rows_carry_the_date_the_last_part_was_published(monkeypatch):
    from curl_cffi import requests as curl_requests
    monkeypatch.setattr(curl_requests, "Session", FakeWattpadSession)
    all_rows = list(search.search_wattpad("dragon"))
    rows = rows_by_title(all_rows)
    # Its last part went up 2019-04-28. The story record was edited again on 2026-08-28, which is not new reading.
    assert rows["The Dragon Father"]["updated"] == "2019-04-28"
    assert rows["The Dragon and the Princess"]["updated"] == "2020-07-09"
    assert len(all_rows) == 20
    assert_every_row_is_dated(all_rows)


def test_wattpad_row_without_a_last_part_falls_back_to_the_edit_date(monkeypatch):
    # Made-up case: none of the 20 saved stories lacks the last-part date, so drop it from every one by hand.
    class SessionWithoutLastPart(FakeWattpadSession):
        def get(self, url, timeout=30):
            reply = json.loads(super().get(url, timeout=timeout).text)
            for story in reply["stories"]:
                story.pop("lastPublishedPart", None)
            return SimpleNamespace(status_code=200, text=json.dumps(reply))

    from curl_cffi import requests as curl_requests
    monkeypatch.setattr(curl_requests, "Session", SessionWithoutLastPart)
    rows = rows_by_title(search.search_wattpad("dragon"))
    assert rows["The Dragon Father"]["updated"] == "2026-08-28"


# ── Literotica ────────────────────────────────────────────────────


def test_literotica_story_cards_carry_their_date():
    all_rows = search._parse_literotica_results(read_fixture("erotica/literotica_new.html"))
    rows = rows_by_title(all_rows)
    assert rows["February Sucks, Version 6,793"]["updated"] == "2026-07-10"
    assert len(all_rows) == 20
    assert_every_row_is_dated(all_rows)


def test_literotica_keyword_search_rows_carry_their_date():
    all_rows = search._parse_literotica_search_results(read_fixture("erotica/literotica_search.html"))
    rows = rows_by_url(all_rows)
    # The page prints these month first: 08/26/2017, 08/28/2020 and 07/22/2002.
    assert rows["https://www.literotica.com/s/kitty-and-the-dragon"]["updated"] == "2017-08-26"
    assert rows["https://www.literotica.com/s/a-dragons-tale-ch-14"]["updated"] == "2020-08-28"
    assert rows["https://www.literotica.com/s/dragon-master"]["updated"] == "2002-07-22"
    assert len(all_rows) == 50
    assert_every_row_is_dated(all_rows)


# ── BDSM Library ──────────────────────────────────────────────────


def test_bdsmlibrary_rows_carry_the_added_date(monkeypatch):
    monkeypatch.setattr(erotica_search, "_fetch", lambda url: read_fixture("erotica/bdsmlibrary_list.html"))
    all_rows = erotica_search.search_bdsmlibrary("")
    assert len(all_rows) == 10
    # The listing says "(Added on Sep 12, 2017)" under the first story and "(Added on Aug 15, 2017)" under the last.
    assert all_rows[0]["url"].endswith("storyid=10994")
    assert all_rows[0]["updated"] == "2017-09-12"
    assert all_rows[-1]["updated"] == "2017-08-15"
    assert_every_row_is_dated(all_rows)


# ── Series rows ───────────────────────────────────────────────────


def ao3_part(title, part_number, updated):
    return {
        "title": title, "author": "A", "url": f"https://archiveofourown.org/works/{part_number}",
        "summary": "", "words": "1000", "chapters": "1", "rating": "T", "fandom": "", "status": "Complete",
        "updated": updated,
        "series": [{"id": "99", "name": "Saga", "url": "https://archiveofourown.org/series/99", "part": part_number}],
    }


def test_ao3_series_row_shows_its_newest_part():
    parts = [
        ao3_part("Part One", 1, "2021-03-09"),
        ao3_part("Part Two", 2, "2020-01-05"),
        ao3_part("Part Three", 3, ""),
    ]
    collapsed = search.collapse_ao3_series(parts)
    assert len(collapsed) == 1
    assert collapsed[0]["is_series"] is True
    assert collapsed[0]["updated"] == "2021-03-09"


def test_series_row_with_no_dated_parts_stays_blank():
    parts = [ao3_part("Part One", 1, ""), ao3_part("Part Two", 2, "")]
    collapsed = search.collapse_ao3_series(parts)
    assert collapsed[0]["is_series"] is True
    assert collapsed[0]["updated"] == ""


def test_literotica_series_row_shows_its_newest_part():
    parts = [
        {
            "title": "Sample Story Ch. 06", "author": "Author1",
            "url": "https://www.literotica.com/s/sample-story-ch-06",
            "rating": "4.7", "fandom": "Fetish", "summary": "", "updated": "2026-02-03",
        },
        {
            "title": "Sample Story Ch. 07", "author": "Author1",
            "url": "https://www.literotica.com/s/sample-story-ch-07",
            "rating": "4.6", "fandom": "Fetish", "summary": "", "updated": "2026-02-17",
        },
    ]
    collapsed = search.collapse_literotica_series(parts)
    assert len(collapsed) == 1
    assert collapsed[0]["is_series"] is True
    assert collapsed[0]["updated"] == "2026-02-17"


def lush_part(slug, updated):
    return {
        "title": "Foo", "url": f"https://www.lushstories.com/stories/erotic/{slug}",
        "site": "lushstories", "updated": updated,
    }


def test_lush_series_row_shows_its_newest_part():
    parts = [lush_part("foo", "2026-01-10"), lush_part("foo-2", "2026-05-01"), lush_part("foo-3", "2026-03-20")]
    collapsed = search.collapse_lushstories_series(parts)
    assert len(collapsed) == 1
    assert collapsed[0]["is_series"] is True
    assert collapsed[0]["updated"] == "2026-05-01"


def test_newest_first_places_a_series_by_its_newest_part():
    standalone = {
        "title": "Standalone", "url": "https://www.lushstories.com/stories/erotic/standalone",
        "site": "lushstories", "updated": "2026-04-01",
    }
    parts = [lush_part("foo", "2026-01-10"), lush_part("foo-2", "2026-05-01"), lush_part("foo-3", "2026-03-20")]
    collapsed = search.collapse_lushstories_series([standalone] + parts)
    ordered = erotica_search.sort_rows_by_updated(collapsed)
    assert [row["title"] for row in ordered] == ["Foo", "Standalone"]
