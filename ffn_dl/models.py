"""Data models for stories and chapters."""

import re
from dataclasses import dataclass, field


@dataclass
class Chapter:
    number: int
    title: str
    html: str


@dataclass
class Story:
    id: int
    title: str
    author: str
    summary: str
    url: str
    author_url: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# Structural labels that name their own place in the book. When the
# author's chapter title *is* one of these (optionally with a
# subtitle, e.g. "Prologue: Before the Fall"), we render it verbatim —
# "Chapter 1. Prologue" reads wrong.
STRUCTURAL_LABELS = (
    "prologue", "prelude", "preface", "foreword", "introduction",
    "epilogue", "afterword", "postscript", "coda",
    "interlude", "intermission", "intermezzo",
    "author's note", "authors note", "a/n",
)


def format_chapter_heading(n, title):
    """Render a chapter heading from its number and raw title.

    A missing/generic title collapses to ``Chapter N``; a title that
    already starts with "Chapter" or names a structural section
    (Prologue, Epilogue, Interlude, …) is preserved verbatim;
    otherwise the number is prepended so the reader always sees a
    clear chapter marker. Shared between the text/HTML/EPUB exporters
    and the TTS audiobook builder so the spoken and printed forms
    stay in sync.
    """
    title = (title or "").strip()
    if not title:
        return f"Chapter {n}"
    if re.match(r"^chapter\b", title, re.I):
        return title
    low = title.lower()
    for label in STRUCTURAL_LABELS:
        if low == label or low.startswith(label + ":") or low.startswith(label + " -"):
            return title
    if re.match(r"^\d+\s*[.\-:)]*\s*$", title):
        return f"Chapter {n}"
    return f"Chapter {n}. {title}"


def parse_chapter_heading(n, heading):
    """Inverse of :func:`format_chapter_heading`: recover the raw title.

    The merge-in-place updater reads chapter ``<h2>`` text back out of
    an existing export, but the exporter writes the *formatted* heading
    there ("Chapter 3. The Beginning"). Without stripping the prefix
    the recovered title would survive into a re-export and merged
    stories would mix raw and prefixed titles. We strip exactly the
    "Chapter N. " (or "Chapter N") prefix matching this chapter's
    number; anything else — including titles that legitimately start
    with "Chapter " from the author — is returned verbatim so the
    round-trip is idempotent.
    """
    heading = (heading or "").strip()
    if not heading:
        return ""
    bare = re.match(rf"^Chapter\s+{n}\s*$", heading, re.I)
    if bare:
        return ""
    prefixed = re.match(rf"^Chapter\s+{n}\.\s+(?P<rest>.+)$", heading, re.I)
    if prefixed:
        return prefixed.group("rest").strip()
    return heading


def parse_chapter_spec(spec):
    """Parse a chapter-range expression into a list of (lo, hi) tuples.

    Accepts comma-separated tokens: "5" (single), "1-5" (closed range),
    "-5" (1..5), "20-" (20..end), "1,3,5-10". hi=None means "through
    the last chapter". Whitespace is tolerated. Returns None when the
    spec is empty or None (meaning "no filter — take everything").
    Raises ValueError on malformed input.
    """
    if spec is None:
        return None
    text = str(spec).strip()
    if not text:
        return None

    ranges = []
    for token in text.split(","):
        t = token.strip()
        if not t:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", t)
        if m:
            lo_s, hi_s = m.group(1), m.group(2)
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else None
            if lo < 1:
                raise ValueError(f"Chapter range {t!r}: lower bound must be >= 1")
            if hi is not None and hi < lo:
                raise ValueError(f"Chapter range {t!r}: upper < lower")
            ranges.append((lo, hi))
            continue
        if t.isdigit():
            n = int(t)
            if n < 1:
                raise ValueError(f"Chapter {t!r}: must be >= 1")
            ranges.append((n, n))
            continue
        raise ValueError(f"Bad chapter token: {t!r}")

    return ranges or None


def chapter_in_spec(n, ranges):
    """True if chapter number `n` falls inside the parsed spec."""
    if ranges is None:
        return True
    for lo, hi in ranges:
        if n >= lo and (hi is None or n <= hi):
            return True
    return False
