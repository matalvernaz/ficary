"""Smoke tests for the erotica subpackage.

Each scraper gets: URL parsing (happy + error paths), site registration
in ``ficary.sites``, and ``canonical_url`` round-trip. Full end-to-end
download tests would require live HTTP and are deliberately omitted —
these tests run offline in <1s so they gate every commit.
"""

import pytest

from ficary.erotica import (
    AFFScraper,
    BDSMLibraryScraper,
    ChastityMansionScraper,
    ChyoaScraper,
    DarkWandererScraper,
    FictionmaniaScraper,
    GiantessWorldScraper,
    GreatFeetScraper,
    LiteroticaScraper,
    LushStoriesScraper,
    MCStoriesScraper,
    MousepadScraper,
    NiftyScraper,
    ReadOnlyMindScraper,
    SexStoriesScraper,
    StoriesOnlineScraper,
    TGStorytimeScraper,
    TicklingForumScraper,
)
from ficary.erotica.search import (
    EROTICA_SITE_SLUGS,
    EROTICA_TAG_VOCABULARY,
    ErotiCAResults,
    TAG_SITE_COVERAGE,
    _normalise_sites,
    _normalise_tags,
    _parse_word_threshold,
    search_erotica,
    tag_site_count,
    tag_sites_for,
)
from ficary.sites import EROTICA_SCRAPERS, canonical_url, detect_scraper


# ── Registration ──────────────────────────────────────────────────

def test_all_erotica_scrapers_registered():
    expected = {
        LiteroticaScraper, AFFScraper, StoriesOnlineScraper, NiftyScraper,
        SexStoriesScraper, MCStoriesScraper, LushStoriesScraper,
        FictionmaniaScraper, TGStorytimeScraper, ChyoaScraper,
        DarkWandererScraper, GreatFeetScraper, BDSMLibraryScraper,
        MousepadScraper, ReadOnlyMindScraper, GiantessWorldScraper,
        ChastityMansionScraper, TicklingForumScraper,
    }
    assert set(EROTICA_SCRAPERS) == expected


@pytest.mark.parametrize("url,expected_cls", [
    ("https://hp.adult-fanfiction.org/story.php?no=600100488", AFFScraper),
    ("https://storiesonline.net/s/40467/slug", StoriesOnlineScraper),
    ("https://www.nifty.org/nifty/gay/college/the-brotherhood/", NiftyScraper),
    ("https://www.sexstories.com/story/114893/slug", SexStoriesScraper),
    ("https://mcstories.com/AToZeb/", MCStoriesScraper),
    ("https://www.lushstories.com/stories/cuckold/a-modern-relationship",
     LushStoriesScraper),
    ("https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345",
     FictionmaniaScraper),
    ("https://www.literotica.com/s/my-story", LiteroticaScraper),
    ("https://www.tgstorytime.com/viewstory.php?sid=9219", TGStorytimeScraper),
    ("https://chyoa.com/story/Insurance-Salesman-s.14", ChyoaScraper),
    ("https://chyoa.com/chapter/Ooh-that-s-hot.17", ChyoaScraper),
    ("https://darkwanderer.net/threads/foo.12345/", DarkWandererScraper),
    ("https://darkwanderer.net/threads/foo.12345/page-3",
     DarkWandererScraper),
    ("https://www.greatfeet.com/stories/ts1735.htm", GreatFeetScraper),
    ("https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=197281",
     MousepadScraper),
    ("https://www.tapatalk.com/groups/themousepad/something-about-her-t197281.html",
     MousepadScraper),
    ("http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
     BDSMLibraryScraper),
    ("http://www.bdsmlibrary.com/stories/chapter.php?storyid=10994&chapterid=31865",
     BDSMLibraryScraper),
    ("https://readonlymind.com/@Krungu5/SmallPackageBigPrize/",
     ReadOnlyMindScraper),
    ("https://giantessworld.net/viewstory.php?sid=11467",
     GiantessWorldScraper),
    ("https://chastitymansion.com/forums/index.php?threads/some-story.63479/",
     ChastityMansionScraper),
    ("https://www.ticklingforum.com/threads/some-story.42755/",
     TicklingForumScraper),
])
def test_detect_scraper_routes_correctly(url, expected_cls):
    assert detect_scraper(url) is expected_cls


# ── URL canonicalisation ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # AFF preserves subdomain + ?no=; strips chapter & other params.
    (
        "https://hp.adult-fanfiction.org/story.php?no=600100488&chapter=2",
        "https://hp.adult-fanfiction.org/story.php?no=600100488",
    ),
    # Same id on a different subdomain stays distinct.
    (
        "https://naruto.adult-fanfiction.org/story.php?no=600100488",
        "https://naruto.adult-fanfiction.org/story.php?no=600100488",
    ),
    # SOL: drops slug, keeps numeric id.
    (
        "https://storiesonline.net/s/40467/ouroboros-dorm-dipping",
        "https://storiesonline.net/s/40467",
    ),
    # SexStories: drops slug, keeps numeric id.
    (
        "https://www.sexstories.com/story/114893/slug",
        "https://www.sexstories.com/story/114893",
    ),
    # MCStories: drops index.html / trailing slash variants.
    (
        "https://mcstories.com/AToZeb/index.html",
        "https://mcstories.com/AToZeb/",
    ),
    # Nifty: directory path preserved.
    (
        "https://www.nifty.org/nifty/gay/college/the-brotherhood",
        "https://www.nifty.org/nifty/gay/college/the-brotherhood/",
    ),
    # Lush: category + slug preserved.
    (
        "https://www.lushstories.com/stories/cuckold/a-modern-relationship",
        "https://www.lushstories.com/stories/cuckold/a-modern-relationship",
    ),
    # Fictionmania: reader page + storyID preserved.
    (
        "https://fictionmania.tv/stories/readhtmlstory.html?storyID=74553&junk=1",
        "https://fictionmania.tv/stories/readhtmlstory.html?storyID=74553",
    ),
    # TGStorytime: keep sid, drop chapter/ageconsent churn.
    (
        "https://www.tgstorytime.com/viewstory.php?sid=9219&chapter=2&ageconsent=ok",
        "https://www.tgstorytime.com/viewstory.php?sid=9219",
    ),
    # Chyoa: both /story and /chapter collapse to /chapter form.
    (
        "https://chyoa.com/story/Insurance-Salesman-s.14",
        "https://chyoa.com/chapter/Insurance-Salesman-s.14",
    ),
    # Dark Wanderer: strip /page-N from paginated thread URLs.
    (
        "https://darkwanderer.net/threads/foo.12345/page-5",
        "https://darkwanderer.net/threads/foo.12345/",
    ),
    # GreatFeet: story path preserved verbatim.
    (
        "https://www.greatfeet.com/stories/ts1735.htm",
        "https://www.greatfeet.com/stories/ts1735.htm",
    ),
    # BDSM Library: story id lives in ``?storyid=N``; chapter URLs
    # collapse to the story page so chapter and story variants dedupe.
    # HTTPS cert is expired so the canonical form stays http://.
    (
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
    ),
    (
        "http://www.bdsmlibrary.com/stories/chapter.php?storyid=10994&chapterid=31865",
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
    ),
    # ReadOnlyMind: chapter pages collapse to the story overview.
    (
        "https://readonlymind.com/@Krungu5/SmallPackageBigPrize/2/",
        "https://readonlymind.com/@Krungu5/SmallPackageBigPrize/",
    ),
    # Giantess World: keep sid, drop chapter/textsize churn.
    (
        "https://giantessworld.net/viewstory.php?sid=11467&chapter=3",
        "https://giantessworld.net/viewstory.php?sid=11467",
    ),
    # Chastity Mansion: thread ref lives in the query string; page-N
    # and the rewritten /forums/threads/ shape both collapse.
    (
        "https://chastitymansion.com/forums/index.php?threads/a-b.63479/page-2",
        "https://chastitymansion.com/forums/index.php?threads/a-b.63479/",
    ),
    (
        "https://chastitymansion.com/forums/threads/a-b.63479/",
        "https://chastitymansion.com/forums/index.php?threads/a-b.63479/",
    ),
    # TicklingForum: strip page-N like Dark Wanderer.
    (
        "https://www.ticklingforum.com/threads/a-tale.42755/page-9",
        "https://www.ticklingforum.com/threads/a-tale.42755/",
    ),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


