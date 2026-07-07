"""Release-notes extraction (tools/extract_changelog.py).

Guards the fix for the blank in-app changelog: the GitHub release body is
filled from CHANGELOG.md at tag time, and self_update.fetch_changelog_since
surfaces that body in the update dialog. Empty bodies were why every
release showed blank notes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = _REPO_ROOT / "tools" / "extract_changelog.py"
_spec = importlib.util.spec_from_file_location("extract_changelog", _MOD_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_section = _mod.extract_section


_SAMPLE = """# Changelog

## 2.9.0 — 2026-07-06

**New features**

* Library browser.

## 2.8.1 — 2026-07-06

**Bug fixes**

* The Download button works again.

## 2.8.0 — 2026-07-05

Audit round 11.
"""


def test_extracts_named_section_without_heading():
    out = extract_section(_SAMPLE, "2.9.0")
    assert out.startswith("**New features**")
    assert "Library browser." in out
    # Stops at the next version heading — no bleed-through.
    assert "Download button" not in out
    assert "## 2.8.1" not in out


def test_accepts_v_prefixed_tag():
    assert extract_section(_SAMPLE, "v2.8.1") == extract_section(_SAMPLE, "2.8.1")


def test_middle_section_bounded_both_ends():
    out = extract_section(_SAMPLE, "2.8.1")
    assert "Download button works again." in out
    assert "New features" not in out
    assert "Audit round 11" not in out


def test_last_section_runs_to_eof():
    assert extract_section(_SAMPLE, "2.8.0") == "Audit round 11."


def test_unknown_version_returns_none():
    assert extract_section(_SAMPLE, "9.9.9") is None


def test_partial_version_does_not_falsely_match():
    # "2.9" must not match "## 2.9.0"; only exact 3-part versions.
    assert extract_section(_SAMPLE, "2.9") is None


def test_real_changelog_top_section_has_content():
    # The actual repo CHANGELOG — the sections that were shipping blank.
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    out = extract_section(changelog, "2.9.0")
    assert out and "Library browser" in out
