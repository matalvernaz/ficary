"""Tests for AO3 session-cookie auth and the extended search filters
(Archive Warnings, title, creator). No network."""

import pytest

from ffn_dl.ao3 import AO3LockedError, AO3Scraper
from ffn_dl.search import _build_ao3_search_url

_LOGIN_REQUIRED_HTML = (
    "<html><body><p>Sorry, you don't have permission to access this "
    "page. Users must be logged in to access this work.</p></body></html>"
)


def _scraper(**kwargs):
    kwargs.setdefault("use_cache", False)
    return AO3Scraper(**kwargs)


class TestCookieAuth:
    def test_anonymous_has_no_auth(self):
        assert _scraper().has_auth is False

    def test_cookie_seeds_session(self):
        s = _scraper(session_cookie="_otwarchive_session=abc; user_credentials=1")
        names = {c.name for c in s.session.cookies.jar}
        assert s.has_auth is True
        assert "_otwarchive_session" in names and "user_credentials" in names

    def test_worker_session_reseeded(self):
        s = _scraper(session_cookie="_otwarchive_session=abc")
        worker = s._new_session()
        assert any(c.name == "_otwarchive_session" for c in worker.cookies.jar)


class TestLockedErrorHint:
    def test_anonymous_hint_points_at_cookie_flag(self):
        s = _scraper()
        with pytest.raises(AO3LockedError, match="--ao3-cookie"):
            s._check_for_blocks(_LOGIN_REQUIRED_HTML)

    def test_authed_hint_says_cookie_may_be_stale(self):
        s = _scraper(session_cookie="_otwarchive_session=abc")
        with pytest.raises(AO3LockedError, match="expired"):
            s._check_for_blocks(_LOGIN_REQUIRED_HTML)


class TestSearchDepth:
    def test_warning_id_in_url(self):
        url = _build_ao3_search_url("", {"warning": "major character death"})
        assert "work_search%5Barchive_warning_ids%5D%5B%5D=18" in url

    def test_warning_by_numeric_id(self):
        url = _build_ao3_search_url("", {"warning": "17"})
        assert "archive_warning_ids%5D%5B%5D=17" in url

    def test_title_and_creator(self):
        url = _build_ao3_search_url("", {"title": "Dawn", "creator": "astolat"})
        assert "work_search%5Btitle%5D=Dawn" in url
        assert "work_search%5Bcreators%5D=astolat" in url

    def test_unknown_warning_raises(self):
        with pytest.raises(ValueError, match="Unknown warning"):
            _build_ao3_search_url("", {"warning": "mild peril"})
