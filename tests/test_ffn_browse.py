"""Exercise FFN's real directory markup, native community forms, and filtered pagination without network access."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ficary import ffn_browse as browse
from ficary import search


FIXTURES = Path(__file__).parent / "fixtures"
COMMUNITY = "https://www.fanfiction.net/community/Excellent-Harry-Potter-Fanfiction/24898/"


def sample(name):
    return (FIXTURES / name).read_text()


def with_next_page(html, destination):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    links = soup.find_all("a", string="Next »")
    assert links, "The source fixture must contain a native Next link"
    for link in links:
        link["href"] = destination
    return str(soup)


@pytest.fixture(autouse=True)
def clear_fandom_ids():
    browse._fandom_id.cache_clear()
    yield
    browse._fandom_id.cache_clear()


def test_full_fandom_directory_preserves_names_and_counts(monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        return sample("ffn_fandom_directory.html")

    monkeypatch.setattr(search, "_fetch_search_page", fetch)
    rows = browse.search_catalog("", browse="categories", category="book", sort="stories")
    assert urls == ["https://www.fanfiction.net/book/"]
    assert rows.exhausted
    assert rows[0]["title"] == "Harry Potter"
    assert rows[0]["stories"] == 852000
    rare = browse.search_catalog("Knight", browse="categories", category="book", sort="name")
    assert len(rare) == 1
    assert rare[0]["slug"] == "A-Knight-s-Honor"
    assert rare[0]["title"] == "A Knight's Honor"
    assert rare[0]["stories"] == 32


def test_category_root_has_all_sections_and_general_communities():
    rows = browse.search_catalog("", browse="categories", sort="name")
    assert {row["category"] for row in rows} == set(browse.CATEGORIES)
    assert [row["title"] for row in rows] == sorted(row["title"] for row in rows)
    communities = browse.search_catalog("", browse="communities", sort="name")
    assert any(row["kind"] == "general" for row in communities)
    assert communities.exhausted


def test_community_fandom_directory_uses_community_counts(monkeypatch):
    html = '<title>Books Communities | FanFiction</title><a href="/communities/book/Harry-Potter/">Harry Potter</a> <span class="gray">(9,001)<br></span><a href="/communities/book/Twilight/">Twilight</a> <span class="gray">(500)</span>'
    urls = []
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or html)
    rows = browse.search_catalog("", browse="communities", category="book", sort="communities")
    assert rows[0]["communities"] == 9001
    assert rows[1]["communities"] == 500
    assert urls == ["https://www.fanfiction.net/communities/book/"]


def test_mobile_top_200_is_not_presented_as_full_directory():
    with pytest.raises(RuntimeError, match="incomplete mobile"):
        browse.parse_fandoms(sample("ffn_fandom_directory.html") + '<select name="l"><option>Top 200</option></select>', "book")


def test_native_community_directory_order_and_pagination(monkeypatch):
    urls = []
    html = with_next_page(sample("ffn_communities.html"), "/communities/book/Harry-Potter/1/2/3/")
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or html)
    rows = browse.search_catalog("", browse="communities", fandom="Harry Potter", language="english", sort="stories", page=2)
    assert urls == ["https://www.fanfiction.net/communities/book/Harry-Potter/1/2/2/"]
    assert not rows.exhausted
    assert rows[0]["title"] == "The PortkeyOrg C2"
    assert rows[0]["stories"] == 186
    assert rows[0]["followers"] == 5735
    assert rows[0]["created"] == "08-14-10"
    assert rows[1]["staff"] == 48
    assert rows[1]["founder"] == "Jan-AQ"
    assert "Archive: 186" in rows[0]["summary"]


def test_general_directory_is_not_a_fandom(monkeypatch):
    urls = []
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or sample("ffn_communities.html"))
    browse.search_catalog("", browse="communities", category="general", sort="newest", page=3)
    assert urls == ["https://www.fanfiction.net/communities/general/0/4/3/"]


def test_community_keyword_search_has_native_scope_and_age_sort(monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        if "/search/" not in url:
            return '<a href="/crossovers/Harry-Potter/224/">Crossovers</a>'
        return sample("ffn_community_search.html")

    monkeypatch.setattr(search, "_fetch_search_page", fetch)
    rows = browse.search_catalog("harry", browse="communities", fandom="Harry Potter", sort="newest", match="title", page=4)
    params = parse_qs(urlparse(urls[-1]).query)
    assert params == {"ready": ["1"], "type": ["community"], "keywords": ["harry"], "ppage": ["4"], "categoryid": ["224"], "sort": ["datecreated"], "match": ["title"]}
    assert rows[0]["title"] == "General Good Harry Fics"
    assert rows[0]["stories"] == 47
    assert rows[0]["followers"] == 13
    browse.search_catalog("harry", browse="communities", fandom="Harry Potter", sort="newest", match="title", page=5)
    assert len([url for url in urls if "/book/Harry-Potter/" in url]) == 1
    assert len(urls) == 3


def test_global_community_keyword_search_can_narrow_to_books(monkeypatch):
    urls = []
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or sample("ffn_community_search.html"))
    browse.search_catalog("time travel", browse="communities", category="book", sort="best match")
    params = parse_qs(urlparse(urls[0]).query)
    assert params["categoryid"] == ["202"]
    assert "sort" not in params


def test_browser_links_keep_exact_fandom_slug(monkeypatch):
    urls = []
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or sample("ffn_community_stories.html"))
    browse.search_catalog("", fandom="JAG", category="tv", fandom_label="JAG", fandom_path="/tv/JAG/", sort="favorites")
    assert urls == ["https://www.fanfiction.net/tv/JAG/?srt=4"]


@pytest.mark.parametrize("rating,expected", [("all", 99), ("K", 1), ("K+", 12), ("T", 13), ("M", 14), ("K-T", 3)])
def test_community_story_rating_ids_and_form_path(rating, expected):
    url = browse.community_story_url(COMMUNITY + "3/0/9/?p=6", {"rating": rating, "sort": "favorites", "genre": "adventure", "min_words": "10k+", "status": "complete", "time": "updated 1 week"}, 2)
    assert url == COMMUNITY + f"{expected}/4/2/6/10/2/2/"


def test_community_archive_date_sort_is_available():
    assert browse.community_story_url(COMMUNITY, {}, 3) == COMMUNITY + "99/0/3/0/0/0/0/"


def test_community_word_filter_keeps_the_requested_threshold(monkeypatch):
    urls = []
    html = with_next_page(sample("ffn_community_stories.html"), COMMUNITY + "99/0/2/0/100/0/0/")
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: urls.append(url) or html)
    rows = browse.search_catalog("", community=COMMUNITY, min_words="150k+")
    assert "/99/0/1/0/100/0/0/" in urls[0]
    assert [row["title"] for row in rows] == ["Escape"]
    assert not rows.exhausted


def test_filtered_empty_community_page_does_not_end_browse(monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        if "/99/1/1/" in url:
            return with_next_page(sample("ffn_community_stories.html"), COMMUNITY + "99/1/2/0/0/0/0/")
        return '<div class="z-list"><a class="stitle" href="/s/123/1/Find-Me">Find Me</a></div>'

    monkeypatch.setattr(search, "_fetch_search_page", fetch)
    rows, next_page = search.fetch_until_limit(browse.search_catalog, "Find Me", limit=25, community=COMMUNITY, sort="updated")
    assert len(urls) == 2
    assert len(rows) == 1
    assert rows[0]["title"] == "Find Me"
    assert rows.exhausted
    assert next_page == 3


def test_a_story_called_next_is_not_a_next_page(monkeypatch):
    html = '<div class="z-list"><a class="stitle" href="/s/123/1/Next">Next</a></div>'
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: html)
    rows = browse.search_catalog("", community=COMMUNITY)
    assert len(rows) == 1
    assert rows.exhausted


@pytest.mark.parametrize("query", ["", "next"])
def test_a_community_called_next_is_not_a_next_page(monkeypatch, query):
    html = '<div class="z-list"><a class="novtitle" href="/community/Next/123/">Next</a></div>'
    monkeypatch.setattr(search, "_fetch_search_page", lambda url: html)
    rows = browse.search_catalog(query, browse="communities", category="general", sort="newest")
    assert len(rows) == 1
    assert rows.exhausted


def test_unsupported_community_search_sort_is_not_silently_local():
    with pytest.raises(ValueError, match="best match or newest"):
        browse.search_catalog("harry", browse="communities", sort="followers")


@pytest.mark.parametrize("url", ["https://example.com/community/Name/123/", "https://www.fanfiction.net/s/123/1/Name"])
def test_community_url_requires_a_real_ffn_community(url):
    with pytest.raises(ValueError, match="community URL"):
        browse.community_story_url(url, {}, 1)
