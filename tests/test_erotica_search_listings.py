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


def test_mcstories_tag_rows_serve_from_title_index(monkeypatch):
    # Tag searches filter the whole-archive title index by code — the
    # site's /Tags/<code>.html pages list bare title+code rows, while
    # the index carries author, synopsis, and added-date for the same
    # stories.
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(monkeypatch, EROTICA / "mcstories_titles.html", broken={})

    rows = S.search_mcstories("", tags=["femdom"])
    assert [r["title"] for r in rows] == ["Zeb’s Awakening"]  # the fd row
    assert rows[0]["author"] == "Cy"
    assert rows[0]["summary"] == "Zeb learns to surrender control."
    assert rows[0]["fandom"] == "mc fd"
    assert ISO_DATE.match(rows[0]["updated"])

    # A tag and a keyword narrow each other (the old tag-page path
    # could only re-filter titles, so this intersection returned
    # nearly nothing).
    assert [r["title"] for r in S.search_mcstories("robot", tags=["mind-control"])] == [
        "Robot Maid",
    ]
    # Unsupported tag with no query still returns [] rather than the
    # whole index.
    assert S.search_mcstories("", tags=["polyamory"]) == []


def test_mcstories_keyword_scans_title_index(monkeypatch):
    # Free-text search walks the cached A-Z title index, not WhatsNew.
    # Reset the module-level cache so the build runs against the fixture
    # (the stubbed _fetch returns it for every letter page; dedupe by
    # slug collapses the 26 identical fetches to the 3 unique stories).
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(monkeypatch, EROTICA / "mcstories_titles.html", broken={})

    rows = S.search_mcstories("mc")  # every fixture story carries code "mc"
    assert len(rows) == 3
    assert all(r["author"] for r in rows)
    assert all(r["summary"] for r in rows)
    assert all(r["fandom"] for r in rows)  # codes land in the fandom column
    assert all(ISO_DATE.match(r["updated"]) for r in rows)
    assert all(r["url"].startswith("https://mcstories.com/") for r in rows)

    # AND-of-terms: a multi-word query whose words are split across the
    # title and synopsis still matches. The shared contiguous-substring
    # matcher returned nothing for this shape.
    assert [r["title"] for r in S.search_mcstories("college sorority")] == [
        "The College Dean",
    ]
    # A single distinctive term still narrows correctly.
    assert [r["title"] for r in S.search_mcstories("robot")] == ["Robot Maid"]


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


# ── MCStories title-index completeness ───────────────────────────
#
# The index is built from 26 A-Z letter pages. A page that doesn't come
# back used to be skipped, and the partial harvest was then cached with
# the full 6-hour TTL — so one missing letter silently removed on the
# order of a thousand stories from every keyword and tag search while
# the search still reported a clean count.


def _patch_index_fetch(monkeypatch, fetch):
    """Point the title-index crawl at ``fetch``.

    The letter pages go through ``_MCS_INDEX_FETCHER`` rather than the
    shared search fetcher — it fails after one attempt instead of
    sleeping through a 30s/60s retry backoff — so patching ``S._fetch``
    doesn't reach them.
    """
    monkeypatch.setattr(S._MCS_INDEX_FETCHER, "_fetch", fetch)


def _letter_fetch(monkeypatch, fixture: Path, *, broken: dict):
    """Serve ``fixture`` for every letter page except the ones in
    ``broken``, which map a letter to either an exception to raise or a
    replacement body to return."""
    def fake_fetch(url):
        for letter, outcome in broken.items():
            if url == S._MCS_TITLE_PAGE_URL.format(letter=letter):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return _read(fixture)

    _patch_index_fetch(monkeypatch, fake_fetch)


def test_mcstories_index_reports_a_letter_it_could_not_fetch(monkeypatch):
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(
        monkeypatch, EROTICA / "mcstories_titles.html",
        broken={"T": S.SearchFetchError("boom")},
    )

    rows, missing = S._mcs_title_index_state()
    assert missing == ["T"]
    assert rows, "the other 25 letters still contribute rows"

    # The search still returns hits, but says the index is short so a
    # truncated result isn't reported as a complete one.
    page = S.search_mcstories("mc")
    assert page, "a partial index still searches what it has"
    assert page.partial_note.startswith("index incomplete: no T titles")


def test_mcstories_index_treats_a_zero_story_page_as_a_failure(monkeypatch):
    # A Cloudflare interstitial is HTTP 200 with a body the fetch layer
    # can't distinguish from a real page — it just has no div.story.
    # Trusting the status code cached the gap as though it were real.
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(
        monkeypatch, EROTICA / "mcstories_titles.html",
        broken={"T": "<html><body>Just a moment...</body></html>"},
    )

    _rows, missing = S._mcs_title_index_state()
    assert missing == ["T"]


