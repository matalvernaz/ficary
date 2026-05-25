"""Tests for V3: ``reorganizer.plan_with_conflicts`` surfaces target
collisions at plan time so the user can resolve before --apply
silently skips collided moves."""

from __future__ import annotations

from pathlib import Path

from ffn_dl.library.index import LibraryIndex
from ffn_dl.library.reorganizer import plan, plan_with_conflicts


def _seed_entry(idx, root: Path, url: str, relpath: str, **fields) -> None:
    lib = idx.library_state(root)
    entry = {
        "relpath": relpath,
        "title": fields.get("title", "T"),
        "author": fields.get("author", "A"),
        "fandoms": fields.get("fandoms", []),
        "adapter": fields.get("adapter", "ffn"),
        "format": fields.get("format", "epub"),
        "confidence": "high",
        "chapter_count": 1,
    }
    entry.update(fields)
    lib.setdefault("stories", {})[url] = entry


def _make(tmp_path: Path, rel: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    return p


def test_plan_with_conflicts_returns_collisions(tmp_path: Path):
    """Two distinct indexed stories that render to the same path land
    in conflicts, not moves."""
    root = tmp_path
    idx_path = tmp_path / "idx.json"
    idx = LibraryIndex.load(idx_path)
    _make(tmp_path, "old1/Tale.epub")
    _make(tmp_path, "old2/Tale.epub")
    _seed_entry(idx, root, "https://x/1", "old1/Tale.epub",
                title="Tale", author="Author", fandoms=["F"])
    _seed_entry(idx, root, "https://x/2", "old2/Tale.epub",
                title="Tale", author="Author", fandoms=["F"])
    idx.save()

    result = plan_with_conflicts(root, index_path=idx_path)
    # Same title+author+fandom under the default template means both
    # planned moves resolve to the same target path.
    assert result.conflicts, (
        f"expected at least one conflict, got moves={result.moves}"
    )
    # Neither colliding op should appear in the safe-to-apply list.
    assert len(result.moves) == 0


def test_plan_with_conflicts_passes_non_colliding_through(tmp_path: Path):
    """Distinct targets show up in moves as normal."""
    root = tmp_path
    idx_path = tmp_path / "idx.json"
    idx = LibraryIndex.load(idx_path)
    _make(tmp_path, "wrong/A.epub")
    _make(tmp_path, "wrong/B.epub")
    _seed_entry(idx, root, "https://x/1", "wrong/A.epub",
                title="A", author="Author", fandoms=["F"])
    _seed_entry(idx, root, "https://x/2", "wrong/B.epub",
                title="B", author="Author", fandoms=["F"])
    idx.save()

    result = plan_with_conflicts(root, index_path=idx_path)
    assert not result.conflicts
    assert len(result.moves) == 2


def test_plan_backcompat_returns_only_moves(tmp_path: Path):
    """The legacy plan() shape: list of MoveOps. Callers that haven't
    migrated to plan_with_conflicts() still work but lose visibility
    into colliding moves (they'd be silently skipped at apply time)."""
    root = tmp_path
    idx_path = tmp_path / "idx.json"
    idx = LibraryIndex.load(idx_path)
    _make(tmp_path, "wrong/A.epub")
    _seed_entry(idx, root, "https://x/1", "wrong/A.epub",
                title="A", author="Author", fandoms=["F"])
    idx.save()

    moves = plan(root, index_path=idx_path)
    assert isinstance(moves, list)
    assert len(moves) == 1
