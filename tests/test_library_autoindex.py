"""Auto-indexing of downloads + the added_at first-seen stamp.

Covers :func:`ficary.library.scanner.record_downloaded_file` (the
per-file indexer the download paths call after a successful export) and
the ``added_at`` field :meth:`LibraryIndex.record` stamps on first
sight and preserves across rescans.
"""

from __future__ import annotations

from pathlib import Path

from ficary.library.index import LibraryIndex
from ficary.library.scanner import (
    library_skip_reason, record_downloaded_file, scan,
)

from .library_fixtures import ficary_epub


def _idx(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


# ── added_at stamping ────────────────────────────────────────────


def test_scan_stamps_added_at(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    ficary_epub(lib, title="Stamped", url="https://www.fanfiction.net/s/1/1/")
    scan(lib, index_path=_idx(tmp_path))
    idx = LibraryIndex.load(_idx(tmp_path))
    [(_, entry)] = list(idx.stories_in(lib))
    assert entry.get("added_at"), "new entries must carry added_at"


def test_rescan_preserves_original_added_at(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    ficary_epub(lib, title="Stable", url="https://www.fanfiction.net/s/2/1/")
    scan(lib, index_path=_idx(tmp_path))
    idx = LibraryIndex.load(_idx(tmp_path))
    [(_, entry)] = list(idx.stories_in(lib))
    first = entry["added_at"]

    # Simulate time passing by forging an older stamp, then rescanning:
    # the rescan must keep the existing stamp, not re-stamp "now".
    entry["added_at"] = "2020-01-01T00:00:00Z"
    idx.save()
    scan(lib, index_path=_idx(tmp_path))
    reloaded = LibraryIndex.load(_idx(tmp_path))
    [(_, entry2)] = list(reloaded.stories_in(lib))
    assert entry2["added_at"] == "2020-01-01T00:00:00Z"
    assert first  # sanity: the original stamp existed


# ── record_downloaded_file ───────────────────────────────────────


def test_download_inside_library_is_recorded(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "Harry Potter").mkdir(parents=True)
    path = ficary_epub(
        lib / "Harry Potter", title="Fresh Download",
        url="https://www.fanfiction.net/s/10/1/",
    )
    ok = record_downloaded_file(
        path, library_root=lib, index_path=_idx(tmp_path),
    )
    assert ok is True
    idx = LibraryIndex.load(_idx(tmp_path))
    [(url, entry)] = list(idx.stories_in(lib))
    assert entry["title"] == "Fresh Download"
    assert entry["relpath"].startswith("Harry Potter")
    assert entry.get("added_at")


def test_download_outside_any_library_is_ignored(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    elsewhere = tmp_path / "Downloads"
    elsewhere.mkdir()
    path = ficary_epub(
        elsewhere, title="Loose File", url="https://www.fanfiction.net/s/11/1/",
    )
    ok = record_downloaded_file(
        path, library_root=lib, index_path=_idx(tmp_path),
    )
    assert ok is False
    idx = LibraryIndex.load(_idx(tmp_path))
    assert list(idx.stories_in(lib)) == []


def test_download_into_adult_root_is_recorded_there(tmp_path: Path):
    lib = tmp_path / "lib"
    adult = tmp_path / "adult"
    lib.mkdir()
    adult.mkdir()
    path = ficary_epub(
        adult, title="Adult Story",
        url="https://www.literotica.com/s/some-story",
    )
    ok = record_downloaded_file(
        path, library_root=lib, adult_root=adult, index_path=_idx(tmp_path),
    )
    assert ok is True
    idx = LibraryIndex.load(_idx(tmp_path))
    assert list(idx.stories_in(lib)) == []
    [(url, entry)] = list(idx.stories_in(adult))
    assert entry["title"] == "Adult Story"
    assert entry["adapter"] == "literotica"


def test_no_roots_configured_is_noop(tmp_path: Path):
    path = ficary_epub(
        tmp_path, title="X", url="https://www.fanfiction.net/s/12/1/",
    )
    assert record_downloaded_file(
        path, library_root=None, adult_root=None, index_path=_idx(tmp_path),
    ) is False


def test_unsupported_extension_is_noop(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    m4b = lib / "book.m4b"
    m4b.write_bytes(b"not really audio")
    assert record_downloaded_file(
        m4b, library_root=lib, index_path=_idx(tmp_path),
    ) is False


def test_missing_file_is_noop(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    assert record_downloaded_file(
        lib / "ghost.epub", library_root=lib, index_path=_idx(tmp_path),
    ) is False


def test_reindexing_same_download_twice_is_stable(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ficary_epub(
        lib, title="Twice", url="https://www.fanfiction.net/s/13/1/",
    )
    assert record_downloaded_file(
        path, library_root=lib, index_path=_idx(tmp_path),
    )
    idx = LibraryIndex.load(_idx(tmp_path))
    [(_, entry)] = list(idx.stories_in(lib))
    stamp = entry["added_at"]

    # A re-download of the same story (update flow) re-records; the
    # entry stays single and keeps its original added_at.
    assert record_downloaded_file(
        path, library_root=lib, index_path=_idx(tmp_path),
    )
    reloaded = LibraryIndex.load(_idx(tmp_path))
    entries = list(reloaded.stories_in(lib))
    assert len(entries) == 1
    assert entries[0][1]["added_at"] == stamp


# ── why a download wasn't indexed ────────────────────────────────
#
# record_downloaded_file returns a bare False for every decline, and the
# download paths used to log nothing at all in that case — so a Save-to
# folder outside the library read exactly like a successful index in the
# log. library_skip_reason is what those paths log instead.


def test_skip_reason_names_the_library_when_file_is_outside_it(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    staging = tmp_path / "ffdl debug"
    staging.mkdir()
    story = staging / "Trick or Treat.html"
    story.write_text("<html></html>", encoding="utf-8")

    assert record_downloaded_file(story, library_root=str(lib)) is False
    reason = library_skip_reason(story, str(lib))
    assert "outside your library folder" in reason
    assert str(lib) in reason
    assert "Scan Library" in reason  # tells the user how to recover


def test_skip_reason_flags_an_unconfigured_library(tmp_path: Path):
    story = tmp_path / "Story.html"
    story.write_text("<html></html>", encoding="utf-8")
    assert "no library folder is configured" in library_skip_reason(story, "")


def test_skip_reason_flags_an_unindexable_extension(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    audio = lib / "Story.m4b"
    audio.write_bytes(b"")
    reason = library_skip_reason(audio, str(lib))
    assert ".m4b" in reason and "aren't indexed" in reason


def test_indexed_file_has_no_skip_reason_path(tmp_path: Path):
    # The inverse guard: a file that really does land in the library
    # must be recorded, so callers never log a skip for it.
    lib = tmp_path / "lib"
    lib.mkdir()
    story = ficary_epub(
        lib, title="Inside", url="https://www.fanfiction.net/s/99/1/",
    )
    assert record_downloaded_file(
        story, library_root=str(lib), index_path=_idx(tmp_path),
    ) is True
