"""Which of a site's chapters a book already has, and where the others go, so an update adds only the chapters a book lacks and never drops one the site has taken down.

Matt's rule (2026-11-24): "It should figure out that a book has had chapters removed and not remove them from the downloaded copy, only add new ones." Updates went by position: the book's 404 chapters meant "the site's first 404". Once a site takes chapters down that stops being true, and a new chapter can sit at a position the book already covers. So chapters are matched instead: by the site's own id for the chapter where the book recorded it, and by title for books made before it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import Chapter


@dataclass(frozen=True)
class SiteChapter:
    """One chapter in a site's list: its place in the list (from 1), the site's id for it, and its title."""

    position: int
    site_id: str
    title: str


class CannotMatch(ValueError):
    """None of the book's chapters could be found on the site, so there is no telling which of the site's are new."""


@dataclass
class UpdatePlan:
    """What an update should download and where each chapter goes.

    ``new_positions`` are the site positions to download, in site order. ``insert_after`` maps each of them to the index in the book of the chapter it follows, -1 for before the first. ``book_site_ids`` gives the site id of each book chapter matched by title (book index to id), to record in the book so the next update can match by id. ``by`` is "id" or "title".
    """

    new_positions: list[int] = field(default_factory=list)
    insert_after: dict[int, int] = field(default_factory=dict)
    book_site_ids: dict[int, str] = field(default_factory=dict)
    by: str = "id"


def normalise_title(title: str) -> str:
    """A title as compared: case and runs of spaces don't count (a title comes back from a book with its spaces collapsed)."""
    return " ".join(str(title or "").split()).casefold()


def title_key(chapter: Chapter) -> str:
    """What a chapter is matched by when there is no site id: its title, or for a chapter read back from a book with no title (one titled just "Chapter 5" comes back that way), the heading the book wrote for it."""
    return normalise_title(chapter.title or chapter.heading)


def plan_update(book: list[Chapter], site: list[SiteChapter]) -> UpdatePlan:
    """Work out which of ``site``'s chapters ``book`` lacks.

    By id, when any of the book's chapters has the site's id: every site chapter whose id the book doesn't have is new, and goes after the book's copy of the site chapter before it, wherever that is in the book.

    By title otherwise. Only site chapters after the last one the book has count as new, and they go at the end: a title missing further back is far more likely a chapter the author renamed than a new one, and adding it would put the same chapter in twice. Raises CannotMatch when the book has chapters and none of them is on the site.
    """
    if not book:
        return UpdatePlan(
            new_positions=[chapter.position for chapter in site],
            insert_after={chapter.position: -1 for chapter in site},
            by="id",
        )
    if any(chapter.site_id for chapter in book):
        return _plan_by_id(book, site)
    return _plan_by_title(book, site)


def _plan_by_id(book: list[Chapter], site: list[SiteChapter]) -> UpdatePlan:
    book_index_by_id = {chapter.site_id: index for index, chapter in enumerate(book) if chapter.site_id}
    if not any(chapter.site_id in book_index_by_id for chapter in site if chapter.site_id):
        raise CannotMatch(
            "none of the book's chapters could be found on the site, so "
            "there is no telling which of the site's chapters are new"
        )
    plan = UpdatePlan(by="id")
    # A new chapter goes just before the book's copy of the next site
    # chapter the book has, or at the end when there is none. Not just
    # after the copy of the one before it: chapters the site has taken
    # down may follow that copy in the book, and they come first in the
    # story. Walked backwards to know the next one.
    next_index = len(book)
    after_by_position: dict[int, int] = {}
    for chapter in reversed(site):
        if chapter.site_id and chapter.site_id in book_index_by_id:
            next_index = book_index_by_id[chapter.site_id]
        else:
            after_by_position[chapter.position] = next_index - 1
    for chapter in site:
        if chapter.position in after_by_position:
            plan.new_positions.append(chapter.position)
            plan.insert_after[chapter.position] = after_by_position[chapter.position]
    return plan


