"""MCStories full-text index: build, resume, ranking, and how the
search adapter unions body matches with the synopsis matches.

MCStories publishes no search endpoint, so keyword search filters a
client-side index built from the site's A-Z title pages — a title, an
author, tag codes and a one-line synopsis per story. That's under 1% of
each story's prose, which is why a query like ``feet`` returned a couple
of dozen hits from a 17,000-story archive. These tests cover the body
index that closes that gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ficary.erotica.search as S
from ficary.erotica import mcstories_fulltext as FT

pytestmark = pytest.mark.skipif(
    not FT.fts5_supported(), reason="SQLite built without FTS5",
)


def _rows(*slugs: str) -> list[dict]:
    return [
        {
            "slug": slug, "title": slug, "author": "A",
            "codes": "mc", "summary": "a blurb", "updated": "2020-01-01",
        }
        for slug in slugs
    ]


def _archive(bodies: dict[str, str]):
    """A fake ``fetch`` serving one single-chapter story per slug."""
    def fetch(url: str) -> str:
        slug = url.rstrip("/").split("/")[-1].removesuffix(".html")
        if url.endswith(f"/{slug}/"):
            return (
                f'<html><body><h3 class="title">{slug}</h3>'
                f'<div class="chapter"><a href="{slug}1.html">Chapter 1</a>'
                " (100 words)</div></body></html>"
            )
        body = bodies.get(slug.removesuffix("1"), "")
        return f'<html><body><article id="mcstories">{body}</article></body></html>'

    return fetch


# ── query sanitising ─────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "feet*", '"feet', "feet AND (", "feet:shoes", "-feet", "feet)",
])
def test_fts5_syntax_in_a_typed_query_never_reaches_sqlite(query, tmp_path):
    # FTS5 treats ", *, (, : and - as syntax. Passing a raw query
    # through turns a stray character into a query error rather than a
    # search, so terms are extracted and re-quoted instead.
    expression = FT._fts_match_expression(query)
    assert '"feet"' in expression
    assert "*" not in expression and "(" not in expression


@pytest.mark.parametrize("query", ["", "   ", "!!!"])
def test_a_query_with_no_usable_terms_declines_to_answer(query, tmp_path):
    # None means "no full-text opinion" so the caller keeps its own
    # matching, rather than an empty set meaning "nothing matched".
    assert FT.ranked_slugs(query, tmp_path / "absent.sqlite3") is None


def test_multi_word_queries_and_their_terms():
    assert FT._fts_match_expression("college sorority") == (
        '"college" AND "sorority"'
    )


# ── build, resume, cancel ────────────────────────────────────────


def test_build_indexes_bodies_and_reports_them(tmp_path):
    db = tmp_path / "ft.sqlite3"
    assert FT.available(db) is False
    assert FT.stats(db).stories == 0

    report = FT.build(
        _rows("Alpha", "Beta"),
        fetch=_archive({"Alpha": "her bare feet", "Beta": "no match here"}),
        path=db,
    )
    assert report.indexed == 2
    assert report.failed == 0
    assert FT.available(db) is True
    assert FT.stats(db).stories == 2
    assert FT.ranked_slugs("feet", db) == ["Alpha"]


def test_build_resumes_instead_of_recrawling(tmp_path):
    db = tmp_path / "ft.sqlite3"
    FT.build(_rows("Alpha"), fetch=_archive({"Alpha": "feet"}), path=db)

    fetched: list[str] = []

    def counting_fetch(url):
        fetched.append(url)
        return _archive({"Beta": "feet"})(url)

    report = FT.build(
        _rows("Alpha", "Beta"), fetch=counting_fetch, path=db,
    )
    assert report.already_present == 1
    assert report.indexed == 1
    assert not any("Alpha" in url for url in fetched), (
        "an already-indexed story must not be re-fetched"
    )


def test_a_story_that_yields_no_prose_is_not_recorded(tmp_path):
    # Recording an empty body would make the resume logic skip that
    # story forever, so a fetch or parse miss counts as a failure.
    db = tmp_path / "ft.sqlite3"

    def broken_fetch(url):
        raise RuntimeError("network died")

    report = FT.build(_rows("Alpha"), fetch=broken_fetch, path=db)
    assert report.indexed == 0
    assert report.failed == 1
    assert FT.stats(db).stories == 0
    # And a later run retries it rather than treating it as done.
    report = FT.build(
        _rows("Alpha"), fetch=_archive({"Alpha": "feet"}), path=db,
    )
    assert report.indexed == 1


def test_limit_caps_a_build_so_it_can_be_sampled(tmp_path):
    db = tmp_path / "ft.sqlite3"
    report = FT.build(
        _rows("Alpha", "Beta", "Gamma"),
        fetch=_archive({s: "feet" for s in ("Alpha", "Beta", "Gamma")}),
        path=db, limit=2,
    )
    assert report.indexed == 2
    assert FT.stats(db).stories == 2


def test_cancel_stops_the_crawl_and_keeps_committed_progress(tmp_path):
    # More slugs than one chunk, so "did it stop early?" is actually
    # observable. Handing the whole list to ``ThreadPoolExecutor.map``
    # would crawl every story before the cancel was ever noticed —
    # cancelling would only stop the *recording*.
    db = tmp_path / "ft.sqlite3"
    slugs = [f"Story{i:03d}" for i in range(FT._COMMIT_EVERY * 2 + 20)]
    fetched: list[str] = []
    archive = _archive({s: "feet" for s in slugs})

    def counting_fetch(url):
        fetched.append(url)
        return archive(url)

    report = FT.build(
        _rows(*slugs), fetch=counting_fetch, path=db,
        cancel=lambda: True, workers=2,
    )
    assert report.cancelled is True
    assert report.indexed == FT._COMMIT_EVERY
    assert FT.stats(db).stories == FT._COMMIT_EVERY, "committed work survives"
    # Two fetches per story (index page + chapter) for one chunk only.
    assert len(fetched) == FT._COMMIT_EVERY * 2

    # And the remainder is still pending, so a re-run finishes the job.
    report = FT.build(_rows(*slugs), fetch=archive, path=db)
    assert report.already_present == FT._COMMIT_EVERY
    assert FT.stats(db).stories == len(slugs)


def test_build_without_fts5_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(FT, "fts5_supported", lambda: False)
    with pytest.raises(RuntimeError, match="FTS5"):
        FT.build(_rows("Alpha"), fetch=_archive({}), path=tmp_path / "x")


# ── ranking ──────────────────────────────────────────────────────


def test_ranking_puts_the_stories_a_word_is_about_first(tmp_path):
    # The precision trap this ordering exists for: "feet" is also a unit
    # of distance, so presence alone rates a passing mention as highly
    # as a story built around the word. bm25 would make it worse for a
    # one-word query — it normalises by length, so the short story wins.
    db = tmp_path / "ft.sqlite3"
    FT.build(
        _rows("Passing", "About"),
        fetch=_archive({
            "Passing": "he stood ten feet away " + ("filler " * 400),
            "About": "feet " * 40,
        }),
        path=db,
    )
    assert FT.ranked_slugs("feet", db) == ["About", "Passing"]


def test_ranking_is_stable_for_equal_mention_counts(tmp_path):
    # Ties break on slug so the Load More window doesn't reshuffle
    # between pages.
    db = tmp_path / "ft.sqlite3"
    FT.build(
        _rows("Beta", "Alpha"),
        fetch=_archive({"Alpha": "feet", "Beta": "feet"}),
        path=db,
    )
    assert FT.ranked_slugs("feet", db) == ["Alpha", "Beta"]


def test_a_missing_index_declines_rather_than_matching_nothing(tmp_path):
    assert FT.ranked_slugs("feet", tmp_path / "never-built.sqlite3") is None


# ── how the search adapter uses it ───────────────────────────────


def _index_at(monkeypatch, db: Path) -> None:
    monkeypatch.setattr(FT, "index_path", lambda: db)


def test_search_unions_body_matches_after_the_blurb_matches(
    monkeypatch, tmp_path,
):
    db = tmp_path / "ft.sqlite3"
    rows = [
        {"slug": "InBlurb", "title": "Sole Sisters", "author": "A",
         "codes": "mc", "summary": "about her feet", "updated": "2020-01-01"},
        {"slug": "InBodyOnly", "title": "Quiet Story", "author": "A",
         "codes": "mc", "summary": "nothing relevant", "updated": "2020-01-01"},
    ]
    monkeypatch.setattr(S, "_mcs_title_index_state", lambda: (rows, []))
    FT.build(
        [{"slug": "InBodyOnly"}],
        fetch=_archive({"InBodyOnly": "feet " * 20}), path=db,
    )
    _index_at(monkeypatch, db)

    titles = [r["title"] for r in S.search_mcstories("feet")]
    # The blurb match keeps the head — a story whose own synopsis uses
    # the word is the strongest signal the archive gives us — and the
    # body-only match extends the list rather than reordering it.
    assert titles == ["Sole Sisters", "Quiet Story"]


def test_search_without_an_index_matches_blurbs_and_says_so(
    monkeypatch, tmp_path,
):
    rows = [
        {"slug": "InBlurb", "title": "Sole Sisters", "author": "A",
         "codes": "mc", "summary": "about her feet", "updated": "2020-01-01"},
        {"slug": "InBodyOnly", "title": "Quiet Story", "author": "A",
         "codes": "mc", "summary": "nothing relevant", "updated": "2020-01-01"},
    ]
    monkeypatch.setattr(S, "_mcs_title_index_state", lambda: (rows, []))
    _index_at(monkeypatch, tmp_path / "never-built.sqlite3")

    page = S.search_mcstories("feet")
    assert [r["title"] for r in page] == ["Sole Sisters"]
    # Saying what was searched is what turns "this story isn't on the
    # site" into "my index only covers blurbs".
    assert page.partial_note == "blurbs only, full text not indexed"


def test_search_reports_a_part_built_index(monkeypatch, tmp_path):
    db = tmp_path / "ft.sqlite3"
    rows = _rows("Alpha", "Beta", "Gamma")
    monkeypatch.setattr(S, "_mcs_title_index_state", lambda: (rows, []))
    FT.build([{"slug": "Alpha"}], fetch=_archive({"Alpha": "feet"}), path=db)
    _index_at(monkeypatch, db)

    page = S.search_mcstories("feet")
    assert page.partial_note == "full text 1/3"


def test_a_body_match_still_has_to_carry_the_requested_tag(
    monkeypatch, tmp_path,
):
    db = tmp_path / "ft.sqlite3"
    rows = [
        {"slug": "WrongCode", "title": "Quiet Story", "author": "A",
         "codes": "mc", "summary": "nothing", "updated": "2020-01-01"},
    ]
    monkeypatch.setattr(S, "_mcs_title_index_state", lambda: (rows, []))
    FT.build(
        [{"slug": "WrongCode"}],
        fetch=_archive({"WrongCode": "feet " * 20}), path=db,
    )
    _index_at(monkeypatch, db)

    # ``fd`` (female dominant) isn't on the story, so the body match must
    # not smuggle it past the tag filter.
    assert S.search_mcstories("feet", tags=["femdom"]) == []


def test_full_text_hits_are_windowed_without_repeats(monkeypatch, tmp_path):
    db = tmp_path / "ft.sqlite3"
    slugs = [f"Story{i:02d}" for i in range(6)]
    rows = _rows(*slugs)
    monkeypatch.setattr(S, "_mcs_title_index_state", lambda: (rows, []))
    FT.build(
        [{"slug": s} for s in slugs],
        fetch=_archive({s: "feet" for s in slugs}), path=db,
    )
    _index_at(monkeypatch, db)
    monkeypatch.setattr(S, "PER_SITE_PAGE_MAX", 2)

    seen: list[str] = []
    for page_number in (1, 2, 3, 4):
        seen += [r["url"] for r in S.search_mcstories("feet", page=page_number)]
    assert len(seen) == len(set(seen)) == 6
    assert S.search_mcstories("feet", page=5) == []
