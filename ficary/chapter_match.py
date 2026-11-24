"""Which of a site's chapters a book already has, and where the others go, so an update adds only the chapters a book lacks and never drops one the site has taken down.

Matt's rule (2026-11-24): "It should figure out that a book has had chapters removed and not remove them from the downloaded copy, only add new ones." Updates went by position: the book's 404 chapters meant "the site's first 404". Once a site takes chapters down that stops being true, and a new chapter can sit at a position the book already covers. So chapters are matched instead, by the strongest evidence there is:

- the site's own id for the chapter, where the book recorded it;
- the title, for books made before they did;
- the place, between two matched chapters and after the last, for renamed chapters and for chapters whose titles say nothing about them (blank, a bare number, or "Chapter N", which several sites make up from a chapter's place, so once a chapter is taken down it names a different one).

Where the evidence runs out, an update adds nothing it can't place and a fresh copy refuses rather than guess: a doubled chapter is as wrong as a lost one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from itertools import chain

from .models import Chapter


@dataclass(frozen=True)
class SiteChapter:
    """One chapter in a site's list: its place in the list (from 1), the site's id for it, and its title."""

    position: int
    site_id: str
    title: str


class CannotMatch(ValueError):
    """The book's chapters can't be told apart on the site, so there is no telling which of the site's are new, or which of the book's it took down."""


@dataclass
class UpdatePlan:
    """What an update should download and where each chapter goes.

    ``new_positions`` are the site positions to download, in site order. ``insert_after`` maps each of them to the index in the book of the chapter it follows, -1 for before the first. ``book_site_ids`` gives the site id of each book chapter matched without one (book index to id), to record in the book so the next update can match by id. ``by`` is "id" or "title".
    """

    new_positions: list[int] = field(default_factory=list)
    insert_after: dict[int, int] = field(default_factory=dict)
    book_site_ids: dict[int, str] = field(default_factory=dict)
    by: str = "id"


def normalise_title(title: str) -> str:
    """A title as compared: case and runs of spaces don't count (a title comes back from a book with its spaces collapsed)."""
    return " ".join(str(title or "").split()).casefold()


_BARE_NUMBER_RE = re.compile(r"\d+\s*[.\-:)]*")
_CHAPTER_NUMBER_RE = re.compile(r"chapter\s*\d+\s*[.\-:)]*")
_NUMBER_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class _Item:
    """One chapter as matching sees it: the site's id for it, the normalised titles it can be matched by, the one compared for a rename, and whether its title says nothing about it, so that only its place can match it."""

    site_id: str
    keys: frozenset
    name: str
    says_nothing: bool


def _says_nothing(text: str, titles_from_author: bool) -> bool:
    """Whether a normalised title says nothing about its chapter: blank; a bare number (a book writes it as "Chapter N" with its own count, which the chapters before it can put out by one); or, unless the titles are the author's own, "Chapter N", which several sites make up from a chapter's place."""
    if not text or _BARE_NUMBER_RE.fullmatch(text):
        return True
    return not titles_from_author and bool(_CHAPTER_NUMBER_RE.fullmatch(text))


def _site_item(title: str, site_id: str, titles_from_author: bool) -> _Item:
    """A chapter from the site, or one just downloaded from it."""
    text = normalise_title(title)
    says_nothing = _says_nothing(text, titles_from_author)
    return _Item(site_id, frozenset() if says_nothing else frozenset({text}), text, says_nothing)


def _book_item(chapter: Chapter, titles_from_author: bool, site_has_untitled: bool) -> _Item:
    """A chapter read back from a book, matched by its title and by the heading the book wrote for it: "Chapter 3. Foo" reads back as "Foo", and a chapter whose heading is just "Chapter N" reads back with no title. That heading is the author's own "Chapter N" on a site whose titles are the author's; but it is also what a book writes for a blank or bare-number title, so when the site has titles like those it is matched by its place."""
    title = normalise_title(chapter.title)
    heading = normalise_title(chapter.heading)
    keys = frozenset(key for key in (title, heading) if key)
    numbered_only = not title or bool(_BARE_NUMBER_RE.fullmatch(title) or _CHAPTER_NUMBER_RE.fullmatch(title))
    says_nothing = numbered_only and (site_has_untitled or not titles_from_author)
    return _Item(chapter.site_id, keys, title or heading, says_nothing)


