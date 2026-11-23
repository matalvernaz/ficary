"""Tests that a search window opens whatever shape its saved settings are in.

Each search window remembers its filters between runs as a small JSON object in the preferences. The loader checked that the outer value was an object and then trusted the three sections inside it. A section of the wrong type (a list or a string where an object belongs) raised from the window's constructor, so that site's search window could not be opened again until the preference was removed by hand. Settings written by a different version, or edited, are how a section ends up the wrong type.
"""

from __future__ import annotations

import os

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "search window tests need a display (run under xvfb-run -a)",
        allow_module_level=True,
    )

from ficary import prefs as prefs_module  # noqa: E402


def open_search_window(monkeypatch, site_key: str, saved_state: str):
    """Open the search window for ``site_key`` as if ``saved_state`` were what the preferences held for it. Returns (main frame, search window)."""
    from ficary.gui import MainFrame
    from ficary import gui_search

    spec_for_site = {
        "ffn": gui_search._ffn_search_spec,
        "royalroad": gui_search._royalroad_search_spec,
    }
    preference_key = gui_search.SearchFrame._PREF_KEY_BY_SITE[site_key]
    frame = MainFrame()
    real_get = frame.prefs.get

    def get(key, default=None):
        if key == preference_key:
            return saved_state
        return real_get(key, default)

    monkeypatch.setattr(frame.prefs, "get", get)
    try:
        window = gui_search.SearchFrame(frame, site_key, spec_for_site[site_key]())
    except Exception:
        frame.Destroy()
        raise
    return frame, window


@pytest.mark.parametrize(
    "saved_state",
    [
        '{"filters": ["status", "ongoing"]}',
        '{"filters": "ongoing"}',
        '{"filters": 7}',
        '{"text": ["a", "b"]}',
        '{"text": "words"}',
        '{"checks": [true, false]}',
        '{"checks": "yes"}',
        '{"filters": null, "text": null, "checks": null}',
        '{"filters": {"status": ["ongoing"]}, "text": {"query": 5}, "checks": {"x": "y"}}',
        '[]',
        '"just a string"',
        '{not json',
    ],
)
@pytest.mark.parametrize("site_key", ["royalroad", "ffn"])
def test_the_window_opens_whatever_was_saved(wx_app, monkeypatch, site_key, saved_state):
    frame, window = open_search_window(monkeypatch, site_key, saved_state)
    try:
        assert window.filter_ctrls
    finally:
        window.Destroy()
        frame.Destroy()


def test_good_sections_are_still_restored_beside_a_bad_one(wx_app, monkeypatch):
    """One damaged section must not cost the ones that are fine."""
    frame, window = open_search_window(
        monkeypatch, "royalroad",
        '{"filters": {"status": "ongoing"}, "text": ["damaged"], "checks": 3}',
    )
    try:
        assert window.filter_ctrls["status"].GetStringSelection() == "ongoing"
    finally:
        window.Destroy()
        frame.Destroy()


def test_the_preference_keys_under_test_are_the_real_ones():
    """Guards the parametrised tests above against testing a key no window reads."""
    from ficary import gui_search

    assert gui_search.SearchFrame._PREF_KEY_BY_SITE["royalroad"] == prefs_module.KEY_SEARCH_STATE_ROYALROAD
    assert gui_search.SearchFrame._PREF_KEY_BY_SITE["ffn"] == prefs_module.KEY_SEARCH_STATE_FFN