def test_mcstories_index_retries_only_the_failed_letter(monkeypatch):
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(
        monkeypatch, EROTICA / "mcstories_titles.html",
        broken={"T": S.SearchFetchError("boom")},
    )
    assert S._mcs_title_index_state()[1] == ["T"]

    # Heal the site and step past the short retry window: the index must
    # repair itself, and must re-fetch T *only* — the 25 good letters
    # are still inside their full TTL.
    fetched: list[str] = []

    def counting_fetch(url):
        fetched.append(url)
        return _read(EROTICA / "mcstories_titles.html")

    _patch_index_fetch(monkeypatch, counting_fetch)
    base = S.time.time()
    monkeypatch.setattr(
        S.time, "time",
        lambda: base + S._MCS_PARTIAL_INDEX_RETRY_S + 1,
    )

    rows, missing = S._mcs_title_index_state()
    assert missing == []
    assert rows
    assert fetched == [S._MCS_TITLE_PAGE_URL.format(letter="T")]


def test_mcstories_complete_index_is_not_refetched_within_its_ttl(monkeypatch):
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(monkeypatch, EROTICA / "mcstories_titles.html", broken={})
    assert S._mcs_title_index_state()[1] == []

    calls: list[str] = []
    _patch_index_fetch(monkeypatch, lambda url: (calls.append(url), "")[1])
    rows, missing = S._mcs_title_index_state()
    assert calls == [], "a warm complete index must not re-crawl"
    assert missing == []
    assert rows


def test_mcstories_letter_retry_backs_off_while_it_keeps_failing(monkeypatch):
    # A site that is rate-limiting or serving a challenge fails all 26
    # letters, so a flat retry window meant every search a minute apart
    # re-crawled the whole alphabet. Consecutive failures have to widen
    # the window instead, up to the normal TTL.
    monkeypatch.setattr(S, "_mcs_title_index", {})
    attempts: list[str] = []

    def always_fails(url):
        attempts.append(url)
        raise S.SearchFetchError("boom")

    _patch_index_fetch(monkeypatch, always_fails)
    clock = S.time.time()
    monkeypatch.setattr(S.time, "time", lambda: clock)

    assert S._mcs_title_index_state()[1] == list(S._MCS_TITLE_LETTERS)
    assert len(attempts) == 26
    assert S._mcs_title_index["A"]["ttl"] == S._MCS_PARTIAL_INDEX_RETRY_S

    # One retry window later: a second attempt, and the window doubles.
    attempts.clear()
    clock += S._MCS_PARTIAL_INDEX_RETRY_S + 1
    S._mcs_title_index_state()
    assert len(attempts) == 26
    assert S._mcs_title_index["A"]["ttl"] == S._MCS_PARTIAL_INDEX_RETRY_S * 2

    # ...and that wider window is respected: the same elapsed time no
    # longer triggers a crawl.
    attempts.clear()
    clock += S._MCS_PARTIAL_INDEX_RETRY_S + 1
    S._mcs_title_index_state()
    assert attempts == [], "a backed-off letter must not be retried yet"


def test_mcstories_letter_backoff_is_capped_and_reset_by_a_success(monkeypatch):
    monkeypatch.setattr(S, "_mcs_title_index", {})
    # Far more consecutive failures than the ladder needs to saturate.
    S._mcs_title_index["A"] = {
        "rows": [], "built_at": 0.0, "ttl": 1.0, "failures": 40,
    }
    _letter_fetch(
        monkeypatch, EROTICA / "mcstories_titles.html",
        broken={"A": S.SearchFetchError("boom")},
    )
    S._mcs_title_index_state()
    assert S._mcs_title_index["A"]["ttl"] == S._MCS_TITLE_INDEX_TTL_S, (
        "the backoff must not grow past the normal refresh interval"
    )

    # A letter that comes back drops straight to the normal TTL rather
    # than staying on the ladder.
    monkeypatch.setattr(S, "_mcs_title_index", {
        "A": {"rows": [], "built_at": 0.0, "ttl": 1.0, "failures": 5},
    })
    _letter_fetch(monkeypatch, EROTICA / "mcstories_titles.html", broken={})
    S._mcs_title_index_state()
    assert S._mcs_title_index["A"]["failures"] == 0
    assert S._mcs_title_index["A"]["ttl"] == S._MCS_TITLE_INDEX_TTL_S


def test_mcstories_partial_note_reaches_the_fan_out_site_stats(monkeypatch):
    monkeypatch.setattr(S, "_mcs_title_index", {})
    _letter_fetch(
        monkeypatch, EROTICA / "mcstories_titles.html",
        broken={"T": S.SearchFetchError("boom")},
    )

    merged = S.search_erotica("mc", sites=["mcstories"])
    stats = merged.site_stats["mcstories"]
    assert stats["ok"] is True, "a partial index is degraded, not failed"
    assert stats["count"] > 0
    assert "no T titles" in (stats["notice"] or "")
