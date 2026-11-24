"""SexStories put its own page furniture into the book.

The story's panel ends with the site's stats and vote list ("Read 60531
times | Rated 98.7 % | 156 votes", then every voter's name and
"POSITIVE") and a "login or register to rate this story" prompt, and the
readers' comments come in a panel of their own. The chapter parser joined
every panel, so all of it was in the book, and an audiobook read the
voters' names out. The fixture is the site's real page.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from ficary.erotica.sexstories import SexStoriesScraper

PAGE = (Path(__file__).parent / "fixtures" / "erotica" / "sexstories_story.html").read_text(encoding="utf-8", errors="replace")


def chapter_text() -> str:
    html = SexStoriesScraper._parse_chapter_html(BeautifulSoup(PAGE, "lxml"))
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_the_votes_rating_prompt_and_comments_are_not_in_the_book():
    text = chapter_text()
    for furniture in ("Vote list", "POSITIVE", "times | Rated", "register to rate", "0 comments"):
        assert furniture not in text, furniture


def test_the_introduction_and_the_whole_story_are():
    text = chapter_text()
    assert text.startswith("Last night, I had just gotten back home for the summer.")
    assert text.endswith("juliusincestus.com/product-category/family-reunion-series/")
    assert len(text.split()) > 4000


def test_no_rule_is_left_where_the_votes_were():
    html = SexStoriesScraper._parse_chapter_html(BeautifulSoup(PAGE, "lxml"))
    story = html.split("\n<hr />\n")[-1]   # the panels are joined with a rule
    assert "<hr" not in story[-400:]


def test_a_chapter_cached_with_the_votes_is_read_again():
    from ficary.models import Chapter

    scraper = SexStoriesScraper(use_cache=False)
    scraper._fetch = lambda url, *args, **kwargs: PAGE
    stale = Chapter(number=1, title="Old", html='<p>story</p><div class="story_info">Read 1 times</div>')
    scraper._load_chapter_cache = lambda story_id, number: stale
    scraper._save_chapter_cache = lambda story_id, chapter: None
    story = scraper.download("https://www.sexstories.com/story/114893/slug")
    assert "story_info" not in story.chapters[0].html
    assert "Last night, I had just gotten back home" in story.chapters[0].html


def test_a_clean_cached_chapter_is_still_used():
    from ficary.models import Chapter

    scraper = SexStoriesScraper(use_cache=False)
    scraper._fetch = lambda url, *args, **kwargs: PAGE
    scraper._load_chapter_cache = lambda story_id, number: Chapter(number=1, title="Kept", html="<p>cached text</p>")
    story = scraper.download("https://www.sexstories.com/story/114893/slug")
    assert story.chapters[0].html == "<p>cached text</p>"
