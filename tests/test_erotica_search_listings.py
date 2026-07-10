"""Listing-metadata tests: search rows and author-picker rows carry the
summary / word count / date each site actually publishes.

Fixtures are live captures (2026-07-10) of one listing page per site,
served through a patched ``_fetch``/``_post`` so every parser runs
offline exactly as it does in production. Row-count assertions are
floors, not exact counts, so a re-captured fixture with more rows
doesn't break the suite; field assertions pin the shape (ISO dates,
digit-grouped words) rather than specific stories where possible.
"""

import re
from pathlib import Path

import pytest

import ficary.erotica.search as S
from ficary.erotica.aff import AFFScraper
from ficary.erotica.fictionmania import FictionmaniaScraper
from ficary.erotica.literotica import LiteroticaScraper
from ficary.erotica.mcstories import MCStoriesScraper
from ficary.erotica.storiesonline import StoriesOnlineScraper
from ficary.ficwad import FicWadScraper
from ficary.mediaminer import MediaMinerScraper
from ficary.royalroad import RoyalRoadScraper

FIXTURES = Path(__file__).parent / "fixtures"
EROTICA = FIXTURES / "erotica"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _patch_fetch(monkeypatch, fixture: Path):
    monkeypatch.setattr(S, "_fetch", lambda url: _read(fixture))


# ── Search rows ─────────────────────────────────────────────────────

def test_aff_rows_carry_summary_chapters_updated(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "aff_index.html")
    rows = S.search_aff("", fandom="hp")
    assert len(rows) >= 15
    assert all(r["summary"] for r in rows)
    assert all(r["chapters"].isdigit() for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)
    assert all(r["author"] for r in rows)


