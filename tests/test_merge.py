"""Unit tests for ffn_dl.merge — combining a series into one Story.

Uses synthetic Story/Chapter objects so nothing hits the network. Guards
the two things the merge path must get right: chapter renumbering across
parts, and labelling the merged book by its actual source (the bug that
made a merged Literotica series claim every part was "Original on AO3").
"""

from ffn_dl.merge import merge_stories, source_display_name
from ffn_dl.models import Chapter, Story

LIT = "https://www.literotica.com/s/"
AO3 = "https://archiveofourown.org/works/"


def _story(title, author, url, chapters, *, summary="", words="", status=""):
    meta = {}
    if words:
        meta["words"] = words
    if status:
        meta["status"] = status
    return Story(
        id=0,
        title=title,
        author=author,
        summary=summary,
        url=url,
        chapters=[
            Chapter(number=i + 1, title=t, html=h)
            for i, (t, h) in enumerate(chapters)
        ],
        metadata=meta,
    )


def test_chapter_numbering_is_contiguous_across_parts():
    a = _story("Part 1", "Alice", LIT + "p1", [("C1", "<p>a1</p>"), ("C2", "<p>a2</p>")])
    b = _story("Part 2", "Alice", LIT + "p2", [("C1", "<p>b1</p>")])
    merged = merge_stories("My Series", LIT + "series", [a, b])
    # Two title chapters (one per part) + three content chapters = five
    # sections, numbered 1..5 with no gaps or repeats.
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4, 5]


def test_shared_author_kept_otherwise_joined():
    a = _story("P1", "Alice", LIT + "p1", [("C", "<p>x</p>")])
    b = _story("P2", "Alice", LIT + "p2", [("C", "<p>y</p>")])
    assert merge_stories("S", LIT + "s", [a, b]).author == "Alice"

    c = _story("P3", "Bob", LIT + "p3", [("C", "<p>z</p>")])
    assert merge_stories("S", LIT + "s", [a, c]).author == "Alice, Bob"


def test_word_count_summed_and_status_rolled_up():
    a = _story("P1", "A", LIT + "p1", [("C", "<p>x</p>")], words="1,000", status="Complete")
    b = _story("P2", "A", LIT + "p2", [("C", "<p>y</p>")], words="500", status="In-Progress")
    merged = merge_stories("S", LIT + "s", [a, b])
    assert merged.metadata["words"] == "1,500"
    # Any non-complete part → the whole merged work is In-Progress.
    assert merged.metadata["status"] == "In-Progress"

    both_done = merge_stories("S", LIT + "s", [
        _story("P1", "A", LIT + "p1", [("C", "<p>x</p>")], status="Complete"),
        _story("P2", "A", LIT + "p2", [("C", "<p>y</p>")], status="Complete"),
    ])
    assert both_done.metadata["status"] == "Complete"


def test_literotica_series_is_not_labelled_ao3():
    """The reported bug: a merged Literotica book must name Literotica,
    not AO3, in the category and every per-part header link."""
    parts = [_story("Part 1", "A", LIT + "making-of-a-male-slut-ch-01", [("C", "<p>x</p>")])]
    merged = merge_stories("Making of a Male Slut", LIT + "series", parts)
    assert merged.metadata["category"] == "Literotica series"
    header = merged.chapters[0].html
    assert "Original on Literotica" in header
    assert "AO3" not in header
    assert "AO3" not in merged.metadata["category"]


def test_ao3_series_still_labelled_ao3():
    """Regression guard: AO3 series keep their original wording."""
    parts = [_story("Part 1", "A", AO3 + "123", [("C", "<p>x</p>")])]
    merged = merge_stories("An AO3 Series", "https://archiveofourown.org/series/9", parts)
    assert merged.metadata["category"] == "AO3 series"
    assert "Original on AO3" in merged.chapters[0].html


def test_source_display_name_maps_known_and_unknown_hosts():
    assert source_display_name(LIT + "x") == "Literotica"
    assert source_display_name(AO3 + "1") == "AO3"
    assert source_display_name("https://www.royalroad.com/fiction/1") == "Royal Road"
    assert source_display_name("https://www.fanfiction.net/s/1") == "FanFiction.net"
    # Unknown host: title-cased second-level label rather than a crash or
    # a bare hostname leaking into the book.
    assert source_display_name("https://www.example.org/x") == "Example"
    assert source_display_name("") == "the original source"
