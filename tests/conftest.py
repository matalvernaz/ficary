"""Pytest fixtures: load saved HTML samples once per session."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolate_mcstories_title_cache(tmp_path, monkeypatch):
    """Point the MCStories title-index cache at a per-test temp path.

    The cache is mirrored to the portable cache dir and loaded once per
    process, so without this a developer who has run a real search gets
    a pre-populated index — and different results — from CI's empty one.
    """
    import ficary.erotica.search as search

    monkeypatch.setattr(
        search, "_mcs_title_cache_path",
        lambda: tmp_path / "mcstories-titles.json",
    )
    monkeypatch.setattr(search, "_mcs_title_index_loaded", False)


@pytest.fixture(autouse=True)
def isolate_ficary_data(tmp_path_factory, monkeypatch):
    """Give every test its own Ficary data folder.

    Settings, caches, the library index and the watchlist all live under
    ``portable.portable_root()``, which is ``~/.ficary`` when running
    from source. Without this every test that built a window or saved a
    setting read and wrote the real settings file of whoever ran the
    tests, and a test of anything that happens at launch would have
    seen their real library. A test that wants a particular folder sets
    ``portable._cached_root`` itself, which overrides this.
    """
    from ficary import portable

    # Its own folder, not inside tmp_path: several tests check that
    # nothing but what they wrote is in tmp_path.
    data = tmp_path_factory.mktemp("ficary-data")
    monkeypatch.setattr(portable, "_cached_root", data)


@pytest.fixture(autouse=True)
def isolate_chapter_audio_cache(tmp_path, monkeypatch):
    """Keep every test's audiobook chapter cache in a per-test temp folder.

    A render makes a folder for its story in that cache before anything
    else, and ends by clearing old chapter audio out of it. Without this
    a test that runs a render does both to the real cache of whoever is
    running the tests.
    """
    import ficary.tts as tts

    monkeypatch.setattr(
        tts, "_chapter_audio_cache_base",
        lambda: tmp_path / "chapter-audio-cache",
    )


@pytest.fixture(autouse=True)
def no_update_check_at_launch(monkeypatch):
    """Stop the main window asking GitHub for the latest release.

    Every test that opens the main window used to make that request for
    real. GitHub answers 60 an hour without a sign-in, counted per
    internet address, so a day of test runs used them all up and the
    real updater on another computer behind the same router was told
    403. A test of the launch check itself can take this back off with
    ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("FICARY_SKIP_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def no_launch_questions(monkeypatch):
    """Keep the library questions a new main window asks at launch out of
    every test.

    They are modal dialogs shown from a wx.CallAfter, so any test that
    lets pending events run after building a window would sit on them.
    With each test's own empty data folder, the first-start question
    would come up every time. Tests of the questions call
    ``MainFrame._library_questions_at_start`` themselves through the
    original, which this fixture keeps as ``original_library_questions``.
    """
    try:
        import ficary.gui as gui
    except Exception:  # noqa: BLE001 - no wx here, so no window either
        return
    monkeypatch.setattr(gui.MainFrame, "original_library_questions", gui.MainFrame._library_questions_at_start, raising=False)
    monkeypatch.setattr(gui.MainFrame, "_library_questions_at_start", lambda self: None)
    # Likewise the check for new chapters at launch, which would ask
    # real sites about whatever library a test had set up.
    monkeypatch.setattr(gui.MainFrame, "original_story_check", gui.MainFrame._start_story_check_at_launch, raising=False)
    monkeypatch.setattr(gui.MainFrame, "_start_story_check_at_launch", lambda self: None)


@pytest.fixture(autouse=True)
def no_real_clipboard(monkeypatch):
    """Keep every test away from the system clipboard.

    Turning the clipboard watch on reads the clipboard straight away.
    Under Xvfb a clipboard read in the middle of a long run of window
    tests once ended the whole run with a bus error inside GTK. A test
    has no business reading or writing whatever the user last copied
    anyway. A test that needs clipboard text sets ``_get_clipboard``
    on its window itself.
    """
    try:
        import ficary.gui as gui
    except Exception:  # noqa: BLE001 - no wx here, so no clipboard either
        return
    monkeypatch.setattr(gui.MainFrame, "_get_clipboard", lambda self: "")


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
def ffn_chapter_not_found_html():
    """FFN's 200 "Chapter not found" message panel, captured 2026-09-16
    for a chapter number the same story's chapter menu listed."""
    return _load("ffn_chapter_not_found.html")


@pytest.fixture(scope="session")
def ffn_search_html():
    return _load("ffn_search.html")


@pytest.fixture(scope="session")
def ficwad_story_html():
    return _load("ficwad_story.html")


@pytest.fixture(scope="session")
def ficwad_chapter_view_html():
    return _load("ficwad_chapter_view.html")
