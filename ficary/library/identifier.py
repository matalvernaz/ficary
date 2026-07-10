"""Turn a read FileMetadata into an identified StoryCandidate.

Phase 1 is URL-only. If the file has an embedded source URL that
matches one of our adapters, confidence is HIGH and adapter_name is
filled in. Everything else is LOW — the file is still indexed, just
not auto-updatable until the review flow (Phase 4) resolves it.

identify() also performs a last-resort *fandom* backfill from the
file's parent directory relative to the library root. Many libraries
are already organised by fandom folder ("Naruto/", "Harry Potter/"),
and for downloader formats that don't embed an explicit Fandom field
(FicLab is the common case — its metadata goes into a single `tags`
row mixing genres/characters/status/fandom), the folder name is the
best signal available.

Adult-site and original-fiction adapters are *source-classified*:
their bucket is determined by the source URL, not by any embedded
fandom metadata or parent-folder name. A Literotica story sitting
in ``Harry Potter/`` because the user pasted it there once is still
adult fiction — the adapter override forces ``fandoms=["Adult"]``
on identification so the next reorganise migrates it correctly.
Without this override, ``_fandom_from_parent_folder`` would cement
the historical misplacement forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..updater import FileMetadata
from .candidate import Confidence, StoryCandidate
from .template import ADULT_FICTION_ADAPTERS, ORIGINAL_FICTION_ADAPTERS


# Site identifiers returned by identify(). These mirror the class names
# in cli._detect_site but as plain strings so the library package
# doesn't need to import every scraper class up front.
_URL_MARKERS = [
    ("ficwad.com", "ficwad"),
    ("archiveofourown.org", "ao3"),
    ("ao3.org", "ao3"),
    ("royalroad.com", "royalroad"),
    ("mediaminer.org", "mediaminer"),
    ("literotica.com", "literotica"),
    ("wattpad.com", "wattpad"),
    ("fanfiction.net", "ffn"),
    # Erotica sites — share the routing-by-adapter machinery so
    # downloads from these sources can be sorted into a dedicated
    # adult-fiction folder rather than falling through to "Misc".
    ("adult-fanfiction.org", "aff"),
    ("storiesonline.net", "storiesonline"),
    ("nifty.org", "nifty"),
    ("sexstories.com", "sexstories"),
    ("mcstories.com", "mcstories"),
    ("lushstories.com", "lushstories"),
    ("fictionmania.tv", "fictionmania"),
    ("tgstorytime.com", "tgstorytime"),
    ("chyoa.com", "chyoa"),
    ("darkwanderer.net", "darkwanderer"),
    ("greatfeet.com", "greatfeet"),
    ("bdsmlibrary.com", "bdsmlibrary"),
    # Group-specific fragment: tapatalk.com hosts thousands of boards,
    # only The Mousepad is a supported site.
    ("tapatalk.com/groups/themousepad", "mousepad"),
    ("readonlymind.com", "readonlymind"),
    ("giantessworld.net", "giantessworld"),
    ("chastitymansion.com", "chastitymansion"),
    ("ticklingforum.com", "ticklingforum"),
]

# Folder names that look like categorisation aids rather than fandoms.
# Used by _fandom_from_parent_folder so a ``Misc`` / ``Unsorted`` /
# ``Downloads`` catch-all dir never ends up recorded as a fandom.
# Includes the dedicated adult / original-works buckets so a file
# sitting in those folders without a recognised source URL doesn't
# get its bucket name backfilled as a fandom (which would then
# survive a reorganise that swaps the bucket name).
_NON_FANDOM_FOLDER_NAMES = frozenset({
    "misc", "miscellaneous", "unsorted", "sorted", "downloads",
    "fanfics", "fanfiction", "fics", "stories", "works",
    "archive", "todo", "tbr", "read", "unread",
    "adult", "original works", "original",
})


def adapter_for_url(url: str) -> str | None:
    """Return the short adapter name for a story URL, or None if the
    URL doesn't match any supported site. Used for indexing; the
    actual scraper class lookup happens through cli._detect_site when
    we need to probe for updates."""
    if not url:
        return None
    lower = url.lower()
    for marker, name in _URL_MARKERS:
        if marker in lower:
            return name
    return None


def _fandom_from_parent_folder(path: Path, root: Path | None) -> Optional[str]:
    """Return the library-root-relative top subfolder, if any.

    Guards:
    * ``path`` must live under ``root`` — otherwise the relative path
      would climb out with ``..`` and the first segment would be
      meaningless.
    * A file directly in the library root has no parent folder to
      borrow from; return None.
    * The folder name must not be a generic catch-all bucket like
      ``Misc`` — see :data:`_NON_FANDOM_FOLDER_NAMES`.
    """
    if root is None:
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    # relative.parts ends with the filename; anything shorter than
    # (folder, file) means the file sits in the root.
    if len(relative.parts) < 2:
        return None
    folder_name = relative.parts[0]
    if folder_name.lower() in _NON_FANDOM_FOLDER_NAMES:
        return None
    return folder_name


def identify(
    path: Path,
    metadata: FileMetadata,
    *,
    root: Path | None = None,
    adult_folder: str | None = None,
    original_folder: str | None = None,
) -> StoryCandidate:
    """Assemble a :class:`StoryCandidate` from a path + its read metadata.

    ``root`` is the library scan root; when supplied, the immediate
    parent folder is used as a fandom fallback for metadata that came
    back with no embedded fandom (common on FicLab-style downloads
    whose HTML lacks a dedicated fandom field).

    ``adult_folder`` / ``original_folder`` are the user's configured
    bucket names for adult-only and original-fiction adapters. When the
    source URL belongs to one of those adapter groups, the function
    forces ``metadata.fandoms`` to ``[adult_folder]`` (or
    ``[original_folder]``) and *skips* the parent-folder backfill —
    that bucket is determined by the source site, not by wherever the
    file happens to sit today. Pass ``None`` (the default) to disable
    the override; callers that don't care about adult routing keep the
    historical behaviour.
    """
    # Classify by source URL first — adult/original adapters have a
    # dedicated bucket and shouldn't inherit a parent-folder fandom or
    # honour an embedded fandom from a prior misplacement.
    adapter = (
        adapter_for_url(metadata.source_url) if metadata.source_url else None
    )

    if adapter in ADULT_FICTION_ADAPTERS and adult_folder:
        metadata.fandoms = [adult_folder]
    elif adapter in ORIGINAL_FICTION_ADAPTERS and original_folder:
        metadata.fandoms = [original_folder]
    elif not metadata.fandoms:
        # Back-fill from the parent folder for everything else — applies
        # regardless of whether the URL branch or the fallback branch
        # runs below, so FicLab files (which do have a URL and therefore
        # land in the HIGH-confidence path) still get a fandom.
        folder_fandom = _fandom_from_parent_folder(path, root)
        if folder_fandom:
            metadata.fandoms = [folder_fandom]

    candidate = StoryCandidate(path=path, metadata=metadata)

    if metadata.source_url:
        if adapter:
            candidate.adapter_name = adapter
            candidate.confidence = Confidence.HIGH
            return candidate
        candidate.notes.append(
            f"source URL {metadata.source_url!r} does not match any "
            "supported site; indexed but not trackable"
        )
        return candidate

    if not metadata.title and not metadata.author:
        candidate.notes.append(
            "no embedded URL, title, or author; filename is the only "
            "identifier — run --review-library to match it interactively"
        )
    else:
        candidate.notes.append(
            "no embedded URL; title/author present but fuzzy matching "
            "is deferred to --review-library"
        )
    return candidate