def _looks_renamed(first: _Item, second: _Item) -> bool:
    """Whether two chapters' titles look like one chapter renamed: the same chapter numbers in both ("Chapter 404" and "404. The Fall"); one title starting or ending the other, its numbers too ("The Fall" and "Chapter 404 - The Fall"); or, numbers aside, the two nearly the same ("The Fal" and "The Fall")."""
    a, b = first.name, second.name
    if not a or not b:
        return False
    numbers_a, numbers_b = _NUMBER_RE.findall(a), _NUMBER_RE.findall(b)
    if numbers_a and numbers_a == numbers_b:
        return True
    shorter, longer = sorted((a, b), key=len)
    numbers_short, numbers_long = _NUMBER_RE.findall(shorter), _NUMBER_RE.findall(longer)
    if len(shorter) >= 4:
        if longer.startswith(shorter) and numbers_long[: len(numbers_short)] == numbers_short:
            return True
        if longer.endswith(shorter) and numbers_long[len(numbers_long) - len(numbers_short):] == numbers_short:
            return True
    return numbers_a == numbers_b and SequenceMatcher(None, a, b).ratio() >= 0.85


def _align(book: list[_Item], other: list[_Item]) -> dict[int, int]:
    """Which of the book's chapters each of the other list's is, as other index -> book index.

    1. The same site id.
    2. The same title, for book chapters without an id, looking first after the book's previous match, so repeated titles ("Interlude") pair in order.
    3. Between two matched chapters: runs the same length on both sides pair in order (renamed chapters, and chapters whose titles say nothing); runs of different lengths pair only titles that look renamed, from each end.
    4. After the last matched chapter, in order: chapters whose titles say nothing, and titles that look renamed. A book chapter there with a title of its own that the other list lacks was taken down; one in the other list with a title the book lacks is new.

    A book chapter with an id the other list doesn't have was taken down, and is never matched by title or place, unless the other list has no ids to go by.
    """
    pairs: dict[int, int] = {}
    used: set[int] = set()
    other_has_ids = any(item.site_id for item in other)

    def free(index: int) -> bool:
        return index not in used and not (other_has_ids and book[index].site_id)

    def pair(position: int, index: int) -> None:
        pairs[position] = index
        used.add(index)

    by_id: dict[str, int] = {}
    for index, item in enumerate(book):
        if item.site_id:
            by_id.setdefault(item.site_id, index)
    for position, item in enumerate(other):
        index = by_id.get(item.site_id) if item.site_id else None
        if index is not None and index not in used:
            pair(position, index)

    previous = -1
    for position, item in enumerate(other):
        if position in pairs:
            previous = pairs[position]
            continue
        if not item.keys:
            continue
        for index in chain(range(previous + 1, len(book)), range(previous + 1)):
            if free(index) and item.keys & book[index].keys:
                pair(position, index)
                previous = index
                break

    def between(start: tuple[int, int], end: tuple[int, int]) -> None:
        others = [position for position in range(start[0] + 1, end[0]) if position not in pairs]
        books = [index for index in range(start[1] + 1, end[1]) if free(index)]
        if not others or not books:
            return
        if len(others) == len(books):
            for position, index in zip(others, books):
                pair(position, index)
            return
        while others and books and _looks_renamed(book[books[0]], other[others[0]]):
            pair(others.pop(0), books.pop(0))
        while others and books and _looks_renamed(book[books[-1]], other[others[-1]]):
            pair(others.pop(), books.pop())

    start = (-1, -1)
    for anchor in sorted(pairs.items()):
        if anchor[1] > start[1]:
            between(start, anchor)
        start = anchor

    last_position = max(pairs, default=-1)
    last_index = max(pairs.values(), default=-1)
    others = list(range(last_position + 1, len(other)))
    books = [index for index in range(last_index + 1, len(book)) if free(index)]
    in_book = in_other = 0
    while in_book < len(books) and in_other < len(others):
        book_item, other_item = book[books[in_book]], other[others[in_other]]
        if (book_item.says_nothing and other_item.says_nothing) or _looks_renamed(book_item, other_item):
            pair(others[in_other], books[in_book])
            in_book += 1
            in_other += 1
        elif not book_item.says_nothing:
            in_book += 1
        else:
            in_other += 1
    return pairs


def _match(book: list[Chapter], others: list, titles_from_author: bool) -> tuple[dict[int, int], list[_Item]]:
    """:func:`_align` for a book's chapters and a list of SiteChapters or Chapters; with the book's items, for what they say about the chapters left over."""
    other_items = [_site_item(chapter.title, chapter.site_id, titles_from_author) for chapter in others]
    site_has_untitled = any(item.says_nothing for item in other_items)
    book_items = [_book_item(chapter, titles_from_author, site_has_untitled) for chapter in book]
    return _align(book_items, other_items), book_items


