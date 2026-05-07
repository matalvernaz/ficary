"""Chyoa (chyoa.com) scraper — interactive CYOA erotica.

Chyoa is a "choose your own adventure" erotica platform. Every work is
a tree of chapters where each chapter forks into multiple child
chapters; readers pick a branch at each decision point.

We download the **entire tree** rooted at whatever URL the user
pastes, in depth-first preorder. Each tree node becomes one chapter
in the output, numbered 1..N in traversal order. Pasting a
``/story/<slug>.<id>`` URL walks from the story root; pasting a
``/chapter/<slug>.<id>`` URL walks the subtree rooted at that
chapter.

Optional ``max_depth`` caps how deep the walker descends from the
entry. Depth 0 is the entry itself, depth 1 its immediate children,
etc. When the cap trips, the skipped child URLs are logged so
nothing is silently hidden.

Branch enumeration is anchored to ``<div class="question-content">``
on each chapter page — that container holds only the genuine
forward-branches and excludes Chyoa's site-wide footer
(Thank-You / DMCA / Contact). The "Previous Chapter" navigation link
is also outside this container, so backwards traversal can't happen
by accident.

HTML is clean server-side rendered: ``<h1>`` holds the chapter
title, ``<div class="chapter-content">`` holds the prose, and OG
meta tags carry the summary and canonical URL.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ..atomic import atomic_write_text
from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

CHYOA_BASE = "https://chyoa.com"

CHYOA_STORY_RE = re.compile(
    r"^https?://(?:www\.)?chyoa\.com/story/([^/?#\s]+)\.(\d+)", re.I,
)

CHYOA_CHAPTER_RE = re.compile(
    r"^https?://(?:www\.)?chyoa\.com/chapter/([^/?#\s]+)\.(\d+)", re.I,
)

# Pattern matched against href attributes when discovering child
# branches. Anchored to a clean ``.<digits>`` terminator so report /
# edit / favourite sub-paths under the same chapter URL don't get
# treated as separate nodes.
_CHILD_HREF_RE = re.compile(r"/chapter/[^/]+\.\d+$")


def _slug_id_to_int(slug: str, numeric: int) -> int:
    """Combine Chyoa's slug + numeric id into a stable integer key so
    two URL variants for the same chapter hash to the same cache dir."""
    h = hashlib.md5(f"{slug}:{numeric}".encode("utf-8")).hexdigest()[:10]
    return int(h, 16)


class ChyoaScraper(BaseScraper):
    """Scraper for chyoa.com chapters/stories with full tree walk."""

    site_name = "chyoa"

    def __init__(self, max_depth: Optional[int] = None, **kwargs):
        """``max_depth`` caps tree walk depth from the entry node.
        ``None`` means walk the whole reachable subtree."""
        super().__init__(**kwargs)
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        self.max_depth = max_depth

    @staticmethod
    def parse_story_id(url_or_id):
        """Return ``(kind, slug, numeric)`` where ``kind`` is ``'story'``
        or ``'chapter'``. Callers needing a single int should run the
        slug + numeric through :func:`_slug_id_to_int`."""
        text = str(url_or_id).strip()
        m = CHYOA_STORY_RE.search(text)
        if m:
            return ("story", m.group(1), int(m.group(2)))
        m = CHYOA_CHAPTER_RE.search(text)
        if m:
            return ("chapter", m.group(1), int(m.group(2)))
        raise ValueError(
            f"Cannot parse Chyoa URL from: {text!r}\n"
            "Expected e.g. https://chyoa.com/story/Dominant-Girlfriend.14 "
            "or https://chyoa.com/chapter/Ooh-that-s-hot.17"
        )

    @staticmethod
    def _canonical_url(kind: str, slug: str, numeric: int) -> str:
        return f"{CHYOA_BASE}/{kind}/{slug}.{numeric}"

    @staticmethod
    def _parse_metadata(soup, kind: str, numeric: int) -> dict:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        title = (og_title.get("content") if og_title else "") or ""
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
        title = title.strip() or f"Chyoa {kind} {numeric}"

        summary = (og_desc.get("content") if og_desc else "") or ""

        author = "Unknown Author"
        author_url = ""
        # Chyoa renders author links as absolute URLs
        # (``href="https://chyoa.com/user/<name>"``); the older
        # relative-only regex matched zero hits and every story
        # came back attributed to "Unknown Author".
        author_link = soup.find(
            "a", href=re.compile(r"(?:^|//(?:www\.)?chyoa\.com)/user/", re.I),
        )
        if author_link:
            author = author_link.get_text(strip=True) or author
            href = author_link.get("href", "")
            if href:
                author_url = (
                    href if href.startswith("http") else CHYOA_BASE + href
                )

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": {"chyoa_kind": kind, "numeric_id": numeric},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        content = soup.find("div", class_="chapter-content")
        if content is None:
            # Fall back: pick the article body or the first long div.
            content = soup.find("article") or soup.find("div", id="content")
        if content is None:
            raise ValueError("Could not find Chyoa chapter body.")
        # Drop Chyoa's ad-zones and author-bio inserts that share the
        # page but aren't chapter prose.
        for selector in ("div.chyoa-adzone", "div.chyoa-banner",
                         "div.chapter-nav", "div.chapter-choices"):
            for el in content.select(selector):
                el.decompose()
        return content.decode_contents()

    @staticmethod
    def _parse_children(soup) -> list[tuple[str, int, str]]:
        """Extract immediate child branches from a chapter page.

        Returns ``[(slug, numeric, title), ...]`` in the order Chyoa
        renders them. Scoped to ``div.question-content`` so the
        site-wide footer (Thank-You/DMCA/Contact) and the
        "Previous Chapter" back-link can't leak into the walk."""
        out: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for container in soup.find_all("div", class_="question-content"):
            for a in container.find_all("a", href=_CHILD_HREF_RE):
                href = a.get("href", "")
                m = CHYOA_CHAPTER_RE.search(
                    href if href.startswith("http") else CHYOA_BASE + href
                )
                if not m:
                    continue
                slug, numeric = m.group(1), int(m.group(2))
                key = (slug, numeric)
                if key in seen:
                    continue
                seen.add(key)
                title = a.get_text(" ", strip=True) or f"Chapter {numeric}"
                out.append((slug, numeric, title))
        return out

    # ── Per-node cache ───────────────────────────────────────────
    #
    # Each chyoa chapter is cached individually keyed by its own
    # slug+numeric, not by its position in the current walk. That
    # means re-running with a different entry URL — or an unrelated
    # tree that shares some chapters via cross-links — reuses the
    # cached HTML for free, and adding/reordering branches on Chyoa's
    # side doesn't invalidate unaffected nodes.

    def _node_cache_path(self, slug: str, numeric: int) -> Optional[Path]:
        if not self.use_cache:
            return None
        node_id = _slug_id_to_int(slug, numeric)
        d = self.cache_dir / f"{self.site_name}_node_{node_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d / "node.json"

    def _load_node_cache(self, slug: str, numeric: int) -> Optional[dict]:
        path = self._node_cache_path(slug, numeric)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Children round-tripped as list-of-lists in JSON.
            data["children"] = [
                (c[0], int(c[1]), c[2]) for c in data.get("children", [])
            ]
            return data
        except (ValueError, UnicodeDecodeError, OSError, KeyError) as exc:
            logger.warning(
                "Corrupt chyoa node cache %s (%s); will refetch", path, exc
            )
            path.unlink(missing_ok=True)
            return None

    def _save_node_cache(
        self, slug: str, numeric: int, title: str, html: str,
        children: list[tuple[str, int, str]],
    ) -> None:
        path = self._node_cache_path(slug, numeric)
        if path is None:
            return
        atomic_write_text(path, json.dumps({
            "title": title, "html": html, "children": children,
        }, ensure_ascii=False))

    def _fetch_node(
        self, kind: str, slug: str, numeric: int,
    ) -> tuple[str, str, list[tuple[str, int, str]], BeautifulSoup | None]:
        """Return ``(title, html, children, soup_or_None)`` for one node.

        ``soup_or_None`` is the parsed page when the node was just
        fetched (so the caller can extract Story-level metadata from
        the entry node), and ``None`` when the node was served from
        cache."""
        cached = self._load_node_cache(slug, numeric)
        if cached is not None:
            return cached["title"], cached["html"], cached["children"], None
        url = self._canonical_url(kind, slug, numeric)
        page_html = self._fetch(url)
        soup = BeautifulSoup(page_html, "lxml")
        # Node title: prefer og:title (matches what Chyoa renders),
        # fall back to <h1>, then a generic id-based label so a
        # malformed page never produces a blank chapter title.
        og_title = soup.find("meta", attrs={"property": "og:title"})
        title = (og_title.get("content") if og_title else "") or ""
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
        title = title.strip() or f"Chyoa chapter {numeric}"
        body = self._parse_chapter_html(soup)
        children = self._parse_children(soup)
        self._save_node_cache(slug, numeric, title, body, children)
        return title, body, children, soup

    def get_chapter_count(self, url_or_id):
        """Walk the tree shape (no body parsing) to count nodes.

        Cheap on a warm cache — ``_fetch_node`` short-circuits on
        cache hit. Cold cache costs one HTTP request per node, same
        as a full ``download``."""
        kind, slug, numeric = self.parse_story_id(url_or_id)
        visited: set[tuple[str, int]] = set()
        return self._count_recursive(kind, slug, numeric, 0, visited)

    def _count_recursive(
        self, kind: str, slug: str, numeric: int,
        depth: int, visited: set[tuple[str, int]],
    ) -> int:
        key = (slug, numeric)
        if key in visited:
            return 0
        visited.add(key)
        _title, _body, children, _soup = self._fetch_node(kind, slug, numeric)
        total = 1
        if self.max_depth is not None and depth >= self.max_depth:
            return total
        for cslug, cnum, _ctitle in children:
            total += self._count_recursive(
                "chapter", cslug, cnum, depth + 1, visited,
            )
        return total

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        entry_kind, entry_slug, entry_numeric = self.parse_story_id(url_or_id)
        story_id = _slug_id_to_int(entry_slug, entry_numeric)
        entry_url = self._canonical_url(entry_kind, entry_slug, entry_numeric)

        logger.info(
            "Walking Chyoa tree from %s %s.%s (max_depth=%s) ...",
            entry_kind, entry_slug, entry_numeric, self.max_depth,
        )

        # Fetch the entry first so we have the soup for Story-level
        # metadata. Subsequent walk nodes only need (title, html,
        # children) and may come from cache.
        entry_title, entry_body, entry_children, entry_soup = self._fetch_node(
            entry_kind, entry_slug, entry_numeric,
        )

        if entry_soup is not None:
            meta = self._parse_metadata(entry_soup, entry_kind, entry_numeric)
        else:
            # Cache hit on entry — re-derive minimal metadata from
            # the cached title; author/summary fall back to defaults.
            # (Re-fetching the soup here would defeat the cache.)
            meta = {
                "title": entry_title, "author": "Unknown Author",
                "author_url": "", "summary": "",
                "extra": {
                    "chyoa_kind": entry_kind, "numeric_id": entry_numeric,
                },
            }

        # Depth-first preorder walk. Each node gets a sequential
        # chapter number; ``visited`` dedups the rare case of a
        # chyoa cross-link pointing back into the subtree we already
        # visited (treats the tree as a DAG to be safe).
        chapters_out: list[tuple[int, str, str]] = []  # (depth, title, html)
        skipped: list[tuple[int, str, int, str]] = []  # at depth cap
        visited: set[tuple[str, int]] = set()

        def walk(kind: str, slug: str, numeric: int, depth: int) -> None:
            key = (slug, numeric)
            if key in visited:
                return
            visited.add(key)
            if (slug, numeric) == (entry_slug, entry_numeric):
                title, body, children = entry_title, entry_body, entry_children
            else:
                title, body, children, _soup = self._fetch_node(
                    kind, slug, numeric,
                )
            chapters_out.append((depth, title, body))
            if self.max_depth is not None and depth >= self.max_depth:
                for cslug, cnum, ctitle in children:
                    skipped.append((depth + 1, cslug, cnum, ctitle))
                return
            for cslug, cnum, _ctitle in children:
                walk("chapter", cslug, cnum, depth + 1)

        walk(entry_kind, entry_slug, entry_numeric, 0)

        if skipped:
            # Transparency: list every branch the depth cap blocked
            # so the user knows what's missing rather than guessing
            # from the chapter count.
            logger.info(
                "Chyoa max_depth=%s blocked %d branch(es):",
                self.max_depth, len(skipped),
            )
            for depth, cslug, cnum, ctitle in skipped:
                logger.info(
                    "  depth %d: %s (%s)",
                    depth, ctitle,
                    self._canonical_url("chapter", cslug, cnum),
                )

        meta["num_chapters"] = len(chapters_out)
        meta["chapter_titles"] = {
            str(i + 1): t for i, (_d, t, _h) in enumerate(chapters_out)
        }
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=entry_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        total = len(chapters_out)
        for idx, (_depth, title, html) in enumerate(chapters_out, start=1):
            if idx <= skip_chapters or not chapter_in_spec(idx, chapters):
                continue
            ch = Chapter(number=idx, title=title, html=html)
            story.chapters.append(ch)
            if progress_callback:
                # ``cached`` is hard to report accurately at this
                # point — the per-node cache decision was made inside
                # ``_fetch_node`` on the walk pass. Reporting False
                # would lie on warm-cache runs; reporting True would
                # lie on cold runs. The ``cached_unknown=True``
                # convention lets the callback render a neutral
                # state and matches what other tree-walking scrapers
                # in this codebase do for batch progress.
                progress_callback(idx, total, title, False)
        return story
