"""A StoriesOnline cookie (Matt, 2026-11-24).

StoriesOnline shows only the start of a story to someone not signed in,
and Ficary could not sign in there, so every StoriesOnline book stopped
at "There is more of this story...". A Cookie header from a browser
signed in to an account (the site says a free one is enough) now goes
the same way as the ScribbleHub and SubscribeStar ones: Preferences >
Downloads, the command line, the environment, and the saved settings
that library updates and the watchlist use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from ficary import cli
from ficary import prefs as prefs_module
from ficary.erotica import StoriesOnlineScraper

SOL_URL = "https://storiesonline.net/s/40467/ouroboros-dorm-dipping"
FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def jar(scraper) -> dict:
    return {cookie.name: (cookie.value, cookie.domain) for cookie in scraper.session.cookies.jar}


def test_the_scraper_signs_its_session_in_with_the_cookie():
    scraper = StoriesOnlineScraper(session_cookie="SOLSESSID=abc123; other=x", use_cache=False)
    assert scraper.has_auth
    cookies = jar(scraper)
    assert cookies["SOLSESSID"][0] == "abc123"
    assert cookies["SOLSESSID"][1].endswith("storiesonline.net")


def test_without_one_it_is_not_signed_in():
    assert not StoriesOnlineScraper(use_cache=False).has_auth


def test_still_only_the_free_part_with_a_cookie_says_it_may_have_run_out(caplog):
    page = (FIXTURES / "sol_story.html").read_text(encoding="utf-8", errors="replace")
    scraper = StoriesOnlineScraper(session_cookie="SOLSESSID=old", use_cache=False)
    scraper._fetch = lambda url, *args, **kwargs: page
    with caplog.at_level(logging.WARNING, logger="ficary.erotica.storiesonline"):
        scraper.download(SOL_URL)
    said = " ".join(record.getMessage() for record in caplog.records)
    assert "even with your StoriesOnline cookie" in said and "may have run out" in said


class TestTheCommandLine:
    def test_the_option_reaches_the_scraper(self):
        args = cli._build_parser().parse_args(["--storiesonline-cookie", "SOLSESSID=abc", SOL_URL])
        assert args.storiesonline_cookie == "SOLSESSID=abc"
        scraper = cli._build_scraper(SOL_URL, args)
        assert isinstance(scraper, StoriesOnlineScraper) and scraper.has_auth

    def test_the_environment_is_read_when_the_option_is_not_given(self, monkeypatch):
        monkeypatch.setenv("FICARY_STORIESONLINE_COOKIE", "SOLSESSID=from-env")
        args = cli._build_parser().parse_args([SOL_URL])
        assert cli._build_scraper(SOL_URL, args).has_auth

    def test_other_sites_are_not_given_it(self):
        args = cli._build_parser().parse_args(["--storiesonline-cookie", "SOLSESSID=abc", "https://www.fanfiction.net/s/1"])
        scraper = cli._build_scraper("https://www.fanfiction.net/s/1", args)
        assert not isinstance(scraper, StoriesOnlineScraper)


def test_unattended_downloads_use_the_saved_cookie(monkeypatch):
    """Library updates and the watchlist build their job from the settings."""
    from ficary import jobs

    saved = {prefs_module.KEY_STORIESONLINE_COOKIE: "SOLSESSID=saved"}
    monkeypatch.setattr(prefs_module.Prefs, "get", lambda self, key, default=None: saved.get(key, default))
    job = jobs.DownloadJob.from_prefs()
    assert job.storiesonline_cookie == "SOLSESSID=saved"
    assert cli._build_scraper(SOL_URL, job).has_auth


def test_the_setting_is_empty_by_default():
    assert prefs_module.DEFAULTS[prefs_module.KEY_STORIESONLINE_COOKIE] == ""


wx = pytest.importorskip("wx")
needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")


@needs_display
class TestTheWindow:
    def test_the_window_gives_it_to_a_storiesonline_scraper_only(self, wx_app):
        from ficary import gui

        frame = gui.MainFrame()
        try:
            assert frame._scraper_for(SOL_URL, storiesonline_cookie="SOLSESSID=abc").has_auth
            assert not isinstance(frame._scraper_for("https://www.fanfiction.net/s/1", storiesonline_cookie="SOLSESSID=abc"), StoriesOnlineScraper)
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_downloads_take_it_from_the_settings(self, wx_app, monkeypatch):
        from ficary import gui

        frame = gui.MainFrame()
        try:
            real_get = frame.prefs.get
            monkeypatch.setattr(frame.prefs, "get", lambda key, default=None: "SOLSESSID=saved" if key == prefs_module.KEY_STORIESONLINE_COOKIE else real_get(key, default))
            assert frame._snapshot_download_params().storiesonline_cookie == "SOLSESSID=saved"
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()

    def test_preferences_has_the_field_and_saves_it(self, wx_app):
        from ficary import gui
        from ficary.preferences import PreferencesDialog

        frame = gui.MainFrame()
        try:
            dlg = PreferencesDialog(frame, frame.prefs)
            try:
                assert dlg.storiesonline_cookie_ctrl.GetName() == "StoriesOnline cookie"
                dlg.storiesonline_cookie_ctrl.SetValue("  SOLSESSID=typed  ")
                dlg._save()
            finally:
                dlg.Destroy()
            assert frame.prefs.get(prefs_module.KEY_STORIESONLINE_COOKIE) == "SOLSESSID=typed"
        finally:
            frame._clip_timer.Stop()
            frame.Destroy()
            wx.SafeYield()
