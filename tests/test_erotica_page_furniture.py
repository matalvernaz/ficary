"""Site furniture that ended up in books, found by reading the first and
last words of a book made from each adult site's saved page.

* GreatFeet ended every book with its navigation: "Back to the Story
  Gallery" and "Great Feet Foot Fetish Menu".
* StoriesOnline kept a line the site hides, "The source of this story is
  Storiesonline", which a screen reader or an audiobook reads out. And to
  someone not signed in it shows only the start of the story, ending
  "There is more of this story...": the book stopped there with nothing
  said at download time.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ficary.erotica import GreatFeetScraper, StoriesOnlineScraper

FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_greatfeet_books_end_with_the_story_not_the_menu():
    page = (FIXTURES / "greatfeet_story.html").read_text(encoding="utf-8", errors="replace")
    text = text_of(GreatFeetScraper._parse_chapter_html(BeautifulSoup(page, "lxml")))
    assert "Back to the Story Gallery" not in text
    assert "Foot Fetish Menu" not in text
    assert text.endswith("having our feet worshiped.")


def test_a_greatfeet_paragraph_with_a_link_in_its_prose_is_kept():
    page = (
        "<html><body><hr><p>" + "A long paragraph of story text that mentions " * 3
        + '<a href="http://www.greatfeet.com/x.htm">a link</a> in passing.</p></body></html>'
    )
    text = text_of(GreatFeetScraper._parse_chapter_html(BeautifulSoup(page, "lxml")))
    assert "a link in passing." in text


def sol_page() -> str:
    return (FIXTURES / "sol_story.html").read_text(encoding="utf-8", errors="replace")


def test_storiesonline_leaves_out_the_line_it_hides():
    text = text_of(StoriesOnlineScraper._parse_chapter_html(BeautifulSoup(sol_page(), "lxml")))
    assert "The source of this story is Storiesonline" not in text
    assert "There is more of this story" in text


def test_storiesonline_says_when_the_book_is_only_the_free_part(caplog):
    scraper = StoriesOnlineScraper(use_cache=False)
    scraper._fetch = lambda url, *args, **kwargs: sol_page()
    with caplog.at_level(logging.WARNING, logger="ficary.erotica.storiesonline"):
        scraper.download("https://storiesonline.net/s/40467/ouroboros-dorm-dipping")
    said = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("shows only the start of" in line and "Ficary cannot sign in to StoriesOnline" in line for line in said)


def test_a_whole_storiesonline_story_says_nothing(caplog):
    page = sol_page()
    cut = page.index('<div class="sep">')
    whole = page[:cut] + page[page.index("</article>"):]
    scraper = StoriesOnlineScraper(use_cache=False)
    scraper._fetch = lambda url, *args, **kwargs: whole
    with caplog.at_level(logging.WARNING, logger="ficary.erotica.storiesonline"):
        scraper.download("https://storiesonline.net/s/40467/ouroboros-dorm-dipping")
    assert not any("shows only the start of" in record.getMessage() for record in caplog.records)
