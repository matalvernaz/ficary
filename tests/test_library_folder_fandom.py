"""Tests for the parent-folder fandom fallback in library.identifier.

FicLab (and any future downloader whose HTML doesn't carry a dedicated
fandom field) leaves ``FileMetadata.fandoms`` empty after parsing. When
the library has already been organised into fandom folders, the folder
name is the best available signal — identify() uses the immediate
subfolder under the scan root as a fandom fallback.
"""
from __future__ import annotations

from pathlib import Path

from ficary.library.identifier import identify
from ficary.updater import FileMetadata


def _mk_metadata(**overrides) -> FileMetadata:
    """Build a FileMetadata with sensible defaults."""
    md = FileMetadata(
        source_url="https://www.fanfiction.net/s/12345/",
        title="Some Story",
        author="Some Author",
        format="html",
    )
    for k, v in overrides.items():
        setattr(md, k, v)
    return md


def test_fandom_backfill_uses_immediate_subfolder(tmp_path):
    """A file in ``<root>/Naruto/story.html`` gets fandom ``Naruto``."""
    root = tmp_path / "library"
    fandom_dir = root / "Naruto"
    fandom_dir.mkdir(parents=True)
    path = fandom_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Naruto"]


def test_fandom_backfill_respects_existing_fandoms(tmp_path):
    """If the file's metadata already carried a fandom, we don't
    overwrite it with the folder name — the explicit value wins."""
    root = tmp_path / "library"
    fandom_dir = root / "Harry Potter"
    fandom_dir.mkdir(parents=True)
    path = fandom_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=["Actual Fandom From Metadata"])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Actual Fandom From Metadata"]


def test_fandom_backfill_skipped_for_files_in_library_root(tmp_path):
    """A file directly in the library root has no parent subfolder to
    borrow from — fandoms stay empty rather than borrowing 'library'."""
    root = tmp_path / "library"
    root.mkdir()
    path = root / "flat-file.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_skips_catch_all_folders(tmp_path):
    """A folder called ``Misc`` or ``Unsorted`` isn't a fandom."""
    root = tmp_path / "library"
    misc_dir = root / "Misc"
    misc_dir.mkdir(parents=True)
    path = misc_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_when_root_not_supplied(tmp_path):
    """identify() called without ``root`` skips the backfill — the
    caller (older code path, or a test that doesn't care) gets the
    historical behaviour."""
    path = tmp_path / "anywhere" / "story.html"
    path.parent.mkdir(parents=True)
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md)  # no root=

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_uses_first_segment_for_nested_folders(tmp_path):
    """A deeper layout like ``Naruto/Crossovers/story.html`` records
    ``Naruto`` as the fandom — the top-level subfolder is the user's
    primary categorisation signal."""
    root = tmp_path / "library"
    deep = root / "Naruto" / "Crossovers"
    deep.mkdir(parents=True)
    path = deep / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Naruto"]


# ── Adapter override: adult / original-fiction routing ───────────


def test_identify_routes_adult_adapter_to_adult_folder(tmp_path):
    """A Literotica story sitting in ``Harry Potter/`` (because the
    user pasted it there once, or because pre-Adult-routing autosort
    landed it there) gets re-tagged with the Adult bucket on the
    next scan. Without this override, ``_fandom_from_parent_folder``
    would cement the historical misplacement forever — the original
    bug behind erotica leaking into fandom folders."""
    root = tmp_path / "library"
    hp = root / "Harry Potter"
    hp.mkdir(parents=True)
    path = hp / "story.epub"
    path.write_text("dummy", encoding="utf-8")

    md = _mk_metadata(
        source_url="https://www.literotica.com/s/some-slug",
        fandoms=[],
    )
    candidate = identify(path, md, root=root, adult_folder="Adult")

    assert candidate.metadata.fandoms == ["Adult"]
    assert candidate.adapter_name == "literotica"


def test_identify_routes_original_adapter_to_original_folder(tmp_path):
    """Royal Road stories — even with an inherited parent folder —
    land under Original Works on the next scan."""
    root = tmp_path / "library"
    misplaced = root / "Some Fandom"
    misplaced.mkdir(parents=True)
    path = misplaced / "story.epub"
    path.write_text("dummy", encoding="utf-8")

    md = _mk_metadata(
        source_url="https://www.royalroad.com/fiction/12345/title",
        fandoms=[],
    )
    candidate = identify(
        path, md, root=root, original_folder="Original Works",
    )

    assert candidate.metadata.fandoms == ["Original Works"]
    assert candidate.adapter_name == "royalroad"


def test_identify_adult_override_supersedes_existing_fandom(tmp_path):
    """An AFF EPUB that already carried ``fandoms=['Harry Potter']``
    from an earlier scan (because the parent folder was HP/) gets
    re-tagged to the Adult bucket. Otherwise a single bad placement
    would self-perpetuate across every scan/reorganise cycle."""
    root = tmp_path / "library"
    hp = root / "Harry Potter"
    hp.mkdir(parents=True)
    path = hp / "story.epub"
    path.write_text("dummy", encoding="utf-8")

    md = _mk_metadata(
        source_url="https://hp.adult-fanfiction.org/story.php?no=12345",
        fandoms=["Harry Potter"],
    )
    candidate = identify(path, md, root=root, adult_folder="Adult")

    assert candidate.metadata.fandoms == ["Adult"]
    assert candidate.adapter_name == "aff"


def test_identify_adult_override_disabled_when_folder_arg_omitted(tmp_path):
    """Backwards-compat path: callers that don't pass adult_folder
    keep the historical behaviour. Used by the older test suite and
    by any caller that doesn't care about adult routing."""
    root = tmp_path / "library"
    misplaced = root / "Some Fandom"
    misplaced.mkdir(parents=True)
    path = misplaced / "story.epub"
    path.write_text("dummy", encoding="utf-8")

    md = _mk_metadata(
        source_url="https://www.literotica.com/s/some-slug",
        fandoms=["Some Fandom"],
    )
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Some Fandom"]


def test_identify_non_adult_adapter_unchanged_by_override_arg(tmp_path):
    """The adult/original folder kwargs only fire for the matching
    adapter group — an FFN story still gets its parent-folder
    fallback even when the kwargs are present."""
    root = tmp_path / "library"
    fandom_dir = root / "Naruto"
    fandom_dir.mkdir(parents=True)
    path = fandom_dir / "story.epub"
    path.write_text("dummy", encoding="utf-8")

    md = _mk_metadata(
        source_url="https://www.fanfiction.net/s/12345/",
        fandoms=[],
    )
    candidate = identify(
        path, md, root=root,
        adult_folder="Adult",
        original_folder="Original Works",
    )

    assert candidate.metadata.fandoms == ["Naruto"]
    assert candidate.adapter_name == "ffn"


def test_non_fandom_folder_set_includes_adult_and_original_buckets(tmp_path):
    """A file in ``Adult/`` or ``Original Works/`` without a source
    URL doesn't get its bucket name backfilled as a fandom — without
    this exclusion, a no-URL EPUB in Adult/ would survive a future
    reorganise under a renamed bucket as ``fandom='Adult'``."""
    root = tmp_path / "library"
    for bucket in ("Adult", "Original Works"):
        bucket_dir = root / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        path = bucket_dir / "story.html"
        path.write_text("<html></html>", encoding="utf-8")

        md = _mk_metadata(source_url="", fandoms=[])
        candidate = identify(path, md, root=root)
        assert candidate.metadata.fandoms == [], (
            f"folder {bucket!r} should not be treated as a fandom"
        )
