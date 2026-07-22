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


def merge_chapter_lists(existing, new):
    """Merge two chapter lists, deduping by chapter number with the
    freshly-downloaded chapter winning. Returns ``(merged_sorted,
    duplicate_count)``.

    Without the dedupe, an author re-publishing chapter N (a routine
    occurrence — fixing typos, re-numbering after edits) produces a
    merged file with two chapter-N rows. The freshly-downloaded body is
    the one we keep. Lives here (not in cli) so the CLI update path and
    the GUI update path route through the same helper — the GUI used to
    have its own dedupe-free merge, which was exactly the bug class the
    CLI was hardened against in round 9.
    """
    by_number: dict[int, Chapter] = {}
    for ch in existing:
        by_number[ch.number] = ch
    duplicates = 0
    for ch in new:
        if ch.number in by_number:
            duplicates += 1
        by_number[ch.number] = ch
    merged = sorted(by_number.values(), key=lambda c: c.number)
    return merged, duplicates


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


def is_structural_title(title):
    """True when the title names a structural section (Prologue, …).

    Matches a bare structural label or one carrying a subtitle
    ("Prologue: Before the Fall"). Structural chapters render verbatim
    and don't consume a "Chapter N" slot in display numbering.
    """
    low = (title or "").strip().lower()
    for label in STRUCTURAL_LABELS:
        if low == label or low.startswith(label + ":") or low.startswith(label + " -"):
            return True
    return False


def chapter_display_numbers(pairs):
    """Map stored chapter numbers to the numbers their headings display.

    ``pairs`` is an iterable of ``(number, title)``. Structural chapters
    (Prologue, Interlude, …) are unnumbered in most authors' schemes, so
    they don't consume a slot: a fic that opens with a Prologue has its
    next chapter display as "Chapter 1", not "Chapter 2". Each
    non-structural chapter displays its stored number minus the count of
    structural chapters before it, which keeps partial-range downloads
    (``--chapters 5-10``) anchored to their real numbers instead of
    restarting at 1. Structural entries get the same offset value; it is
    never rendered (their headings are verbatim) but keeps the map total.
    """
    numbers = {}
    offset = 0
    for number, title in sorted(pairs, key=lambda p: p[0]):
        if is_structural_title(title):
            offset += 1
        numbers[number] = number - offset
    return numbers


def format_chapter_heading(n, title):
    """Render a chapter heading from its number and raw title.

    A missing/generic title collapses to ``Chapter N``; a title that
    already starts with "Chapter" or names a structural section
    (Prologue, Epilogue, Interlude, …) is preserved verbatim;
    otherwise the number is prepended so the reader always sees a
    clear chapter marker. ``n`` is the *display* number — callers with
    the full chapter list pass it through
    :func:`chapter_display_numbers` first so structural chapters don't
    shift the numbering. Shared between the text/HTML/EPUB exporters
    and the TTS audiobook builder so the spoken and printed forms
    stay in sync.
    """
    title = (title or "").strip()
    if not title:
        return f"Chapter {n}"
    if re.match(r"^chapter\b", title, re.I):
        return title
    if is_structural_title(title):
        return title
    if re.match(r"^\d+\s*[.\-:)]*\s*$", title):
        return f"Chapter {n}"
    return f"Chapter {n}. {title}"


def parse_chapter_heading(n, heading, display_n=None):
    """Inverse of :func:`format_chapter_heading`: recover the raw title.

    The merge-in-place updater reads chapter ``<h2>`` text back out of
    an existing export, but the exporter writes the *formatted* heading
    there ("Chapter 3. The Beginning"). Without stripping the prefix
    the recovered title would survive into a re-export and merged
    stories would mix raw and prefixed titles. We strip exactly the
    "Chapter N. " (or "Chapter N") prefix matching this chapter's
    display number, falling back to its stored number — exports written
    before structural chapters (Prologue, …) stopped consuming a slot
    carry the stored number, and accepting it lets a merge heal their
    headings to the display scheme. Anything else — including titles
    that legitimately start with "Chapter " from the author — is
    returned verbatim.
    """
    heading = (heading or "").strip()
    if not heading:
        return ""
    accepted = [display_n, n] if display_n is not None and display_n != n else [n]
    for num in accepted:
        if re.match(rf"^Chapter\s+{num}\s*$", heading, re.I):
            return ""
        prefixed = re.match(rf"^Chapter\s+{num}\.\s+(?P<rest>.+)$", heading, re.I)
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
            if not lo_s and not hi_s:
                # A bare "-" matches with both groups empty and would
                # silently expand to "all chapters" — almost certainly a
                # typo (e.g. "1-5,-"). Reject it instead.
                raise ValueError(f"Bad chapter token: {t!r} (empty range)")
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
