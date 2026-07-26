"""Full-text search index over MCStories story bodies.

MCStories has no search endpoint, so ficary's keyword search filters
the site's A-Z title index. That index carries a title, an author, tag
codes and a one-line synopsis per story — roughly 150 characters of
searchable text against an average body of 23,000. Searching it is
searching under 1% of the archive's prose: the query ``feet`` matched
32 stories, while sampling bodies put the number whose prose is
substantially about feet at closer to 600.

This module crawls chapter bodies once into a SQLite FTS5 index so
keyword search can match the prose instead. Design notes:

* **Explicitly built, never implicit.** The crawl is ~17,600 page
  fetches. A search must not silently trigger that, so searches use
  the index when it exists and fall back to the synopsis match when it
  doesn't. Building is a deliberate user action.

* **Resumable.** Every indexed slug is recorded with the time it was
  indexed, so a re-run fetches only slugs that aren't in the index
  yet. A build interrupted at story 9,000 resumes there rather than
  starting over, and the index is queryable while incomplete — a
  partial index still beats synopsis-only recall.

* **Bodies are stored, not just indexed.** FTS5 can index without
  retaining the text, which would roughly halve the file. Retaining it
  keeps per-story re-indexing a plain ``DELETE`` and leaves
  ``snippet()`` available, at the cost of carrying the archive's text
  (~0.4 GB) on disk.

* **HTTP comes from the caller.** ``build`` takes a ``fetch``
  callable rather than importing the search module's fetcher, because
  the search module imports this one. Same reason ``build`` takes the
  title-index rows instead of fetching them.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

MCS_BASE = "https://mcstories.com"

_INDEX_FILENAME = "mcstories-fulltext.sqlite3"

_BUILD_FETCH_WORKERS = 6
"""Concurrent story fetches during a build. The letter pages tolerate
8, but a build makes thousands of requests rather than 26, so this
leans lower — the crawl is a background one-off and finishing a few
minutes sooner isn't worth leaning on someone's archive."""

_COMMIT_EVERY = 50
"""Stories per transaction. A build that dies (or is cancelled) keeps
everything up to the last commit, so this bounds re-fetch on resume to
at most 50 stories rather than the whole run."""

_FTS_TERM_RE = re.compile(r"[0-9A-Za-zÀ-￿]+")
"""Runs of word characters, used to rebuild a query as quoted FTS5
terms. Everything else is dropped: FTS5 treats ``"``, ``*``, ``(``,
``:`` and ``-`` as syntax, so passing a raw user query through would
turn a typo into a query error."""


@dataclass
class BuildReport:
    """Outcome of a :func:`build` run, split so callers can report
    actionable numbers without re-walking a result list."""

    indexed: int = 0
    already_present: int = 0
    failed: int = 0
    cancelled: bool = False


@dataclass
class IndexStats:
    """What's currently on disk. ``stories`` is the number of indexed
    slugs; ``newest_indexed_at`` is a Unix timestamp, 0.0 when the
    index is empty."""

    stories: int = 0
    bytes_on_disk: int = 0
    newest_indexed_at: float = 0.0


def index_path() -> Path:
    """Location of the index file. Lives in the portable cache dir so a
    portable install keeps it alongside the rest of ficary's state."""
    from ..portable import cache_dir

    return cache_dir() / _INDEX_FILENAME


