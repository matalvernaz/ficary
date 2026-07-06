"""Separate adult-library root: download routing + index removal.

These are pure-logic tests (no wx), so they run in the plain CI job too —
the routing decision that sends erotica to its own library root, and the
single-story index removal the browser's Delete action relies on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ficary import cli
from ficary.library.index import SCHEMA_VERSION, LibraryIndex
from ficary.models import Story

_LIT = "https://www.literotica.com/s/some-slug"
_FFN = "https://www.fanfiction.net/s/123/1/"


def _story(url: str) -> Story:
    return Story(id=1, title="T", author="A", summary="", url=url)


def _args(adult_path: str = "", autosort: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _library_autosort=autosort, _library_adult_path=adult_path,
    )


def test_adult_url_routes_to_separate_root():
    dest = cli._adult_root_override(_story(_LIT), _args("/adult"))
    assert dest == Path("/adult")


def test_non_adult_url_falls_through():
    # An FFN download must NOT be hijacked to the adult root even when one
    # is configured — it goes through normal fandom routing.
    assert cli._adult_root_override(_story(_FFN), _args("/adult")) is None


def test_no_adult_path_falls_through():
    # No separate root set → fall back to the in-library Adult subfolder.
    assert cli._adult_root_override(_story(_LIT), _args("")) is None


def test_autosort_off_disables_override():
    # A one-off download to a custom folder (autosort off) is respected.
    assert cli._adult_root_override(
        _story(_LIT), _args("/adult", autosort=False),
    ) is None


def test_adult_path_expands_user():
    dest = cli._adult_root_override(_story(_LIT), _args("~/adult"))
    assert dest == Path("~/adult").expanduser()


def _index_with_story(tmp_path: Path) -> LibraryIndex:
    data = {
        "version": SCHEMA_VERSION,
        "libraries": {
            str(tmp_path): {
                "stories": {_LIT: {"relpath": "a.epub", "title": "X"}},
                "untrackable": [],
            }
        },
    }
    return LibraryIndex(tmp_path / "idx.json", data)


def test_index_remove_existing(tmp_path):
    idx = _index_with_story(tmp_path)
    assert idx.remove(tmp_path, _LIT) is True
    assert list(idx.stories_in(tmp_path)) == []


def test_index_remove_missing_returns_false(tmp_path):
    idx = _index_with_story(tmp_path)
    assert idx.remove(tmp_path, "https://example.com/nope") is False
    # The real story is untouched.
    assert len(list(idx.stories_in(tmp_path))) == 1
