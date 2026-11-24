"""A Fictionmania story number that does not exist.

The site answers 200 with an empty template of about 3.5 KB, which got
past the adapter's empty-page test, so Ficary saved a book called
"Fictionmania 123456" by "Unknown Author" whose only word was a menu
entry. The fixtures are the site's real answers for story 123456 and its
details page, saved 2026-11-24.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ficary.erotica.fictionmania import FictionmaniaScraper
from ficary.scraper import StoryNotFoundError

FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def scraper_answering(pages: dict) -> FictionmaniaScraper:
    scraper = FictionmaniaScraper(use_cache=False)
    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return pages[url]

    scraper._fetch = fetch
    scraper.fetched = fetched
    return scraper


def test_a_story_that_is_not_there_is_not_found_and_nothing_is_saved():
    probe = FictionmaniaScraper(use_cache=False)
    scraper = scraper_answering({
        probe._html_url(123456): (FIXTURES / "fm_missing_story.html").read_text(encoding="utf-8"),
        probe._details_url(123456): (FIXTURES / "fm_missing_details.html").read_text(encoding="utf-8"),
    })
    saved = []
    scraper._save_meta_cache = lambda *args, **kwargs: saved.append(args)
    with pytest.raises(StoryNotFoundError) as error:
        scraper.download("https://fictionmania.tv/stories/readhtmlstory.html?storyID=123456")
    assert "no story 123456" in str(error.value)
    assert saved == []


def layout_page() -> str:
    return (FIXTURES / "fm_story_2026_layout.html").read_text(encoding="utf-8")


def test_a_story_page_in_the_2026_layout_gives_the_story_and_nothing_around_it():
    """A made-up story laid out like the site's real pages: the menu, the
    title and author, links, the synopsis, the story, then review prompts,
    a copyright notice and a disclaimer."""
    probe = FictionmaniaScraper(use_cache=False)
    scraper = scraper_answering({probe._html_url(77): layout_page(), probe._details_url(77): "<html><body></body></html>"})
    scraper._fetch_with_text_fallback = lambda story_id: (layout_page(), False)
    story = scraper.download("https://fictionmania.tv/stories/readhtmlstory.html?storyID=77")
    [chapter] = story.chapters
    assert "Paragraph 12 of a story made up for this test" in chapter.html
    for around in ("Message Board", "New Today", "Synopsis:", "Please review", "Report Inappropriate", "copyrighted material", "disclaimer", "crossdress"):
        assert around not in chapter.html, around


def test_the_plain_text_page_keeps_its_pre():
    page = "<html><head><title>Fictionmania text story</title></head><body><pre>" + ("A line of the story.\n" * 40) + "</pre></body></html>"
    from bs4 import BeautifulSoup

    html = FictionmaniaScraper._parse_chapter_html(BeautifulSoup(page, "lxml"))
    assert html.startswith("<pre>") and html.endswith("</pre>")


def test_the_text_address_answering_with_a_web_page_is_read_as_one():
    """It answers text/html with the same page as the reader page.
    Escaped into a <pre>, its HTML source became the book."""
    probe = FictionmaniaScraper(use_cache=False)
    scraper = scraper_answering({
        probe._html_url(77): "<html><body></body></html>",
        probe._text_url(77): layout_page(),
    })
    html, from_text = scraper._fetch_with_text_fallback(77)
    assert from_text is True
    assert "&lt;div" not in html and "<div id=\"menu\">" in html


def test_a_short_story_beside_a_long_menu_is_still_the_story():
    """The menu is skipped by its id, not just outvoted by word count."""
    from bs4 import BeautifulSoup

    menu = "<div id=\"menu\"><dir>" + "".join(f"<li><a href=\"/{n}\">Menu entry number {n}</a></li>" for n in range(40)) + "</dir></div>"
    story = "<div>" + " ".join(f"word{n}" for n in range(60)) + "</div>"
    html = FictionmaniaScraper._parse_chapter_html(BeautifulSoup(f"<html><body>{menu}<h3>Title</h3>{story}</body></html>", "lxml"))
    assert "word59" in html
    assert "Menu entry" not in html


def test_a_chapter_cached_with_the_menu_is_read_again():
    """Chapters cached before the fix hold the whole page, the menu first.
    A download or an audiobook from the address would have used them."""
    from ficary.models import Chapter

    probe = FictionmaniaScraper(use_cache=False)
    scraper = scraper_answering({probe._details_url(77): "<html><body></body></html>"})
    scraper._fetch_with_text_fallback = lambda story_id: (layout_page(), False)
    stale = Chapter(number=1, title="A Made-Up Story", html='<div id="menu"><dir><li>Message Board</li></dir></div><div>old</div>')
    scraper._load_chapter_cache = lambda story_id, number: stale
    scraper._save_chapter_cache = lambda story_id, chapter: None
    story = scraper.download("https://fictionmania.tv/stories/readhtmlstory.html?storyID=77")
    assert "Message Board" not in story.chapters[0].html
    assert "Paragraph 12 of a story made up for this test" in story.chapters[0].html


def test_a_clean_cached_chapter_is_still_used():
    from ficary.models import Chapter

    probe = FictionmaniaScraper(use_cache=False)
    scraper = scraper_answering({probe._details_url(77): "<html><body></body></html>"})
    scraper._fetch_with_text_fallback = lambda story_id: (layout_page(), False)
    clean = Chapter(number=1, title="A Made-Up Story", html="<p>the cached story text</p>")
    scraper._load_chapter_cache = lambda story_id, number: clean
    story = scraper.download("https://fictionmania.tv/stories/readhtmlstory.html?storyID=77")
    assert story.chapters[0].html == "<p>the cached story text</p>"