def fts5_supported() -> bool:
    """Whether this interpreter's SQLite was built with FTS5."""
    try:
        with sqlite3.connect(":memory:") as probe:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.Error:
        return False


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the index, creating the schema when it's a fresh file."""
    target = Path(path) if path is not None else index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS bodies USING fts5(
            slug UNINDEXED,
            body
        );
        CREATE TABLE IF NOT EXISTS indexed(
            slug TEXT PRIMARY KEY,
            chapters INTEGER NOT NULL,
            chars INTEGER NOT NULL,
            indexed_at REAL NOT NULL
        );
        """,
    )
    return conn


def stats(path: Optional[Path] = None) -> IndexStats:
    """Read the index's size and coverage. Returns an empty
    :class:`IndexStats` when there's no index yet or it can't be
    opened — callers treat that as "not built"."""
    target = Path(path) if path is not None else index_path()
    if not target.exists():
        return IndexStats()
    try:
        with _connect(target) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(indexed_at), 0.0) FROM indexed",
            ).fetchone()
        return IndexStats(
            stories=int(row[0] or 0),
            bytes_on_disk=target.stat().st_size,
            newest_indexed_at=float(row[1] or 0.0),
        )
    except (sqlite3.Error, OSError):
        logger.debug("mcstories full-text stats failed", exc_info=True)
        return IndexStats()


def available(path: Optional[Path] = None) -> bool:
    """Whether a usable index exists. An index with zero stories counts
    as absent so a half-created file doesn't suppress the synopsis
    fallback."""
    return stats(path).stories > 0


def _fts_match_expression(query: str) -> str:
    """Translate a typed query into an FTS5 MATCH expression.

    Terms are quoted and ANDed, mirroring the AND-of-terms semantics of
    the synopsis matcher: ``college sorority`` finds bodies containing
    both words anywhere, not the adjacent phrase. Returns ``""`` when
    the query has no usable terms.
    """
    terms = _FTS_TERM_RE.findall(query or "")
    return " AND ".join(f'"{t}"' for t in terms)


def ranked_slugs(
    query: str, path: Optional[Path] = None,
) -> Optional[list[str]]:
    """Slugs whose body matches ``query``, most mentions first.

    Ordering matters more here than it looks. Common words carry
    incidental matches — "feet" appears in thousands of MCStories
    bodies purely as a unit of distance — so presence alone is a weak
    signal, and an unordered union buries the stories a query is
    *about* under passing mentions.

    Ranking is by how many times the query's terms occur in the body.
    FTS5's ``bm25`` is the obvious candidate and the wrong one here: it
    normalises by document length, so for a one-word query a 3,000-word
    story mentioning "feet" twice outranks a 40,000-word story built
    around it. Raw occurrence count is the signal that actually
    separates "this story is about feet" from "someone stood ten feet
    away", and it's the same measure used to size the recall gap in the
    first place. ``MATCH`` still does the heavy filtering; the count
    only runs over rows that already matched.

    Returns ``None`` — meaning "no full-text opinion, fall back" — when
    there's no index, the query has no usable terms, or the lookup
    errors. An empty list is a real answer: the index was consulted and
    no body matched.
    """
    expression = _fts_match_expression(query)
    if not expression:
        return None
    target = Path(path) if path is not None else index_path()
    if not target.exists():
        return None

    # Occurrence count per term, summed. Counting by the length the
    # string loses when the term is deleted is the standard SQL trick
    # for this and keeps the scan in C rather than pulling every
    # matched body into Python. Terms come from _FTS_TERM_RE so they
    # are word characters only, but they're still bound as parameters.
    terms = _FTS_TERM_RE.findall(query or "")
    params: dict[str, object] = {"expr": expression}
    score_parts = []
    for position, term in enumerate(terms):
        params[f"t{position}"] = term.lower()
        params[f"n{position}"] = len(term)
        score_parts.append(
            f"(LENGTH(lowered) - LENGTH(REPLACE(lowered, :t{position}, '')))"
            f" / :n{position}",
        )
    # Ties break on slug so the page window stays stable across
    # Load More.
    sql = (
        f"SELECT slug, {' + '.join(score_parts)} AS mentions FROM ("
        "SELECT slug, LOWER(body) AS lowered FROM bodies "
        "WHERE bodies MATCH :expr"
        ") ORDER BY mentions DESC, slug"
    )
    try:
        with _connect(target) as conn:
            return [r[0] for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        logger.debug(
            "mcstories full-text query %r failed", expression, exc_info=True,
        )
        return None


def indexed_slugs(path: Optional[Path] = None) -> set[str]:
    """Every slug already in the index. Used to skip work on resume."""
    target = Path(path) if path is not None else index_path()
    if not target.exists():
        return set()
    try:
        with _connect(target) as conn:
            return {r[0] for r in conn.execute("SELECT slug FROM indexed")}
    except sqlite3.Error:
        logger.debug("mcstories full-text slug read failed", exc_info=True)
        return set()


def _story_text(slug: str, fetch: Callable[[str], str]) -> tuple[str, int]:
    """Fetch one story and return ``(plain text, chapter count)``.

    Chapter discovery reuses the scraper's own index-page parser rather
    than a second URL-guessing implementation, so a story whose
    chapters are linked unusually is crawled the same way a download
    would crawl it.
    """
    from bs4 import BeautifulSoup

    from .mcstories import MCStoriesScraper

    soup = BeautifulSoup(fetch(f"{MCS_BASE}/{slug}/"), "lxml")
    meta = MCStoriesScraper._parse_metadata(soup, slug)
    parts: list[str] = []
    for chapter_url in meta["chapter_urls"]:
        chapter_soup = BeautifulSoup(fetch(chapter_url), "lxml")
        article = (
            chapter_soup.find("article", id="mcstories")
            or chapter_soup.find("article")
        )
        if article is not None:
            parts.append(article.get_text(" ", strip=True))
    return "\n".join(parts), len(meta["chapter_urls"])


def build(
    rows: Iterable[dict],
    *,
    fetch: Callable[[str], str],
    progress: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    workers: int = _BUILD_FETCH_WORKERS,
    limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> BuildReport:
    """Index the bodies of every story in ``rows`` that isn't indexed yet.

    ``rows`` are title-index rows (only ``slug`` is read). ``fetch``
    performs one HTTP GET and returns the body. ``progress`` receives
    human-readable status lines. ``cancel`` is polled between stories
    and stops the run cleanly, keeping what's already committed.
    ``limit`` caps how many *new* stories are fetched, which is what
    makes a smoke run possible without crawling the archive.

    Raises :class:`RuntimeError` when SQLite has no FTS5 support.
    """
    if not fts5_supported():
        raise RuntimeError(
            "This build of SQLite has no FTS5 support, so the MCStories "
            "full-text index cannot be created.",
        )

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    report = BuildReport()
    present = indexed_slugs(path)
    pending = []
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        if slug in present:
            report.already_present += 1
            continue
        pending.append(slug)
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        say(
            f"MCStories full-text index already covers all "
            f"{report.already_present} stories.",
        )
        return report

    say(
        f"Indexing {len(pending)} MCStories stories "
        f"({report.already_present} already indexed).",
    )

    def harvest(slug: str) -> tuple[str, Optional[str], int]:
        try:
            text, chapters = _story_text(slug, fetch)
            return slug, text, chapters
        except Exception as exc:
            logger.debug("mcstories full-text: %s failed: %s", slug, exc)
            return slug, None, 0

    # Work in chunks rather than handing the whole list to ``map``:
    # ``map`` submits every item immediately, so a cancel would only
    # stop *recording* results while the crawl carried on to the end.
    # One chunk is also one transaction and one progress line.
    chunk_size = max(max(1, workers), _COMMIT_EVERY)
    conn = _connect(path)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for start in range(0, len(pending), chunk_size):
                batch = pending[start:start + chunk_size]
                for slug, text, chapters in ex.map(harvest, batch):
                    if not text or not text.strip():
                        # No prose means a failed fetch or a parse
                        # miss, not an empty story — recording it
                        # would make resume skip it forever.
                        report.failed += 1
                        continue
                    conn.execute("DELETE FROM bodies WHERE slug = ?", (slug,))
                    conn.execute(
                        "INSERT INTO bodies(slug, body) VALUES (?, ?)",
                        (slug, text),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO indexed"
                        "(slug, chapters, chars, indexed_at) "
                        "VALUES (?, ?, ?, ?)",
                        (slug, chapters, len(text), time.time()),
                    )
                    report.indexed += 1
                conn.commit()
                say(
                    f"  full-text index: "
                    f"{min(start + chunk_size, len(pending))}/{len(pending)} "
                    f"({report.indexed} indexed, {report.failed} failed)",
                )
                if cancel is not None and cancel():
                    report.cancelled = True
                    say("  full-text index: cancelled; progress kept.")
                    break
    finally:
        conn.close()

    final = stats(path)
    say(
        f"MCStories full-text index: {final.stories} stories, "
        f"{final.bytes_on_disk / 1e6:.0f} MB on disk "
        f"({report.indexed} added, {report.failed} failed this run).",
    )
    return report