def _plan_by_title(book: list[Chapter], site: list[SiteChapter]) -> UpdatePlan:
    # Each book chapter still waiting to be matched, by title, in book
    # order, so a title used twice matches in turn.
    waiting: dict[str, list[int]] = {}
    for index, chapter in enumerate(book):
        waiting.setdefault(title_key(chapter), []).append(index)
    plan = UpdatePlan(by="title")
    last_match_position = 0
    for chapter in site:
        indexes = waiting.get(normalise_title(chapter.title))
        if indexes:
            index = indexes.pop(0)
            last_match_position = chapter.position
            if chapter.site_id:
                plan.book_site_ids[index] = chapter.site_id
    if last_match_position == 0:
        raise CannotMatch(
            "none of the book's chapters could be found on the site, so "
            "there is no telling which of the site's chapters are new"
        )
    # Every site chapter after the last one the book has is new: had any
    # of them matched, the last match would be later.
    last_index = len(book) - 1
    for chapter in site:
        if chapter.position > last_match_position:
            plan.new_positions.append(chapter.position)
            plan.insert_after[chapter.position] = last_index
    return plan


def apply_plan(book: list[Chapter], new_chapters: dict[int, Chapter], plan: UpdatePlan) -> list[Chapter]:
    """The book with ``new_chapters`` (by site position) put in where ``plan`` says, every chapter the book had kept, and the chapters numbered 1 onwards. Book chapters matched by title get the site's id for them."""
    following: dict[int, list[Chapter]] = {}
    for position in plan.new_positions:
        chapter = new_chapters.get(position)
        if chapter is None:
            raise ValueError(f"the site's chapter {position} did not arrive")
        following.setdefault(plan.insert_after[position], []).append(chapter)
    result: list[Chapter] = list(following.get(-1, []))
    for index, chapter in enumerate(book):
        site_id = plan.book_site_ids.get(index)
        result.append(replace(chapter, site_id=site_id) if site_id and not chapter.site_id else chapter)
        result.extend(following.get(index, []))
    return _numbered(result)


def keep_taken_down(book: list[Chapter], fresh: list[Chapter]) -> list[Chapter]:
    """For a fresh copy: every chapter downloaded again, plus the book's chapters the site no longer has, each kept after the chapter it followed in the book.

    Raises CannotMatch when none of the book's chapters is among the fresh ones: then the titles have changed (an older version wrote them differently, or the author renamed them all), not been taken down, and keeping them all would put the whole story in twice.
    """
    fresh_ids = {chapter.site_id for chapter in fresh if chapter.site_id}
    fresh_titles = {title_key(chapter) for chapter in fresh}

    def on_the_site(chapter: Chapter) -> bool:
        if chapter.site_id:
            return chapter.site_id in fresh_ids
        return title_key(chapter) in fresh_titles

    if book and fresh and not any(on_the_site(chapter) for chapter in book):
        raise CannotMatch(
            "none of the book's chapters could be found among the site's, "
            "so a fresh copy would have put the whole story in twice"
        )

    result = list(fresh)
    # Where in ``result`` the copy of each book chapter sits.
    def place_of(chapter: Chapter) -> int | None:
        for place, candidate in enumerate(result):
            if chapter.site_id and candidate.site_id == chapter.site_id:
                return place
            if not chapter.site_id and title_key(candidate) == title_key(chapter):
                return place
        return None

    previous_place = -1
    for chapter in book:
        if on_the_site(chapter):
            place = place_of(chapter)
            if place is not None:
                previous_place = place
            continue
        previous_place += 1
        result.insert(previous_place, chapter)
    return _numbered(result)


def dropped(old: list[Chapter], new: list[Chapter]) -> list[Chapter]:
    """The chapters of ``old`` that ``new`` lacks: matched by site id where both have one, otherwise by title. Checked before an update writes a book, which must never lose a chapter."""
    new_ids = {chapter.site_id for chapter in new if chapter.site_id}
    new_titles: dict[str, int] = {}
    for chapter in new:
        new_titles[title_key(chapter)] = new_titles.get(title_key(chapter), 0) + 1
    missing = []
    for chapter in old:
        if chapter.site_id and chapter.site_id in new_ids:
            continue
        title = title_key(chapter)
        if new_titles.get(title, 0) > 0:
            new_titles[title] -= 1
            continue
        missing.append(chapter)
    return missing


def _numbered(chapters: list[Chapter]) -> list[Chapter]:
    return [replace(chapter, number=number) for number, chapter in enumerate(chapters, 1)]
