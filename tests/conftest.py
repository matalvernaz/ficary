"""Pytest fixtures: load saved HTML samples once per session."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def wx_app():
    """A single ``wx.App`` shared by every GUI test in the session.

    wxPython doesn't tolerate constructing a second ``wx.App`` in the same
    process — the GTK signal table from the first survives Destroy() and
    segfaults the next App's init — so the app lives here, in conftest, and
    every GUI test file (smoke, browser, ...) shares this one rather than
    each making its own. Tests rely on per-test ``frame.Destroy()`` to keep
    handler tables clean.

    ``wx`` is imported lazily inside the fixture so a wx-less / display-less
    environment (plain CI) still collects and runs the non-GUI suite; the
    GUI test modules skip themselves at import time in that case, so this
    fixture is only ever instantiated when wx and a display are present.
    """
    import wx

    app = wx.App(False)
    yield app
    # Don't ``app.Destroy()`` here — pytest-finalize ordering can call this
    # after a frame fixture's teardown already tore the GTK loop down,
    # segfaulting interpreter shutdown.


@pytest.fixture(scope="session")
def ao3_work_full_html():
    return _load("ao3_work_full.html")


@pytest.fixture(scope="session")
def ao3_work_bare_html():
    return _load("ao3_work_bare.html")


@pytest.fixture(scope="session")
def ao3_series_html():
    return _load("ao3_series.html")


@pytest.fixture(scope="session")
def ao3_search_html():
    return _load("ao3_search.html")


@pytest.fixture(scope="session")
def ffn_story_html():
    return _load("ffn_story.html")


@pytest.fixture(scope="session")
def ffn_story_not_found_html():
    return _load("ffn_story_not_found.html")


@pytest.fixture(scope="session")
def ffn_search_html():
    return _load("ffn_search.html")


@pytest.fixture(scope="session")
def ficwad_story_html():
    return _load("ficwad_story.html")


@pytest.fixture(scope="session")
def ficwad_chapter_view_html():
    return _load("ficwad_chapter_view.html")