# ── Per-scraper URL parsing ───────────────────────────────────────

class TestAFFParsing:
    def test_story_id(self):
        assert (
            AFFScraper.parse_story_id(
                "https://hp.adult-fanfiction.org/story.php?no=600100488"
            ) == 600100488
        )

    def test_bare_id(self):
        assert AFFScraper.parse_story_id("600100488") == 600100488

    def test_subdomain_parsing(self):
        assert (
            AFFScraper.parse_subdomain(
                "https://naruto.adult-fanfiction.org/story.php?no=1"
            ) == "naruto"
        )

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            AFFScraper.parse_story_id("https://example.com/foo")


class TestAFFAuthorLinkFallbacks:
    """AFF rotates its author-link pattern every few years. The
    resolver walks a chain of href shapes down to a structural
    fallback — pin each rung so a future redesign fails loudly
    through one of these tests instead of silently losing the author
    field on every story."""

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")

    def test_modern_profile_link(self):
        soup = self._soup(
            '<a href="https://members.adult-fanfiction.org/'
            'profile.php?id=123">WriterX</a>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "WriterX"

    def test_legacy_authorlinks_php(self):
        soup = self._soup(
            '<a href="https://hp.adult-fanfiction.org/'
            'authorlinks.php?no=42">OldSchool</a>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert "OldSchool" in link.get_text()

    def test_structural_fallback_via_story_header_author(self):
        """If AFF drops the old href shapes entirely, a link inside
        ``div.story-header-author`` still has to resolve."""
        soup = self._soup(
            '<div class="story-header-author">'
            '<a href="/some/new/author/url?q=1">FreshWriter</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "FreshWriter"

    def test_structural_fallback_via_generic_author_class(self):
        """Second-tier structural fallback: any container whose class
        mentions ``author``. Catches a redesign that renamed the
        specific header class."""
        soup = self._soup(
            '<div class="byline-author">'
            '<a href="/author/new">NamedWriter</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "NamedWriter"

    def test_returns_none_when_nothing_matches(self):
        soup = self._soup(
            '<p>Just prose, no author markers anywhere.</p>'
        )
        assert AFFScraper._find_author_link(soup) is None

    def test_modern_pattern_preferred_over_legacy(self):
        """When both shapes appear on the same page (crossover period
        between AFF layouts), the modern ``profile.php?id=`` wins so
        the resulting author URL is the one AFF actually serves now."""
        soup = self._soup(
            '<div>'
            '<a href="/authorlinks.php?no=1">LegacyName</a>'
            '<a href="https://members.adult-fanfiction.org/'
            'profile.php?id=999">ModernName</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "ModernName"


class TestSOLParsing:
    def test_story_id(self):
        assert (
            StoriesOnlineScraper.parse_story_id(
                "https://storiesonline.net/s/40467/slug"
            ) == 40467
        )

    def test_bare_id(self):
        assert StoriesOnlineScraper.parse_story_id("40467") == 40467

    def test_is_author_url(self):
        assert StoriesOnlineScraper.is_author_url(
            "https://storiesonline.net/a/fan-fiction-man"
        )
        assert not StoriesOnlineScraper.is_author_url(
            "https://storiesonline.net/s/40467"
        )


class TestNiftyParsing:
    def test_story_path(self):
        assert (
            NiftyScraper.parse_story_id(
                "https://www.nifty.org/nifty/gay/college/the-brotherhood/"
            ) == "nifty/gay/college/the-brotherhood"
        )

    def test_rejects_non_nifty(self):
        with pytest.raises(ValueError):
            NiftyScraper.parse_story_id("https://example.com")


class TestSexStoriesParsing:
    def test_story_id(self):
        assert (
            SexStoriesScraper.parse_story_id(
                "https://www.sexstories.com/story/114893/slug"
            ) == 114893
        )


class TestMCStoriesParsing:
    def test_story_slug(self):
        assert (
            MCStoriesScraper.parse_story_id(
                "https://mcstories.com/AToZeb/"
            ) == "AToZeb"
        )

    def test_chapter_url(self):
        assert (
            MCStoriesScraper.parse_story_id(
                "https://mcstories.com/AToZeb/AToZeb.html"
            ) == "AToZeb"
        )

    def test_bare_slug(self):
        assert MCStoriesScraper.parse_story_id("AToZeb") == "AToZeb"


class TestLushStoriesParsing:
    def test_story_tuple(self):
        assert (
            LushStoriesScraper.parse_story_id(
                "https://www.lushstories.com/stories/cuckold/a-modern-relationship"
            ) == ("cuckold", "a-modern-relationship")
        )


class TestFictionmaniaParsing:
    def test_story_id(self):
        assert (
            FictionmaniaScraper.parse_story_id(
                "https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345"
            ) == 12345
        )

    def test_text_url_also_works(self):
        assert (
            FictionmaniaScraper.parse_story_id(
                "https://fictionmania.tv/stories/readtextstory.html?storyID=12345"
            ) == 12345
        )

    def test_bare_id(self):
        assert FictionmaniaScraper.parse_story_id("12345") == 12345


# ── Unified search normalization ──────────────────────────────────

def test_normalise_sites_gui_single():
    assert _normalise_sites(None, "literotica") == ["literotica"]


def test_normalise_sites_all_collapses_to_none():
    assert _normalise_sites(None, "all") is None
    assert _normalise_sites(None, "") is None
    assert _normalise_sites(["all"], "") is None


def test_normalise_sites_list_pass_through():
    assert _normalise_sites(["mcstories", "aff"], "") == ["mcstories", "aff"]


def test_normalise_tags_string_and_list():
    assert _normalise_tags("femdom, feet, mind-control") == [
        "femdom", "feet", "mind-control",
    ]
    assert _normalise_tags(["femdom", "feet"]) == ["femdom", "feet"]
    assert _normalise_tags(None) == []


def test_parse_word_threshold():
    assert _parse_word_threshold("") == 0
    assert _parse_word_threshold("any") == 0  # "any" falls through as 0
    assert _parse_word_threshold("5k+") == 5000
    assert _parse_word_threshold("30k") == 30000
    assert _parse_word_threshold("1000") == 1000


def test_erotica_tag_vocabulary_includes_key_fetishes():
    # Explicit check so a future cleanup doesn't accidentally drop
    # the kinks the unified search was built for.
    for tag in ("femdom", "feet", "spanking", "cuckold", "mind-control"):
        assert tag in EROTICA_TAG_VOCABULARY


def test_erotica_site_slugs_have_labels():
    from ficary.erotica.search import EROTICA_SITE_LABELS
    for slug in EROTICA_SITE_SLUGS:
        assert slug in EROTICA_SITE_LABELS


def test_search_erotica_empty_sites_returns_empty():
    # ``sites=[]`` with no usable entries short-circuits without HTTP.
    assert search_erotica("", sites=["nonexistent"]) == []


# ── Tag-capability filter ────────────────────────────────────────


def test_site_supports_all_tags_matches_coverage_table():
    """The per-site tag-capability check must agree with
    :data:`TAG_SITE_COVERAGE` — the dispatcher and the GUI tag-picker
    annotation both consult this dict, so a disagreement would leak
    noise back into the fan-out."""
    from ficary.erotica.search import _site_supports_all_tags

    # Literotica is one of the well-covered sites; bdsm is in the
    # vocabulary and Literotica covers it.
    assert _site_supports_all_tags("literotica", ["bdsm"]) is True

    # AFF doesn't filter by tag — its search adapter ignores the
    # ``tags`` kwarg. The coverage table reflects that for ``bdsm``.
    assert _site_supports_all_tags("aff", ["bdsm"]) is False

    # Empty tag list is always supported (non-tag search).
    assert _site_supports_all_tags("aff", []) is True

    # Niche tags (not in the vocabulary) match no site — safer
    # default than letting an arbitrary free-text tag bypass the
    # filter and return raw recent/popular rows.
    assert _site_supports_all_tags("literotica", ["nonexistent-tag"]) is False


def test_search_erotica_tag_only_drops_tag_ignoring_sites(monkeypatch):
    """Tag-only fan-out must drop sites that ignore the tag —
    otherwise they fall back to recent/popular browses and flood the
    merged result set with unrelated rows. The 'all sites + bdsm
    returns junk' bug.

    Sites that fold ``tags`` into a full-text search payload
    (:data:`_TAG_TEXT_FOLD_SITES`) ARE invoked even when the tag
    isn't pre-mapped — they handle arbitrary tags sensibly, so
    dropping them would discard real discovery surface. The forbidden
    set still catches sites whose scrapers genuinely ignore the
    ``tags`` kwarg entirely.

    The earlier slug-passthrough escape hatch (Lush) was retired
    when each adapter started routing tags through
    :func:`_translate_tag` — Lush now returns ``[]`` for tags it
    doesn't carry, so the gate no longer needs to wave it through.
    """
    from ficary.erotica import search as erotica_search
    from ficary.erotica.search import _TAG_TEXT_FOLD_SITES

    called: set = set()

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            called.add(site_slug)
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    search_erotica("", tags=["bdsm"])

    expected_callers = (
        set(TAG_SITE_COVERAGE["bdsm"])
        | _TAG_TEXT_FOLD_SITES
    )
    # Sites whose adapters ignore the ``tags`` kwarg entirely must
    # NOT be called for a tag-only bdsm search — their scrapers
    # would silently fall back to a default browse.
    forbidden = {
        "aff", "fictionmania", "tgstorytime", "chyoa",
        "darkwanderer", "greatfeet",
    }
    assert called == expected_callers
    assert called.isdisjoint(forbidden)


def test_search_erotica_skips_filter_when_site_explicitly_picked(monkeypatch):
    """A user who explicitly picked a single site has opted into
    whatever that site returns — don't second-guess them."""
    from ficary.erotica import search as erotica_search

    called: set = set()

    def aff_stub(query, **kwargs):
        called.add("aff")
        return []

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {**erotica_search._SITE_FNS, "aff": aff_stub},
    )

    # Even though AFF doesn't cover bdsm, picking it explicitly
    # should still fire its search.
    search_erotica("", sites=["aff"], tags=["bdsm"])
    assert "aff" in called


def test_normalise_sites_extracts_slug_from_label():
    """The Site dropdown shows ``Adult-FanFiction.org (aff)`` etc. for
    readability; ``_normalise_sites`` must strip the label and return
    the bare slug so the fan-out can look it up in ``_SITE_FNS``."""
    from ficary.erotica.search import _normalise_sites, _extract_slug

    # Direct slug extraction
    assert _extract_slug("Adult-FanFiction.org (aff)") == "aff"
    assert _extract_slug("Literotica (literotica)") == "literotica"
    assert _extract_slug("literotica") == "literotica"
    assert _extract_slug("All erotica sites") == "all"
    assert _extract_slug("") == ""

    # Through the GUI's sites_choice path
    assert _normalise_sites(None, "Literotica (literotica)") == ["literotica"]
    assert _normalise_sites(None, "All erotica sites") is None  # "all" collapse
    assert _normalise_sites(None, "literotica") == ["literotica"]  # bare slug
    # Through the CLI / scripted ``sites`` list path
    assert _normalise_sites(["aff", "Literotica (literotica)"], "") == [
        "aff", "literotica",
    ]


def test_search_erotica_promotes_known_tag_query(monkeypatch):
    """Round-7 audit (v2.4.33): when the user types a bare word that is
    a known erotica tag (e.g. ``feet``) and didn't pick anything in the
    tag multi-picker, ``search_erotica`` should promote it to a tag
    search. Tag-capable sites then use their native tag URL instead of
    falling back to title filtering, which is dramatically better for
    broad discovery queries."""
    from ficary.erotica import search as erotica_search

    seen_tags: dict = {}

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            seen_tags[site_slug] = list(kwargs.get("tags") or [])
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    # User typed "feet" as the query; no tags picked. Expect promotion.
    search_erotica("feet")

    # GreatFeet is a tag-handler for feet and should now see the tag.
    assert seen_tags.get("greatfeet") == ["feet"]
    # Other tag-capable sites also see the promoted tag.
    assert seen_tags.get("literotica") == ["feet"]
    # AFF / Fictionmania / DarkWanderer / TGStorytime / Chyoa aren't in
    # TAG_SITE_COVERAGE["feet"] nor in text-fold/passthrough — they
    # were dropped from the fan-out.
    assert "fictionmania" not in seen_tags
    assert "tgstorytime" not in seen_tags


def test_search_erotica_does_not_promote_multiword_query(monkeypatch):
    """Promotion is exact-match only — multi-word queries that contain
    a tag word aren't promoted. Otherwise a query like ``"feet first"``
    would be silently rewritten to the ``feet`` tag, which is far too
    aggressive."""
    from ficary.erotica import search as erotica_search

    seen_tags: dict = {}

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            seen_tags[site_slug] = list(kwargs.get("tags") or [])
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    search_erotica("feet first")

    # No tag was promoted; the fan-out should run with empty tags.
    for site, tags in seen_tags.items():
        assert tags == [], f"site {site} unexpectedly saw promoted tags {tags}"


def test_search_erotica_total_sites_captures_eligible_cohort(monkeypatch):
    """``ErotiCAResults.total_sites`` should hold the canonical
    eligible-sites set so Load More can use it as a stable denominator
    for its ``all exhausted?`` check. Without this, subsequent pages
    invoke ``search_erotica`` with a shrunken active set (skip_sites
    pruned the exhausted ones) and the GUI's len-based comparison
    spuriously claims end-of-results."""
    from ficary.erotica import search as erotica_search

    def stub(query, **kwargs):
        return []

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub for site in erotica_search._SITE_FNS},
    )

    # bdsm covers four pre-mapped sites + sexstories (text-fold) +
    # lushstories (passthrough; already in pre-map). Expect total_sites
    # to include all six even though every stub returns an empty list.
    result = search_erotica("", tags=["bdsm"])
    assert "literotica" in result.total_sites
    assert "sexstories" in result.total_sites
    assert "fictionmania" not in result.total_sites
    # Snapshot must be a SET not a list/None so callers can do set ops.
    assert isinstance(result.total_sites, set)


def test_search_aff_returns_empty_when_no_fandom():
    """Round-7 audit (v2.4.32): AFF used to default ``fandom=\"hp\"``
    silently, leaking Harry Potter results into every empty-fandom
    erotica search. Now an empty fandom yields ``[]`` so the site is
    obviously skipped in the per-site stats panel."""
    from ficary.erotica.search import search_aff
    import ficary.erotica.search as erotica_search

    fetched: list = []
    original_fetch = erotica_search._fetch
    try:
        def remembering_fetch(url):
            fetched.append(url)
            return ""
        erotica_search._fetch = remembering_fetch

        assert search_aff("") == []
        assert search_aff("any query", fandom="") == []
        # No fetch should have been issued — the function bails before
        # touching the network.
        assert fetched == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_nifty_returns_empty_for_unsupported_tag_only():
    """Direct callers (or fan-outs that bypass the dispatcher
    filter) get an explicit ``[]`` for an unsupported tag-only
    query — no fallback to ``/gay/`` directory noise."""
    from ficary.erotica.search import search_nifty

    # Patch the fetch helper inline so this test stays offline.
    import ficary.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""  # would-be category page
        assert search_nifty("", tags=["bdsm"]) == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_mcstories_returns_empty_for_unmapped_tag_only():
    """Same defensive contract for MCStories — an unmapped tag with
    no free-text query returns ``[]`` instead of dumping the entire
    Titles index."""
    from ficary.erotica.search import search_mcstories

    import ficary.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""
        # "polyamory" isn't in _MCS_TAG_CODES.
        assert search_mcstories("", tags=["polyamory"]) == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_greatfeet_returns_empty_for_non_feet_tag_only():
    """GreatFeet's whole catalogue is the feet tag — a tag-only
    ``bdsm`` lookup must return ``[]`` rather than the homepage
    contents."""
    from ficary.erotica.search import search_greatfeet

    import ficary.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""
        assert search_greatfeet("", tags=["bdsm"]) == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_sol_accepts_both_permalink_shapes():
    """SOL bytag listings mix ``/s/<id>/<slug>`` and ``/n/<id>/<slug>``
    title hrefs on the same page. The parser matched only ``/n/`` and
    silently dropped every ``/s/`` row — a femdom browse returned 3 of
    10 rows on the live page. Both shapes must parse."""
    from ficary.erotica.search import search_sol

    fake_html = """
    <html><body>
      <h3 class="sname">1 <a href="/s/50310/female-fighting">Female Fighting</a>
        by <a href="/a/jim-priest">Jim Priest</a></h3>
      <h3 class="sname">2 <a href="/n/49720/laying-the-dragon">Laying the Dragon</a>
        by <a href="/a/jim-priest">Jim Priest</a></h3>
    </body></html>
    """
    import ficary.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: fake_html
        results = search_sol("", tags=["femdom"])
    finally:
        erotica_search._fetch = original_fetch
    assert [r["title"] for r in results] == [
        "Female Fighting", "Laying the Dragon",
    ]
    # Both shapes normalise to the /s/ URL the downloader consumes.
    assert results[0]["url"] == (
        "https://storiesonline.net/s/50310/female-fighting"
    )
    assert results[1]["url"] == (
        "https://storiesonline.net/s/49720/laying-the-dragon"
    )


def test_adapters_return_natural_page_size():
    """Adapters must pass through everything a site page carries.
    The old ``PER_SITE_LIMIT = 8`` truncated each already-fetched page
    (Literotica serves ~94 rows/page) and Load More advanced by *site
    page*, so the truncated rows were permanently skipped — a femdom
    search surfaced ~8 Literotica rows out of thousands."""
    import ficary.erotica.search as erotica_search
    from ficary.erotica.search import search_literotica_wrapped

    rows = [
        {"title": f"Story {i}", "author": "A", "url": f"u{i}",
         "summary": "", "fandom": ""}
        for i in range(30)
    ]
    original = erotica_search.search_literotica
    try:
        erotica_search.search_literotica = lambda q, page=1, **kw: list(rows)
        results = search_literotica_wrapped("", tags=["femdom"])
    finally:
        erotica_search.search_literotica = original
    assert len(results) == 30


def test_single_listing_adapters_window_by_page(monkeypatch):
    """Single-listing sites (MCStories et al.) map fan-out page N onto
    row window N of their one listing: every row is reachable via Load
    More, and the first off-the-end page returns ``[]`` — the fan-out's
    exhaustion signal. The old behaviour re-served page 1's rows for
    every page and never exhausted."""
    import ficary.erotica.search as erotica_search
    from ficary.erotica.search import search_mcstories

    listing = "".join(
        f'<tr><td><a href="../Story{i}/">Story {i}</a></td><td>fd</td></tr>'
        for i in range(3)
    )
    fake_html = f"<html><body><table>{listing}</table></body></html>"
    monkeypatch.setattr(erotica_search, "PER_SITE_PAGE_MAX", 2)
    monkeypatch.setattr(erotica_search, "_fetch", lambda url: fake_html)

    page1 = search_mcstories("", tags=["femdom"])
    page2 = search_mcstories("", tags=["femdom"], page=2)
    page3 = search_mcstories("", tags=["femdom"], page=3)
    assert [r["title"] for r in page1] == ["Story 0", "Story 1"]
    assert [r["title"] for r in page2] == ["Story 2"]
    assert page3 == []


def test_search_erotica_exhausts_only_on_empty_page(monkeypatch):
    """A partial page no longer marks a site exhausted (natural page
    sizes vary per site, so there is no meaningful "full batch"
    threshold) — only an empty page does."""
    import ficary.erotica.search as erotica_search
    from ficary.erotica.search import search_erotica

    row = {"title": "T", "author": "", "url": "u", "summary": ""}
    monkeypatch.setitem(
        erotica_search._SITE_FNS, "literotica",
        lambda q, **kw: [dict(row)] * 3,
    )
    monkeypatch.setitem(
        erotica_search._SITE_FNS, "ao3", lambda q, **kw: [],
    )
    results = search_erotica("", sites=["literotica", "ao3"])
    assert results.site_stats["literotica"]["exhausted"] is False
    assert results.site_stats["ao3"]["exhausted"] is True
    assert "ao3" in results.exhausted_sites


def test_search_bdsmlibrary_raises_on_form_bounce():
    """bdsmlibrary removed its public code-based advanced search
    (observed 2026-07): search.php ignores the code parameters and
    serves the search form back. That must surface as a
    :class:`SearchFetchError` — the fan-out records it as a failed
    site — not as a silent "0 results, ok"."""
    import pytest
    from ficary.erotica.search import SearchFetchError, search_bdsmlibrary

    form_shell = """
    <html><body>
      <form method="post" action="/stories/search.php">
        <input name="term" size="15" />
      </form>
    </body></html>
    """
    import ficary.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: form_shell
        with pytest.raises(SearchFetchError):
            search_bdsmlibrary("", tags=["femdom"])
    finally:
        erotica_search._fetch = original_fetch


# ── Multi-page erotica fan-out driver ────────────────────────────


def test_fetch_erotica_until_limit_accumulates_across_pages():
    """The erotica fan-out used to fetch one page per click —
    ``PER_SITE_LIMIT * supported_sites`` was the hard ceiling for a
    broad tag query. ``fetch_erotica_until_limit`` walks pages until
    it reaches the requested ``limit`` (or the page budget is hit),
    preserving the ``ErotiCAResults`` wrapper that carries the
    per-site stats panel the GUI binds to.
    """
    from ficary.erotica.search import ErotiCAResults
    from ficary.search import fetch_erotica_until_limit

    call_pages = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        call_pages.append(page)
        out = ErotiCAResults()
        # Each page yields a fresh batch of 8 rows from 'literotica'.
        out.extend([
            {
                "title": f"Story p{page} #{i}",
                "url": f"https://www.literotica.com/s/p{page}-n{i}",
                "site": "literotica",
            }
            for i in range(8)
        ])
        out.site_stats = {
            "literotica": {
                "count": 8, "ok": True, "error": None, "exhausted": False,
            },
        }
        out.exhausted_sites = set()
        return out

    results, next_page = fetch_erotica_until_limit(
        fake_search, "feet", limit=25, start_page=1,
    )
    # Need ceil(25 / 8) == 4 pages to reach 25.
    assert call_pages == [1, 2, 3, 4]
    assert len(results) >= 25
    # Wrapper survived — site_stats is on the merged object.
    assert "literotica" in results.site_stats
    assert results.site_stats["literotica"]["count"] == 32  # 8 * 4
    assert next_page == 5


def test_fetch_erotica_until_limit_stops_when_all_sites_exhausted():
    """If every active site flips its ``exhausted`` flag on page 1,
    the driver stops immediately — no point burning more page budget
    re-polling sites that already said they're done."""
    from ficary.erotica.search import ErotiCAResults
    from ficary.search import fetch_erotica_until_limit

    call_pages = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        call_pages.append(page)
        out = ErotiCAResults()
        out.extend([
            {
                "title": "lone result",
                "url": f"https://www.literotica.com/s/lone-{page}",
                "site": "literotica",
            },
        ])
        out.site_stats = {
            "literotica": {
                "count": 1, "ok": True, "error": None, "exhausted": True,
            },
        }
        out.exhausted_sites = {"literotica"}
        return out

    results, next_page = fetch_erotica_until_limit(
        fake_search, "feet", limit=25, start_page=1,
    )
    # Only one page — every site exhausted immediately.
    assert call_pages == [1]
    assert len(results) == 1
    assert "literotica" in results.exhausted_sites


def test_fetch_erotica_until_limit_forwards_exhausted_sites_as_skip():
    """A site exhausted on page N gets added to ``skip_sites`` for
    page N+1, so the dispatcher doesn't bother re-polling it."""
    from ficary.erotica.search import ErotiCAResults
    from ficary.search import fetch_erotica_until_limit

    skip_seen_per_call = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        skip_seen_per_call.append(set(skip_sites or ()))
        out = ErotiCAResults()
        if page == 1:
            # 'aff' is exhausted after page 1; 'literotica' keeps going.
            out.extend([
                {"title": "aff p1", "url": "https://hp.adult-fanfiction.org/story.php?no=1", "site": "aff"},
                {"title": "lit p1", "url": "https://www.literotica.com/s/p1", "site": "literotica"},
            ])
            out.site_stats = {
                "aff": {
                    "count": 1, "ok": True, "error": None, "exhausted": True,
                },
                "literotica": {
                    "count": 1, "ok": True, "error": None, "exhausted": False,
                },
            }
            out.exhausted_sites = {"aff"}
        else:
            # Page 2 should only be polling 'literotica' now.
            assert "aff" in (skip_sites or set())
            out.extend([
                {"title": "lit p2", "url": "https://www.literotica.com/s/p2", "site": "literotica"},
            ])
            out.site_stats = {
                "literotica": {
                    "count": 1, "ok": True, "error": None, "exhausted": True,
                },
            }
            out.exhausted_sites = {"literotica"}
        return out

    fetch_erotica_until_limit(fake_search, "feet", limit=25, start_page=1)
    # Page 1 didn't have anything pre-skipped; page 2 has 'aff' in skip.
    assert skip_seen_per_call[0] == set()
    assert "aff" in skip_seen_per_call[1]


# ── New scraper URL parsing ───────────────────────────────────────

class TestTGStorytimeParsing:
    def test_story_id(self):
        assert (
            TGStorytimeScraper.parse_story_id(
                "https://www.tgstorytime.com/viewstory.php?sid=9219"
            ) == 9219
        )

    def test_bare_id(self):
        assert TGStorytimeScraper.parse_story_id("9219") == 9219

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            TGStorytimeScraper.parse_story_id("https://example.com")


class TestChyoaParsing:
    def test_story_url(self):
        kind, slug, num = ChyoaScraper.parse_story_id(
            "https://chyoa.com/story/Insurance-Salesman-s.14"
        )
        assert (kind, slug, num) == ("story", "Insurance-Salesman-s", 14)

    def test_chapter_url(self):
        kind, slug, num = ChyoaScraper.parse_story_id(
            "https://chyoa.com/chapter/Ooh-that-s-hot.17"
        )
        assert (kind, slug, num) == ("chapter", "Ooh-that-s-hot", 17)


class TestDarkWandererParsing:
    def test_thread_id(self):
        assert (
            DarkWandererScraper.parse_story_id(
                "https://darkwanderer.net/threads/foo.12345/"
            ) == 12345
        )

    def test_bare_id(self):
        assert DarkWandererScraper.parse_story_id("12345") == 12345


class TestGreatFeetParsing:
    def test_story_id(self):
        assert (
            GreatFeetScraper.parse_story_id(
                "https://www.greatfeet.com/stories/ts1735.htm"
            ) == 1735
        )

    def test_bare_id(self):
        assert GreatFeetScraper.parse_story_id("1735") == 1735


class TestBDSMLibraryParsing:
    """BDSM Library URL → story id parsing.

    The site speaks plain HTTP only (HTTPS cert is expired) so the
    parser accepts both schemes and stores nothing scheme-specific.
    Story ids come from ``?storyid=<N>`` on both ``story.php`` and
    ``chapter.php``.
    """

    def test_story_url(self):
        assert (
            BDSMLibraryScraper.parse_story_id(
                "http://www.bdsmlibrary.com/stories/story.php?storyid=10994"
            ) == 10994
        )

    def test_chapter_url(self):
        assert (
            BDSMLibraryScraper.parse_story_id(
                "http://www.bdsmlibrary.com/stories/chapter.php"
                "?storyid=10994&chapterid=31865"
            ) == 10994
        )

    def test_bare_id(self):
        assert BDSMLibraryScraper.parse_story_id("10994") == 10994

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            BDSMLibraryScraper.parse_story_id("https://example.com/foo")

    def test_chapter_links_parsed_in_order(self):
        from bs4 import BeautifulSoup
        # Minimal story.php listing — three chapter anchors. The
        # parser keys off the ``chapter.php?storyid=N&chapterid=M``
        # href shape and returns (1-based-index, chapterid, title)
        # tuples in document order.
        html = """
        <html><body>
          <a href="/stories/chapter.php?storyid=42&chapterid=100">Part 1</a>
          <a href="/stories/chapter.php?storyid=42&chapterid=101">Part 2</a>
          <a href="/stories/chapter.php?storyid=42&chapterid=102">Part 3</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        chap_list = BDSMLibraryScraper._chapter_links(soup, 42)
        assert chap_list == [
            (1, 100, "Part 1"),
            (2, 101, "Part 2"),
            (3, 102, "Part 3"),
        ]


# ── UX plumbing ───────────────────────────────────────────────────

def test_erotica_results_carries_stats():
    r = ErotiCAResults()
    r.site_stats = {"mcstories": {"count": 8, "ok": True}}
    r.exhausted_sites = {"mcstories"}
    assert r.site_stats["mcstories"]["count"] == 8
    assert "mcstories" in r.exhausted_sites


def test_tag_site_count_femdom_well_covered():
    # femdom must be on at least 4 sites; otherwise tag-picker
    # annotation misleads the user about coverage.
    assert tag_site_count("femdom") >= 4


def test_tag_site_count_feet_includes_greatfeet():
    # GreatFeet is the dedicated feet archive — the one we regretted
    # missing. Guard against someone removing it from the feet list.
    assert "greatfeet" in tag_sites_for("feet")


def test_tag_site_count_every_vocabulary_tag_has_coverage():
    # No tag should appear in the vocabulary with zero sites — that
    # would be a broken entry telling the user "this tag works" when
    # it doesn't.
    for tag in EROTICA_TAG_VOCABULARY:
        assert tag_site_count(tag) >= 1, f"tag {tag!r} has no sites"


def test_normalise_tags_strips_coverage_annotation():
    # GUI passes "femdom [5 sites]"; scraper needs bare "femdom".
    assert _normalise_tags("femdom [5 sites], feet [5 sites]") == [
        "femdom", "feet",
    ]


def test_tag_coverage_only_references_registered_sites():
    # Every site listed under a tag must exist in the fan-out
    # registry — otherwise tag selection would silently skip a site
    # we claim to cover.
    known_sites = set(EROTICA_SITE_SLUGS) - {"all"}
    for tag, sites in TAG_SITE_COVERAGE.items():
        for site in sites:
            assert site in known_sites, (
                f"tag {tag!r} references unknown site {site!r}"
            )


# ── Per-site tag translation layer ────────────────────────────────


def test_refining_tags_for_matts_interests_are_in_vocabulary():
    """The 2.4.43 expansion added narrower discovery axes under each
    of Matt's three core interests. Pin them so a future vocab churn
    can't quietly drop one — the whole point of having
    ``foot-worship`` separate from ``feet`` is to let users
    discriminate between them.
    """
    foot_refinements = {"foot-worship", "footjob", "trampling"}
    femdom_refinements = {
        "pegging", "tease-and-denial", "cfnm", "strap-on",
        "female-led", "body-worship",
    }
    cunnilingus_refinements = {"face-sitting", "queening"}
    for tag in foot_refinements | femdom_refinements | cunnilingus_refinements:
        assert tag in EROTICA_TAG_VOCABULARY, f"missing refining tag {tag!r}"
        # Each must also have at least two sites covering it —
        # one-site coverage doesn't justify a vocab slot.
        assert tag_site_count(tag) >= 2, (
            f"{tag!r} only carries {tag_site_count(tag)} sites — likely a "
            "wiring bug; refining tags should hit multiple archives"
        )


def test_wattpad_in_erotica_fan_out():
    """Wattpad must be a fan-out site after 2.4.43, with a real
    adapter wired in. Verifying via the registry rather than calling
    live so the test stays offline."""
    from ficary.erotica.search import _SITE_FNS, EROTICA_SITE_LABELS
    assert "wattpad" in _SITE_FNS, "wattpad not registered in fan-out"
    assert callable(_SITE_FNS["wattpad"])
    assert EROTICA_SITE_LABELS.get("wattpad") == "Wattpad"


def test_wattpad_adapter_parses_json_ld_listitems(monkeypatch):
    """The Wattpad tag-page parser keys off JSON-LD ``ListItem``
    blocks embedded in the page (more durable than the rotating
    Tailwind class names Wattpad uses for the rendered cards).
    Verify a representative ListItem shape extracts cleanly without
    going to the network.
    """
    from ficary.erotica import search as erotica_search
    from ficary.erotica.search import search_wattpad_erotica

    fake_html = """
    <html><body>
    <script type="application/ld+json">{"@context":"http://schema.org",
    "@type":"ListItem","name":"Test Story",
    "description":"A test story summary.",
    "url":"https://www.wattpad.com/story/123456-test-story","position":1}
    </script>
    <script>{"@context":"http://schema.org","@type":"ListItem",
    "name":"Another Story","description":"second summary",
    "url":"https://www.wattpad.com/story/789012-another-story","position":2}
    </script>
    </body></html>
    """

    def fake_fetch(url):
        return fake_html
    monkeypatch.setattr(erotica_search, "_fetch", fake_fetch)
    results = search_wattpad_erotica("", tags=["femdom"])
    assert len(results) == 2
    assert results[0]["title"] == "Test Story"
    assert results[0]["url"].endswith("/story/123456-test-story")
    assert results[0]["site"] == "wattpad"


def test_translate_tag_resolves_matts_interests():
    """The three interests that drove the 2.4.42 search rework
    (foot fetish, femdom, cunnilingus) must translate to a real
    site-specific slug for every site that's claimed to cover them
    in :data:`TAG_SITE_COVERAGE`.

    Earlier revisions passed the unified vocab slug through
    verbatim, which silently 404-ed into stub / all-tags-index pages
    for every site whose actual slug shape differed (SOL needs
    ``foot-fetish``, Lush needs ``fetish``, AO3 needs Title-Case
    ``Foot Fetish``, etc.). Pin a representative translation so any
    future regression hits a test instead of a quiet zero-result
    fan-out.
    """
    from ficary.erotica.search import _translate_tag

    # Feet: literotica permissive, SOL has its own slug, Lush has
    # only the umbrella ``fetish`` category, AO3 uses title-case.
    assert _translate_tag("literotica", "feet") == "feet"
    assert _translate_tag("storiesonline", "feet") == "foot-fetish"
    assert _translate_tag("lushstories", "feet") == "fetish"
    assert _translate_tag("ao3", "feet") == "Foot Fetish"
    assert _translate_tag("bdsmlibrary", "feet") == "41"

    # Femdom: SOL uses ``femaledom``, BDSM Library uses the F/m
    # code id (13). Literotica/Lush/AO3 use the canonical name.
    assert _translate_tag("storiesonline", "femdom") == "femaledom"
    assert _translate_tag("lushstories", "femdom") == "femdom"
    assert _translate_tag("ao3", "femdom") == "Femdom"
    assert _translate_tag("bdsmlibrary", "femdom") == "13"
    assert _translate_tag("mcstories", "femdom") == "fd"

    # Cunnilingus: Literotica + AO3 have a specific tag; SOL +
    # Lush fold it under oral-sex.
    assert _translate_tag("literotica", "cunnilingus") == "cunnilingus"
    assert _translate_tag("ao3", "cunnilingus") == "Cunnilingus"
    assert _translate_tag("storiesonline", "cunnilingus") == "oral-sex"
    assert _translate_tag("lushstories", "cunnilingus") == "oral-sex"


def test_translate_tag_returns_none_for_untranslatable():
    """Sites with no representation for a tag must return ``None`` —
    not a vocab passthrough that would 404 into a stub page."""
    from ficary.erotica.search import _translate_tag

    # MCStories has only 26 codes; cunnilingus / face-sitting / etc.
    # aren't among them.
    assert _translate_tag("mcstories", "cunnilingus") is None
    assert _translate_tag("mcstories", "face-sitting") is None
    # BDSM Library has no humiliation code.
    assert _translate_tag("bdsmlibrary", "humiliation") is None
    # Lush has no harem category.
    # (Confirmed: not in the Lush /stories/<slug> live category list.)
    assert _translate_tag("lushstories", "futanari") is None


def test_mcstories_tag_codes_no_longer_misroute():
    """The 2.4.42 fix corrected seven wrong MCStories code mappings.
    Pin the post-fix correctness so a future edit can't silently
    reintroduce the ``cb=cheating`` / ``ft=feet`` / ``gr=group-sex``
    / ``hm=hypnosis`` / ``hu=humiliation`` / ``la=interracial`` /
    ``ma=transgender`` confusion that sent users to unrelated tag
    pages."""
    from ficary.erotica.search import _MCS_TAG_CODES

    # Tags that USED to map but had no real MCStories representation
    # must now be absent. Adding them back without verifying the code
    # would mean silently sending users to e.g. comic-book stories
    # when they ask for cheating.
    forbidden_keys = {
        "cheating", "feet", "group-sex", "orgy",
        "interracial", "transgender", "futanari",
    }
    for key in forbidden_keys:
        assert key not in _MCS_TAG_CODES, (
            f"{key!r} has no real MCStories tag — last time we mapped "
            "it we landed users on a wholly unrelated tag page."
        )
    # Tags that DO have a real MCStories code must still resolve.
    assert _MCS_TAG_CODES["femdom"] == "fd"
    assert _MCS_TAG_CODES["humiliation"] == "hm"
    assert _MCS_TAG_CODES["mind-control"] == "mc"


class TestSiteDrift2026_07Fixes:
    """Adapters repaired in the 2026-07 audit: fictionmania (RSS),
    chyoa (category/trending), darkwanderer (Author's Den walk),
    nifty (absolute hrefs)."""

    FM_RSS = (
        "<rss><channel>"
        "<item><title>Jul10 - ¿ A Perfect Housewife [Pollymeric]</title>"
        "<description>Blurb here</description>"
        "<link>https://www.fictionmania.tv/stories/readhtmlstory.html"
        "?storyID=178360881719039899</link>"
        "<author>Pollymeric</author>"
        "<pubDate>Fri, 10 Jul 2026 00:05:18 -0500</pubDate></item>"
        "<item><title>Jul09 - Second Tale [Rinny]</title>"
        "<description>Other blurb</description>"
        "<link>https://www.fictionmania.tv/stories/readhtmlstory.html"
        "?storyID=22</link>"
        "<author>Rinny</author>"
        "<pubDate>Thu, 09 Jul 2026 08:00:00 -0500</pubDate></item>"
        "</channel></rss>"
    )

    def test_fictionmania_parses_rss_listing(self, monkeypatch):
        from ficary.erotica import search as es

        monkeypatch.setattr(es, "_fetch", lambda url: self.FM_RSS)
        rows = es.search_fictionmania("")
        assert [r["title"] for r in rows] == [
            "A Perfect Housewife", "Second Tale",
        ]
        assert rows[0]["author"] == "Pollymeric"
        assert rows[0]["summary"] == "Blurb here"
        assert rows[0]["updated"].startswith("2026-07-10")
        assert rows[0]["url"].endswith("storyID=178360881719039899")
        filtered = es.search_fictionmania("housewife")
        assert [r["title"] for r in filtered] == ["A Perfect Housewife"]

    def test_chyoa_tag_maps_to_category_else_trending(self, monkeypatch):
        from ficary.erotica import search as es

        seen_urls = []

        def fake_fetch(url):
            seen_urls.append(url)
            return '<a href="https://chyoa.com/story/tale.14">Tale</a>'

        monkeypatch.setattr(es, "_fetch", fake_fetch)
        es.search_chyoa("", tags=["hypnosis"])
        es.search_chyoa("", tags=["feet"])
        assert seen_urls[0] == "https://chyoa.com/category/mind-control"
        assert seen_urls[1] == "https://chyoa.com/trending-sex-stories"

    def test_darkwanderer_walks_authors_den_and_skips_meta(self, monkeypatch):
        from ficary.erotica import search as es

        seen_urls = []
        listing = (
            '<a href="/threads/faq-for-authors-guide.17305/">x</a>'
            '<a href="/threads/whiteboi-addiction.25942/">x</a>'
            '<a href="/threads/whiteboi-addiction.25942/post-9">x</a>'
        )

        def fake_fetch(url):
            seen_urls.append(url)
            return listing

        monkeypatch.setattr(es, "_fetch", fake_fetch)
        rows = es.search_darkwanderer("", page=2)
        assert seen_urls == [
            "https://darkwanderer.net/forums/authors-den.5/page-2",
        ]
        assert [r["title"] for r in rows] == ["Whiteboi Addiction"]
        sparse = es.search_darkwanderer("zzz-nope")
        assert sparse == [] and getattr(sparse, "more_available", False)

    def test_nifty_accepts_absolute_directory_hrefs(self, monkeypatch):
        from ficary.erotica import search as es

        listing = (
            '<a href="/nifty/gay/college/">College</a>'
            '<a href="relationships/">Relationships</a>'
            '<a href="/nifty/terms.html">Terms</a>'
        )
        monkeypatch.setattr(es, "_fetch", lambda url: listing)
        rows = es.search_nifty("", tags=["gay"])
        assert [r["title"] for r in rows] == ["College", "Relationships"]
        assert rows[0]["url"] == "https://www.nifty.org/nifty/gay/college/"


class TestNewSites2026_07:
    """The four sites added in 2026-07 (ReadOnlyMind, Giantess World,
    Chastity Mansion, TicklingForum) plus the bare-browse contract."""

    def test_rom_parse_story_id_forms(self):
        ref = "@Krungu5/SmallPackageBigPrize"
        assert ReadOnlyMindScraper.parse_story_id(
            f"https://readonlymind.com/{ref}/") == ref
        assert ReadOnlyMindScraper.parse_story_id(
            f"https://readonlymind.com/{ref}/2/") == ref
        assert ReadOnlyMindScraper.parse_story_id(ref) == ref
        with pytest.raises(ValueError):
            ReadOnlyMindScraper.parse_story_id("https://example.com/x")

    def test_gw_parse_story_id_forms(self):
        assert GiantessWorldScraper.parse_story_id(
            "https://giantessworld.net/viewstory.php?sid=11467") == 11467
        assert GiantessWorldScraper.parse_story_id(
            "https://giantessworld.net/viewstory.php?textsize=0&sid=7&chapter=2"
        ) == 7
        assert GiantessWorldScraper.parse_story_id("11467") == 11467
        with pytest.raises(ValueError):
            GiantessWorldScraper.parse_story_id("https://example.com/x")

    def test_xenforo_parse_story_id_forms(self):
        assert ChastityMansionScraper.parse_story_id(
            "https://chastitymansion.com/forums/index.php"
            "?threads/some-story.63479/") == 63479
        assert ChastityMansionScraper.parse_story_id(
            "https://chastitymansion.com/forums/threads/some-story.63479/"
        ) == 63479
        assert TicklingForumScraper.parse_story_id(
            "https://www.ticklingforum.com/threads/a-tale.42755/") == 42755
        with pytest.raises(ValueError):
            ChastityMansionScraper.parse_story_id(
                "https://www.ticklingforum.com/threads/a-tale.42755/")

    def test_xenforo_starter_posts_filter_and_quote_strip(self):
        from bs4 import BeautifulSoup

        html = """
        <article class="message" data-author="Author">
          <div class="bbWrapper">Chapter one.
            <blockquote>quoted reader text</blockquote></div>
        </article>
        <article class="message" data-author="Reader">
          <div class="bbWrapper">Great story!</div>
        </article>
        <article class="message" data-author="Author">
          <div class="bbWrapper">Chapter two.</div>
        </article>
        """
        soup = BeautifulSoup(html, "lxml")
        from ficary.erotica.xenforo import XenForoStoryScraper

        starter = XenForoStoryScraper._thread_starter_username(soup)
        assert starter == "Author"
        posts = XenForoStoryScraper._starter_posts(
            soup, starter, is_first_page=True,
        )
        assert len(posts) == 2
        joined = " ".join(posts)
        assert "Chapter one." in joined and "Chapter two." in joined
        assert "Great story!" not in joined
        assert "quoted reader text" not in joined

    def test_rom_search_card_parse(self, monkeypatch):
        from ficary.erotica import search as es

        card = (
            '<section class="story-card-large ">'
            '<div class="story-card-publication-date">2026-06-05</div>'
            '<div class="story-card-title" >'
            '<a href="/@Krungu5/SmallPackageBigPrize/">Small Package; Big Prize </a>'
            "</div>"
            '<div class="story-card-authors">by <a href="/@Krungu5/">Krungu5</a></div>'
            '<div class="story-card-word-count">(4,108 words)</div>'
            "</section>"
        )
        captured = {}

        def fake_fetch(url):
            captured["url"] = url
            return card

        monkeypatch.setattr(es, "_fetch", fake_fetch)
        rows = es.search_readonlymind("", tags=["foot-worship"])
        assert "%23footplay" in captured["url"]
        assert rows[0]["title"] == "Small Package; Big Prize"
        assert rows[0]["author"] == "Krungu5"
        assert rows[0]["updated"] == "2026-06-05"
        assert rows[0]["words"] == "4108"
        assert rows[0]["url"].endswith("/@Krungu5/SmallPackageBigPrize/")
        # Bare browse = empty q parameter.
        es.search_readonlymind("")
        assert captured["url"].startswith(
            "https://readonlymind.com/search/?q=&page=",
        )

    def test_gw_search_skips_chrome_links(self, monkeypatch):
        from ficary.erotica import search as es

        listing = (
            '<a href="viewstory.php?sid=1">Kink Island</a>'
            '<a href="viewstory.php?sid=1">Table of Contents</a>'
            '<a href="viewstory.php?sid=1">Report This</a>'
            '<a href="viewstory.php?sid=2">Second Story</a>'
        )
        monkeypatch.setattr(es, "_fetch", lambda url: listing)
        rows = es.search_giantessworld("")
        assert [r["title"] for r in rows] == ["Kink Island", "Second Story"]
        assert es.search_giantessworld("", tags=["bdsm"]) == []
        sparse = es.search_giantessworld("zz-nope")
        assert sparse == [] and getattr(sparse, "more_available", False)

    def test_cm_and_tmf_walk_their_story_forums(self, monkeypatch):
        from ficary.erotica import search as es

        seen = []
        cm_listing = (
            '<a href="/forums/index.php?threads/bradley-joness-chastity.50076/">x</a>'
        )
        tmf_listing = (
            '<a href="/threads/story-posting-rules.34534/">x</a>'
            '<a href="/threads/a-real-story.111/">x</a>'
        )

        def fake_fetch(url):
            seen.append(url)
            return cm_listing if "chastitymansion" in url else tmf_listing

        monkeypatch.setattr(es, "_fetch", fake_fetch)
        cm = es.search_chastitymansion("", page=2)
        assert seen[0] == (
            "https://chastitymansion.com/forums/index.php"
            "?forums/member-fiction.19/page-2"
        )
        assert [r["title"] for r in cm] == ["Bradley Joness Chastity"]
        assert cm[0]["url"] == (
            "https://chastitymansion.com/forums/index.php"
            "?threads/bradley-joness-chastity.50076/"
        )
        tmf = es.search_ticklingforum("")
        assert [r["title"] for r in tmf] == ["A Real Story"]

    def test_bare_browse_defaults(self, monkeypatch):
        """Every bare-browse default hits the intended listing URL."""
        from ficary.erotica import search as es

        seen = {}

        def fake_fetch(url):
            seen["url"] = url
            return ""

        monkeypatch.setattr(es, "_fetch", fake_fetch)
        es.search_mcstories("")
        assert seen["url"] == "https://mcstories.com/WhatsNew.html"
        es.search_lushstories("")
        assert seen["url"] == "https://www.lushstories.com/stories"
        es.search_wattpad_erotica("")
        assert seen["url"] == "https://www.wattpad.com/stories/adult"
        es.search_literotica_wrapped("")
        assert seen["url"] == "https://www.literotica.com/new"

    def test_ao3_bare_browse_proceeds(self, monkeypatch):
        from ficary.erotica import search as es

        calls = {}

        def fake_search_ao3(query, *, page=1, **kwargs):
            calls["query"] = query
            calls["kwargs"] = kwargs
            return [{"title": "x", "url": "u", "author": "a"}]

        import ficary.search as top_search
        monkeypatch.setattr(top_search, "search_ao3", fake_search_ao3)
        rows = es.search_ao3_erotica("")
        assert rows and rows[0]["site"] == "ao3"
        assert calls["kwargs"]["rating"] == "explicit"


def test_every_erotica_site_routes_to_the_adult_library_bucket():
    """Library categorisation: every erotica site's story URL must
    resolve through ``adapter_for_url`` to a name enumerated in
    ``ADULT_FICTION_ADAPTERS``. When either table misses a site, its
    downloads skip the Adult folder and land under Misc/fandom
    buckets — the 2026-07 bug where six sites (bdsmlibrary, mousepad,
    readonlymind, giantessworld, chastitymansion, ticklingforum) were
    absent from both tables."""
    from ficary.library.identifier import adapter_for_url
    from ficary.library.template import ADULT_FICTION_ADAPTERS

    sample_urls = {
        "aff": "https://hp.adult-fanfiction.org/story.php?no=600100488",
        "storiesonline": "https://storiesonline.net/s/40467/slug",
        "nifty": "https://www.nifty.org/nifty/gay/college/the-brotherhood/",
        "sexstories": "https://www.sexstories.com/story/114893/slug",
        "mcstories": "https://mcstories.com/AToZeb/",
        "lushstories":
            "https://www.lushstories.com/stories/cuckold/a-modern-relationship",
        "fictionmania":
            "https://fictionmania.tv/stories/readhtmlstory.html?storyID=1",
        "literotica": "https://www.literotica.com/s/my-story",
        "tgstorytime": "https://www.tgstorytime.com/viewstory.php?sid=9219",
        "chyoa": "https://chyoa.com/story/Insurance-Salesman-s.14",
        "darkwanderer": "https://darkwanderer.net/threads/foo.12345/",
        "greatfeet": "https://www.greatfeet.com/stories/ts1735.htm",
        "bdsmlibrary":
            "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
        "mousepad":
            "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=1",
        "readonlymind": "https://readonlymind.com/@A/Story/",
        "giantessworld": "https://giantessworld.net/viewstory.php?sid=1",
        "chastitymansion":
            "https://chastitymansion.com/forums/index.php?threads/a.63479/",
        "ticklingforum": "https://www.ticklingforum.com/threads/a.42755/",
    }
    # Parity with the scraper registry: one sample per erotica scraper.
    assert len(sample_urls) == len(EROTICA_SCRAPERS)
    for expected_name, url in sample_urls.items():
        name = adapter_for_url(url)
        assert name == expected_name, f"{url} identified as {name!r}"
        assert name in ADULT_FICTION_ADAPTERS, (
            f"{expected_name} missing from ADULT_FICTION_ADAPTERS"
        )