def plan_update(book: list[Chapter], site: list[SiteChapter]) -> UpdatePlan:
    """Work out which of ``site``'s chapters ``book`` lacks, and where each goes. The site's titles are the author's own: it is a site that lists its chapters.

    Site chapters after the last one the book has are new, and go at the end, after any chapters the site has taken down. One further back is new only where the book has nothing unmatched between the chapters either side of it that it could be; it goes just before the book's copy of the next site chapter the book has. Raises CannotMatch when the book has chapters and none of them can be found on the site.
    """
    if not book:
        return UpdatePlan(
            new_positions=[chapter.position for chapter in site],
            insert_after={chapter.position: -1 for chapter in site},
            by="id",
        )
    pairs, _book_items = _match(book, site, titles_from_author=True)
    if not pairs:
        raise CannotMatch(
            "none of the book's chapters could be found on the site, so "
            "there is no telling which of the site's chapters are new"
        )
    plan = UpdatePlan(by="id" if any(chapter.site_id for chapter in book) else "title")
    for position, index in pairs.items():
        if site[position].site_id and not book[index].site_id:
            plan.book_site_ids[index] = site[position].site_id

    site_has_ids = any(chapter.site_id for chapter in site)
    matched = set(pairs.values())
    unmatched = [index not in matched and not (site_has_ids and book[index].site_id) for index in range(len(book))]
    last = max(pairs)
    # The matched chapter before and after each place in the site's list.
    before: list[tuple[int, int]] = []
    anchor = (-1, -1)
    for position in range(len(site)):
        before.append(anchor)
        if position in pairs:
            anchor = (position, pairs[position])
    after: list[tuple[int, int] | None] = [None] * len(site)
    anchor = None
    for position in reversed(range(len(site))):
        after[position] = anchor
        if position in pairs:
            anchor = (position, pairs[position])

    for position, chapter in enumerate(site):
        if position in pairs:
            continue
        if position > last:
            follows = len(book) - 1
        else:
            start, end = before[position], after[position]
            if end[1] <= start[1] or any(unmatched[start[1] + 1:end[1]]):
                continue
            follows = end[1] - 1
        plan.new_positions.append(chapter.position)
        plan.insert_after[chapter.position] = follows
    return plan


def apply_plan(book: list[Chapter], new_chapters: dict[int, Chapter], plan: UpdatePlan) -> list[Chapter]:
    """The book with ``new_chapters`` (by site position) put in where ``plan`` says, every chapter the book had kept, and the chapters numbered 1 onwards. Book chapters matched without an id get the site's id for them."""
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


def keep_taken_down(book: list[Chapter], fresh: list[Chapter], *, titles_from_author: bool = False) -> list[Chapter]:
    """For a fresh copy: every chapter downloaded again, plus the book's chapters the site no longer has, each kept after the chapter it followed in the book. ``titles_from_author`` says the site's titles are the author's own, so "Chapter 12" there names chapter 12.

    Raises CannotMatch when none of the book's chapters is among the fresh ones (then the titles have changed, not the chapters, and keeping them all would put the whole story in twice), and when a chapter the site no longer has in its place has no title of its own to tell which one it is.
    """
    if not book:
        return _numbered(list(fresh))
    pairs, book_items = _match(book, fresh, titles_from_author)
    if fresh and not pairs:
        raise CannotMatch(
            "none of the book's chapters could be found among the site's, "
            "so a fresh copy would have put the whole story in twice"
        )
    paired = {index: position for position, index in pairs.items()}
    fresh_has_ids = any(chapter.site_id for chapter in fresh)
    untold = [
        chapter for index, chapter in enumerate(book)
        if index not in paired and book_items[index].says_nothing and not (fresh_has_ids and chapter.site_id)
    ]
    if untold:
        raise CannotMatch(
            "the site no longer has some of the book's chapters, and they "
            "have no titles of their own to tell which, so a fresh copy "
            "can't tell which to keep"
        )
    following: dict[int, list[Chapter]] = {}
    anchor = -1
    for index, chapter in enumerate(book):
        if index in paired:
            anchor = paired[index]
        else:
            following.setdefault(anchor, []).append(chapter)
    result = list(following.get(-1, []))
    for position, chapter in enumerate(fresh):
        result.append(chapter)
        result.extend(following.get(position, []))
    return _numbered(result)


def dropped(old: list[Chapter], new: list[Chapter], *, titles_from_author: bool = False) -> list[Chapter]:
    """The chapters of ``old`` that ``new`` lacks. One carried over is found by its text; one downloaded again, as a fresh copy finds it: by site id, title or place. Checked before an update writes a book, which must never lose a chapter."""
    waiting: dict[str, list[int]] = {}
    for position, chapter in enumerate(new):
        waiting.setdefault(chapter.html, []).append(position)
    carried: set[int] = set()
    rest_of_old = []
    for chapter in old:
        positions = waiting.get(chapter.html)
        if positions:
            carried.add(positions.pop(0))
        else:
            rest_of_old.append(chapter)
    if not rest_of_old:
        return []
    rest_of_new = [chapter for position, chapter in enumerate(new) if position not in carried]
    pairs, _book_items = _match(rest_of_old, rest_of_new, titles_from_author)
    found = set(pairs.values())
    return [chapter for index, chapter in enumerate(rest_of_old) if index not in found]


def _numbered(chapters: list[Chapter]) -> list[Chapter]:
    return [replace(chapter, number=number) for number, chapter in enumerate(chapters, 1)]
