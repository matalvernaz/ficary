"""Tests for the lines the command line prints under each search result.

Every field used to be printed whether or not the site's listing had a value for it. Royal Road's listing has no author and no rating, so every one of its results read "by  | Fantasy | ~6,875 words | 4 ch | Rated ?", which a screen reader speaks as "by, vertical bar ... rated question mark".
"""

from __future__ import annotations

from ficary import cli


def printed(capsys, row: dict) -> list[str]:
    cli._print_search_results([row])
    return capsys.readouterr().out.splitlines()


ROYAL_ROAD_ROW = {
    "title": "The Tax of Farnsworth Bay",
    "author": "",
    "fandom": "Male Lead, Adventure, Historical",
    "words": "~6,050",
    "chapters": "5",
    "rating": "?",
    "status": "Complete",
    "updated": "2026-06-11",
    "summary": "A new lighthouse keeper.",
}

FFN_ROW = {
    "title": "A Story",
    "author": "Somebody",
    "fandom": "Harry Potter",
    "words": "12,000",
    "chapters": "1",
    "rating": "T",
    "status": "In-Progress",
    "updated": "",
    "summary": "",
}


def test_a_field_the_listing_has_no_value_for_is_left_out(capsys):
    lines = printed(capsys, ROYAL_ROAD_ROW)
    details = lines[1]
    assert details.strip() == (
        "Male Lead, Adventure, Historical | ~6,050 words | 5 chapters | "
        "Complete | updated 2026-06-11"
    )
    assert "by" not in details.split()
    assert "Rated" not in details
    assert "?" not in details


def test_a_full_row_still_says_everything(capsys):
    lines = printed(capsys, FFN_ROW)
    assert lines[1].strip() == "by Somebody | Harry Potter | 12,000 words | 1 chapter | Rated T"


def test_chapters_are_spelled_out(capsys):
    """"4 ch" is read letter by letter."""
    details = printed(capsys, ROYAL_ROAD_ROW)[1]
    assert " ch |" not in details
    assert "5 chapters" in details


def test_a_series_row_names_its_author_and_leaves_out_an_unknown_one(capsys):
    with_author = printed(capsys, {
        "title": "A Series", "is_series": True, "series_parts": [1, 2],
        "author": "Somebody", "fandom": "Harry Potter",
    })
    assert with_author[1].strip() == "by Somebody | Harry Potter"
    without = printed(capsys, {
        "title": "A Series", "is_series": True, "series_parts": [1, 2],
        "author": "", "fandom": "",
    })
    # Nothing known about it: no "by |" line and no line of bare spaces either, just the title and the blank line that ends every result.
    assert without == ["   1. A Series  [Series · 2 part(s) seen]", ""]


def test_a_row_missing_a_field_entirely_does_not_crash(capsys):
    lines = printed(capsys, {"title": "Bare"})
    assert lines[0].strip() == "1. Bare"


def test_the_summary_still_follows(capsys):
    lines = printed(capsys, ROYAL_ROAD_ROW)
    assert lines[2].strip() == "A new lighthouse keeper."
