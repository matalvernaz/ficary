"""Literotica scraper tests."""

from pathlib import Path

from bs4 import BeautifulSoup

from ficary.erotica.literotica import LiteroticaScraper, _slug_to_id

FIXTURES = Path(__file__).parent / "fixtures"


def _story_soup():
    return BeautifulSoup(
        (FIXTURES / "literotica_story.html").read_text(encoding="utf-8"),
        "lxml",
    )


def _series_soup():
    return BeautifulSoup(
        (FIXTURES / "literotica_series.html").read_text(encoding="utf-8"),
        "lxml",
    )


class TestURLParsing:
    def test_parses_canonical_url(self):
        assert (
            LiteroticaScraper.parse_story_id(
                "https://www.literotica.com/s/my-story-title"
            )
            == "my-story-title"
        )

    def test_parses_bare_slug(self):
        assert LiteroticaScraper.parse_story_id("my-story-title") == "my-story-title"

    def test_rejects_bad_url(self):
        import pytest
        with pytest.raises(ValueError):
            LiteroticaScraper.parse_story_id("https://example.com/not-literotica")

    def test_is_author_url(self):
        assert LiteroticaScraper.is_author_url(
            "https://www.literotica.com/authors/SomeAuthor"
        )
        assert LiteroticaScraper.is_author_url(
            "https://www.literotica.com/authors/SomeAuthor/works/stories"
        )
        assert not LiteroticaScraper.is_author_url(
            "https://www.literotica.com/s/story-slug"
        )

    def test_is_series_url(self):
        assert LiteroticaScraper.is_series_url(
            "https://www.literotica.com/series/se/12345"
        )
        assert not LiteroticaScraper.is_series_url(
            "https://www.literotica.com/s/story"
        )


class TestSlugHashing:
    def test_stable_across_runs(self):
        assert _slug_to_id("same-slug") == _slug_to_id("same-slug")

    def test_different_slugs_differ(self):
        assert _slug_to_id("slug-one") != _slug_to_id("slug-two")

    def test_returns_int(self):
        assert isinstance(_slug_to_id("my-slug"), int)
        assert _slug_to_id("my-slug") > 0


class TestMetadataAndContent:
    def test_page_count(self):
        soup = _story_soup()
        # Fixture is a 3-page story; pagination links reference 2 and 3
        assert LiteroticaScraper._page_count(soup) == 3

    def test_metadata_extracts_title_and_author(self):
        scraper = LiteroticaScraper(use_cache=False)
        meta = scraper._parse_metadata(_story_soup(), "stop-toying-with-me-miss-yamanaka")
        assert meta["title"] == "Stop Toying With Me, Miss Yamanaka"
        assert meta["author"] == "Duleigh"
        assert meta["num_pages"] == 3

    def test_content_div_is_locatable(self):
        soup = _story_soup()
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        # At least a few hundred chars of text
        assert len(body.get_text(strip=True)) > 200