def test_sol_rows_carry_summary_and_exact_words(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "sol_new.html")
    rows = S.search_sol("")
    assert len(rows) >= 8
    assert all(r["summary"] for r in rows)
    # SOL publishes exact counts ("6,022") in the row's misc line.
    assert all(re.fullmatch(r"[\d,]+", r["words"]) for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


def test_sol_bytag_strips_series_banners(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "sol_bytag_fiction.html")
    # Any vocab tag works — the fetch is stubbed to the bytag fixture.
    rows = S.search_sol("", tags=["feet"])
    assert len(rows) >= 8
    assert all(r["summary"] for r in rows)
    # span.help series/universe banners must not prefix the synopsis —
    # "The Flog Prince" carries an "A Filthy Tales for Wicked
    # Grown-Ups Story" banner in the fixture.
    flog = next(r for r in rows if r["title"] == "The Flog Prince")
    assert flog["summary"].startswith("When a cursed prince")
    assert not any(r["summary"].startswith("Part of the ") for r in rows)


def test_lush_rows_use_article_cards(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "lush_stories.html")
    rows = S.search_lushstories("")
    assert len(rows) >= 10
    assert all(r["summary"] for r in rows)
    # Real <h2> titles, not slug-derived Title Case.
    assert any("'" in r["title"] or "-" in r["title"] for r in rows)
    # Site-rounded k-format counts.
    assert all(re.fullmatch(r"[\d.,]+k?", r["words"]) for r in rows)
    assert all(r["author"] for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


def test_sexstories_browse_rows(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "sexstories_home.html")
    rows = S.search_sexstories("")
    assert len(rows) >= 50
    # Summaries are per-story optional on this site, but the browse
    # surface carries them on most rows.
    with_summary = sum(1 for r in rows if r["summary"])
    assert with_summary >= len(rows) // 2
    # Ratings are site-native percentages.
    assert any(r["rating"].endswith("%") for r in rows)


def test_sexstories_search_post_rows(monkeypatch):
    monkeypatch.setattr(
        S, "_post",
        lambda url, data=None: _read(EROTICA / "sexstories_search.html"),
    )
    rows = S.search_sexstories("feet")
    assert len(rows) >= 30
    # Server-side results: rows are kept even when the query string
    # isn't in the title; the sparse summaries that do exist survive
    # guillemet-stripping.
    summaries = [r["summary"] for r in rows if r["summary"]]
    assert summaries and not any(s.startswith("\xab") for s in summaries)


def test_tgstorytime_rows(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "tgst_new.html")
    rows = S.search_tgstorytime("")
    assert len(rows) >= 40
    assert all(r["summary"] for r in rows)
    assert all(r["author"] for r in rows)
    assert all(r["status"] in ("Complete", "In progress") for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)
    # The sidebar "Random Story" block must not leak in.
    assert all(r["title"] != "Random Story" for r in rows)


def test_chyoa_trending_rows(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "chyoa_trending.html")
    rows = S.search_chyoa("")
    assert len(rows) >= 40
    assert all(r["summary"] for r in rows)
    # Total chapter counts come from the trending meta row.
    assert sum(1 for r in rows if r["chapters"].replace(",", "").isdigit()) >= 40
    # Site chrome (footer/sidebar links) must not leak into a bare browse.
    titles = {r["title"].lower() for r in rows}
    assert not titles & {"supporters", "dmca", "contact us", "chyoa guide"}


def test_chyoa_search_rows_keep_server_hits(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "chyoa_search.html")
    rows = S.search_chyoa("feet")
    # 20 cards on the fixture — including chapter-level hits and rows
    # whose title/summary lack the literal query (server-side
    # relevance must not be re-filtered client-side).
    assert len(rows) >= 18
    assert all(r["summary"] for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


@pytest.mark.parametrize("fn,fixture", [
    (S.search_darkwanderer, "dw_forum.html"),
    (S.search_chastitymansion, "cm_forum.html"),
    (S.search_ticklingforum, "tf_forum.html"),
])
def test_xenforo_rows_carry_real_titles_authors_dates(monkeypatch, fn, fixture):
    _patch_fetch(monkeypatch, EROTICA / fixture)
    rows = fn("")
    assert len(rows) >= 10
    # Real titles (mixed case / punctuation), not slug .title() —
    # slug-derived titles never contain apostrophes or parentheses.
    assert any(re.search(r"[':(&]", r["title"]) for r in rows)
    assert sum(1 for r in rows if r["author"]) >= len(rows) - 2
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


def test_literotica_bare_browse_uses_card_parser(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "literotica_new.html")
    rows = S.search_literotica_wrapped("")
    assert len(rows) >= 15
    assert all(r["summary"] for r in rows)
    assert all(r["author"] for r in rows)
    assert all(r["site"] == "literotica" for r in rows)


def test_readonlymind_rows_survive_all_count_shapes(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "rom_search.html")
    rows = S.search_readonlymind("feet")
    # Server-side relevance hits are NOT re-filtered by the literal
    # query — the old double filter emptied every keyword search.
    assert len(rows) == 10
    assert all(r["summary"] for r in rows)
    # Every count shape parses: "(2797 words)", "(6 chapters, 9232
    # words)", "[Ongoing] (...)".
    assert all(r["words"].isdigit() for r in rows)
    assert any(r["chapters"].isdigit() and int(r["chapters"]) > 1 for r in rows)
    assert any(r["status"] == "In progress" for r in rows)


def test_giantessworld_rows_full_metadata(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "gw_browse.html")
    rows = S.search_giantessworld("")
    assert len(rows) >= 15
    assert all(r["summary"] for r in rows)
    assert all(re.fullmatch(r"[\d,]+", r["words"]) for r in rows)
    assert all(r["chapters"].isdigit() for r in rows)
    assert all(r["status"] in ("Complete", "In progress") for r in rows)
    assert all(r["rating"] in ("G", "PG", "R", "X") for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


def test_mcstories_whatsnew_rows(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "mcstories_whatsnew.html")
    rows = S.search_mcstories("")
    assert len(rows) >= 60
    assert all(r["summary"] for r in rows)
    assert all(r["author"] for r in rows)
    assert all(ISO_DATE.match(r["updated"]) for r in rows)
    # Nav links (Titles/Authors/Tags/ReadersPicks) must not leak in as
    # rows, and cross-section repeats must dedupe.
    urls = [r["url"] for r in rows]
    assert len(urls) == len(set(urls))
    assert not any(
        u.rstrip("/").endswith(("Titles", "Authors", "Tags", "ReadersPicks"))
        for u in urls
    )


def test_mcstories_tag_rows_keep_codes_as_summary(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "mcstories_tag.html")
    rows = S.search_mcstories("", tags=["mind-control"])
    assert len(rows) >= 50
    # Tag pages expose nothing beyond the code string — it stands in
    # for the summary.
    assert all(r["summary"] for r in rows)


def test_bdsmlibrary_dead_listing_raises(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "bdsmlib_list.html")
    with pytest.raises(S.SearchFetchError):
        S.search_bdsmlibrary("")


def test_greatfeet_rows_carry_update_date(monkeypatch):
    _patch_fetch(monkeypatch, EROTICA / "greatfeet_list.html")
    rows = S.search_greatfeet("")
    assert len(rows) >= 100
    assert all(ISO_DATE.match(r["updated"]) for r in rows)


def test_min_words_filter_parses_k_format():
    rows = [
        {"words": "2.6k"}, {"words": "612"}, {"words": "?"},
        {"words": "12,624"},
    ]
    kept = S._filter_by_min_words(rows, "1k")
    # 612 drops; unknown "?" passes through; k-format and grouped
    # digits both parse.
    assert kept == [{"words": "2.6k"}, {"words": "?"}, {"words": "12,624"}]


# ── Author-picker rows ──────────────────────────────────────────────

def _stub(cls, fetch):
    s = cls.__new__(cls)
    s._fetch = fetch
    return s


def test_aff_author_rows_carry_summary(monkeypatch):
    def fetch(url):
        if "load-user-stories" in url:
            sub = url.split("subdomain=")[1].split("&")[0]
            path = EROTICA / f"aff_userstories_{sub}.html"
            return _read(path) if path.exists() else "<div></div>"
        return _read(EROTICA / "aff_profile.html")
    s = _stub(AFFScraper, fetch)
    _, works = s.scrape_author_works(
        "https://members.adult-fanfiction.org/profile.php?id=1296890884",
    )
    assert len(works) == 7
    assert sum(1 for w in works if w["summary"]) >= 5
    assert all(w["chapters"].isdigit() for w in works)
    assert all(not w["updated"] or ISO_DATE.match(w["updated"]) for w in works)


def test_sol_author_rows_carry_summary_and_words():
    s = _stub(StoriesOnlineScraper, lambda url: _read(EROTICA / "sol_author.html"))
    s._delay = lambda: None
    _, works = s.scrape_author_works(
        "https://storiesonline.net/a/fan-fiction-man", max_results=10,
    )
    assert len(works) == 10
    assert all(w["summary"] for w in works)
    assert all(re.fullmatch(r"[\d,]+", w["words"]) for w in works)


def test_literotica_author_rows_carry_summary():
    s = _stub(
        LiteroticaScraper, lambda url: _read(EROTICA / "lit_author_works.html"),
    )
    author, works = s.scrape_author_works(
        "https://www.literotica.com/authors/Duleigh/works/stories",
    )
    assert author == "Duleigh"
    assert len(works) >= 10
    assert all(w["summary"] for w in works)
    assert all(w["fandom"] for w in works)
    assert all(ISO_DATE.match(w["updated"]) for w in works)


def test_mediaminer_author_rows_carry_summary_and_words():
    s = _stub(MediaMinerScraper, lambda url: _read(FIXTURES / "mm_author.html"))
    _, works = s.scrape_author_works(
        "https://www.mediaminer.org/fanfic/src.php/u/Majicman55",
    )
    assert len(works) >= 20
    # Only the <article>-wrapped rows carry the stat line (15 on the
    # fixture); the page's flat complete-list links stay metadata-less.
    assert sum(1 for w in works if w["summary"]) >= 12
    assert sum(1 for w in works if re.fullmatch(r"[\d.,]+[KM]?", w["words"])) >= 12
    assert sum(1 for w in works if w["chapters"].isdigit()) >= 12


def test_ficwad_author_rows_carry_summary_and_words():
    s = _stub(FicWadScraper, lambda url: _read(FIXTURES / "ficwad_author.html"))
    _, works = s.scrape_author_works("https://ficwad.com/a/Vanir")
    assert works
    w = works[0]
    assert w["summary"] and re.fullmatch(r"[\d,]+", w["words"])
    assert w["status"] == "Complete"
    assert ISO_DATE.match(w["updated"])


def test_royalroad_author_uses_fictions_tab():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return _read(FIXTURES / "rr_profile_fictions.html")

    s = _stub(RoyalRoadScraper, fetch)
    author, works = s.scrape_author_works("https://www.royalroad.com/profile/119608")
    assert fetched == ["https://www.royalroad.com/profile/119608/fictions"]
    assert author == "Alexander Wales"
    assert len(works) == 3
    assert all(w["summary"] for w in works)
    # Pages-derived estimates, "~"-prefixed like the search parser.
    assert all(re.fullmatch(r"~[\d,]+", w["words"]) for w in works)


# ── Story-fetch metadata ────────────────────────────────────────────

def test_royalroad_fetch_captures_site_word_count():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(
        _read(FIXTURES / "royalroad_fiction.html"), "lxml",
    )
    meta = RoyalRoadScraper._parse_metadata(soup)
    assert meta["extra"]["words"] == "751,549"


def test_mcstories_fetch_sums_chapter_word_counts():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(
        _read(EROTICA / "mcstories_index.html"), "lxml",
    )
    meta = MCStoriesScraper._parse_metadata(soup, "AToZeb")
    assert meta["extra"]["words"] == "2,491"


def test_fictionmania_details_page_parses():
    details = FictionmaniaScraper._parse_details(
        _read(EROTICA / "fm_details.html"),
    )
    assert details["title"] == "A Perfect Housewife"
    assert details["author"] == "Pollymeric"
    assert details["synopsis"].startswith("24 year old Hobson Bucknall")