class TestContentDivFallbacks:
    """Exercise the three-layer selector chain in ``_content_div``.

    Literotica's CSS-module class names rebuild per release; these
    tests pin each structural fallback so a future rebuild that
    invalidates the hash prefix still finds the body through
    ``itemprop`` / ``itemtype`` microdata."""

    def test_itemprop_articlebody_wins_even_without_css_module(self):
        html = (
            '<html><body><main>'
            '<div itemprop="articleBody" class="totally_unrelated">'
            '<p>body</p>'
            '</div></main></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "body" in body.get_text()

    def test_css_module_prefix_matches_when_itemprop_absent(self):
        html = (
            '<html><body>'
            '<div class="_article__content_FUTURE_HASH_xyz"><p>body</p></div>'
            '</body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "body" in body.get_text()

    def test_article_itemtype_fallback(self):
        html = (
            '<html><body>'
            '<article itemtype="https://schema.org/Article">'
            '<p>body</p>'
            '</article></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert body.name == "article"

    def test_returns_none_when_no_marker(self):
        html = (
            '<html><body><p>no story markers here</p></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        assert LiteroticaScraper._content_div(soup) is None

    def test_itemprop_preferred_over_other_matches(self):
        # If both a CSS-module hash and an itemprop element exist, the
        # itemprop wins so we track the stable contract, not the hash.
        html = (
            '<html><body>'
            '<div class="_article__content_STALE_HASH">'
            '<p>stale content</p></div>'
            '<div itemprop="articleBody" class="_article__content_FRESH">'
            '<p>fresh content</p></div>'
            '</body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "fresh" in body.get_text()


class TestSeriesExtraction:
    def test_series_works_parsed_from_fixture(self):
        import re
        soup = _series_soup()
        seen = set()
        count = 0
        for a in soup.find_all("a", href=True):
            m = re.search(r"literotica\.com/s/([a-z0-9-]+)", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                count += 1
        # Series fixture for /series/se/100 (Ruth) has 3 chapters
        assert count >= 3


class TestParseTagBrowseResults:
    """:func:`_parse_literotica_results` against the post-Next.js
    markup. The live tag-browse page wraps each card in
    ``<div role="article">`` and uses a title anchor with
    ``rel="external"`` + a ``literotica.com/s/<slug>`` href. The
    previous parser keyed off schema.org ``property="itemListElement"``
    selectors that Literotica's React rewrite removed."""

    @staticmethod
    def _card_html(slug, title, author, summary, category, rating):
        return f"""
        <div role="article" class="_works_item_rand_4">
          <p class="_works_item__title_rand_50" role="heading" aria-level="3">
            <a href="https://www.literotica.com/s/{slug}" rel="external"
               class="_item_title_rand_227">{title}</a>
            <a href="/{slug}?dialog=log_in" class="_headline__link_rand_478"
               title="Bookmark Story" aria-label="Bookmark Story: {title}">
              <span class="visually-hidden">Bookmark Story</span>
            </a>
          </p>
          <p class="_item_description_rand_256">{summary}</p>
          <div class="_item_metadata_rand_492">
            <span class="_item_by_rand_502">by</span>
            <a href="https://www.literotica.com/authors/{author}/works/stories"
               class="_item_authorname_link_rand_511">{author}</a>
            <span class="_item_in_rand_556">in</span>
            <a href="https://www.literotica.com/c/{category.lower().replace(' ', '-')}"
               rel="external" class="_item_category_rand_92">{category}</a>
            <time datetime="2026-05-22">05/22/2026</time>
          </div>
          <div class="_stats_wrapper_rand_468">
            <span data-value="{rating}" title="Rating">
              <span class="_stats__text_rand_209">{rating}</span>
            </span>
          </div>
        </div>
        """

    def test_parses_modern_card_markup(self):
        from ficary.search import _parse_literotica_results
        html = "<html><body>" + "".join([
            self._card_html(
                "first-card", "First Card", "AuthorOne",
                "A summary line.", "BDSM", "4.75",
            ),
            self._card_html(
                "second-card", "Second Card", "AuthorTwo",
                "Another summary.", "Novels and Novellas", "4.20",
            ),
        ]) + "</body></html>"
        results = _parse_literotica_results(html)
        assert len(results) == 2
        first = results[0]
        assert first["title"] == "First Card"
        assert first["url"] == "https://www.literotica.com/s/first-card"
        assert first["author"] == "AuthorOne"
        assert first["summary"] == "A summary line."
        assert first["fandom"] == "BDSM"
        assert first["rating"] == "4.75"

    def test_ignores_nav_and_bookmark_links_sharing_href_shapes(self):
        """Pinning the title selector to ``rel="external"`` plus an
        anchored ``literotica.com/s/<slug>`` URL keeps tag-page chrome
        (bookmark / login dialog / category nav) from being mistaken
        for cards."""
        from ficary.search import _parse_literotica_results
        # No rel="external" → not a card. Path-only links and series
        # links → not a card either.
        html = """
        <html><body>
          <a href="https://www.literotica.com/s/should-not-show">No rel attr</a>
          <a href="/femdom?dialog=log_in" rel="external">Login</a>
          <a href="https://www.literotica.com/series/se/12345" rel="external">Series</a>
          <a href="https://www.literotica.com/c/bdsm" rel="external">Category</a>
        </body></html>
        """
        assert _parse_literotica_results(html) == []

    def test_dedupes_repeat_card_anchors(self):
        from ficary.search import _parse_literotica_results
        html = (
            "<html><body>"
            + self._card_html("same", "Same Card", "A", "summary", "BDSM", "5")
            + self._card_html("same", "Same Card", "A", "summary", "BDSM", "5")
            + "</body></html>"
        )
        results = _parse_literotica_results(html)
        assert len(results) == 1


class TestParseTagBrowseArticleMarkup:
    """:func:`_parse_literotica_results` against the 2026-07 build.

    Literotica rotated its front-end again: cards became ``<article>``
    elements (no ``role`` attribute), title anchors moved inside an
    ``<h3>`` and flipped ``rel="external"`` to ``rel="_self"``, and
    author hrefs grew a ``/stories`` suffix. The previous parser keyed
    on ``rel="external"`` + ``div[role=article]`` and returned zero
    rows for every live tag page."""

    @staticmethod
    def _card_html(slug, title, author, summary, category, rating):
        return f"""
        <article class="_card_1gpbw_15">
          <div class="_content_1gpbw_55">
            <h3 class="_title_1gpbw_51">
              <a href="https://www.literotica.com/s/{slug}" rel="_self"
                 class="_title_link_1gpbw_66">{title}</a>
            </h3>
            <p class="_description_1gpbw_95">{summary}</p>
            <div class="_meta_row_1gpbw_599">
              <span class="_by_1gpbw_117">by</span>
              <a href="https://www.literotica.com/authors/{author}/works/stories"
                 class="_author_link_1gpbw_137">{author}</a>
              <span class="_in_1gpbw_160">in</span>
              <a href="https://www.literotica.com/c/{category.lower().replace(' ', '-')}"
                 rel="_self" class="_category_1gpbw_138">{category}</a>
              <time datetime="2026-07-07">07/07/2026</time>
            </div>
          </div>
          <a href="/femdom/?dialog=log-in" rel="nofollow"
             title="Bookmark Story"
             aria-label="Bookmark Story: {title}">
            <span class="visually-hidden">Bookmark Story</span>
          </a>
          <span class="_stat_1gpbw_199" data-value="{rating}" title="Rating">
            <span class="_stats_text_1gpbw_258">{rating}</span>
          </span>
        </article>
        """

    def test_parses_article_card_markup(self):
        from ficary.search import _parse_literotica_results
        html = "<html><body>" + "".join([
            self._card_html(
                "her-lesbian-awakening", "Her Lesbian Awakening",
                "lickablelucy23", "When a girl sits on her face.",
                "Lesbian Sex", "4.5",
            ),
            self._card_html(
                "sam-and-jenny-ch-38", "Sam and Jenny Ch. 38",
                "MASEVEN", "TPE FemDom slice of life.", "BDSM", "5",
            ),
        ]) + "</body></html>"
        results = _parse_literotica_results(html)
        assert len(results) == 2
        first = results[0]
        assert first["title"] == "Her Lesbian Awakening"
        assert first["url"] == (
            "https://www.literotica.com/s/her-lesbian-awakening"
        )
        assert first["author"] == "lickablelucy23"
        assert first["summary"] == "When a girl sits on her face."
        assert first["fandom"] == "Lesbian Sex"
        assert first["rating"] == "4.5"

    def test_heading_anchor_without_rel_still_counts_as_title(self):
        """``rel`` has flipped value once already (external → _self);
        if it disappears entirely, a permalink inside a heading must
        still be recognised as a card title."""
        from ficary.search import _parse_literotica_results
        html = """
        <html><body><article>
          <h3><a href="https://www.literotica.com/s/no-rel-card">No Rel Card</a></h3>
        </article></body></html>
        """
        results = _parse_literotica_results(html)
        assert len(results) == 1
        assert results[0]["title"] == "No Rel Card"

    def test_bare_permalink_outside_heading_still_ignored(self):
        """Page chrome sharing the permalink shape (related-story
        links, footers) has neither a title ``rel`` nor a heading
        ancestor and must not become a result row."""
        from ficary.search import _parse_literotica_results
        html = """
        <html><body>
          <div><a href="https://www.literotica.com/s/chrome-link">Chrome</a></div>
        </body></html>
        """
        assert _parse_literotica_results(html) == []


class TestSubmissionIsOneChapter:
    """The fix for pages-as-chapters: a submission's ?page=N splits are
    length breaks, so downloads must emit exactly one merged chapter."""

    def _scraper_with_fixture(self, monkeypatch):
        from ficary.erotica.literotica import LiteroticaScraper

        html = (FIXTURES / "literotica_story.html").read_text(
            encoding="utf-8",
        )
        scraper = LiteroticaScraper(
            use_cache=False, delay_floor=0.0, delay_start=0.0,
        )
        fetched = []

        def fake_fetch_page(slug, page_num):
            fetched.append(page_num)
            return html

        monkeypatch.setattr(scraper, "_fetch_page", fake_fetch_page)
        return scraper, fetched

    def test_multi_page_story_merges_to_single_chapter(self, monkeypatch):
        scraper, fetched = self._scraper_with_fixture(monkeypatch)
        story = scraper.download("stop-toying-with-me-miss-yamanaka")
        # Fixture reports 3 pages; all must be fetched, one chapter out.
        assert fetched == [1, 2, 3]
        assert len(story.chapters) == 1
        ch = story.chapters[0]
        assert ch.number == 1
        assert "Page" not in ch.title
        assert ch.title == story.title

    def test_get_chapter_count_is_always_one_and_offline(self, monkeypatch):
        from ficary.erotica.literotica import LiteroticaScraper

        scraper = LiteroticaScraper(use_cache=False)

        def boom(*a, **kw):
            raise AssertionError("get_chapter_count must not fetch")

        monkeypatch.setattr(scraper, "_fetch", boom)
        assert scraper.get_chapter_count(
            "https://www.literotica.com/s/some-story",
        ) == 1

    def test_skip_chapters_skips_the_whole_submission(self, monkeypatch):
        scraper, fetched = self._scraper_with_fixture(monkeypatch)
        story = scraper.download(
            "stop-toying-with-me-miss-yamanaka", skip_chapters=1,
        )
        assert story.chapters == []
        assert fetched == [1]  # metadata page only, no page walk
