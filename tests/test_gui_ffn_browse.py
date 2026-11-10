"""Drive FFN search navigation with real wx controls and a fake network, including history and native pagination."""

import json
import os
import queue
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")
if not os.environ.get("DISPLAY"):
    pytest.skip("FFN browser tests need xvfb-run or a display.", allow_module_level=True)

from ficary import search
from ficary.gui_search import SearchFrame, _ffn_search_spec


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def browser(wx_app, monkeypatch):
    """Use a real parent window, but keep preferences and downloads away from the user's library."""
    parent = wx.Frame(None)
    parent._downloading = False
    parent._global_busy = False
    parent.messages = []
    settings = {}
    parent.prefs = SimpleNamespace(get=settings.get, set=lambda key, value: settings.__setitem__(key, value))
    parent._log = parent.messages.append
    parent._notify_search_frame_closed = lambda key: None
    frame = SearchFrame(parent, "ffn", _ffn_search_spec())
    errors = []
    monkeypatch.setattr(frame, "_show_search_error", errors.append)
    # Dispatch this worker's callbacks on the main thread without pumping unrelated callbacks left by other GUI tests.
    callbacks = queue.Queue()
    monkeypatch.setattr(wx, "CallAfter", lambda function, *args, **kwargs: callbacks.put((function, args, kwargs)))

    def busy(value, **kwargs):
        parent._downloading = value
        frame.apply_busy(value)

    parent._set_busy = busy

    def fetch(url):
        if "/communities/book/Harry-Potter/" in url:
            name = "ffn_communities.html"
        elif "/community/" in url:
            # These two rows are a whole final page in this fake; real fixture links are stripped below.
            name = "ffn_community_stories.html"
        elif "/search/" in url and "type=community" in url:
            name = "ffn_community_search.html"
        elif url == "https://www.fanfiction.net/book/":
            return (FIXTURES / "ffn_fandom_directory.html").read_text()
        else:
            return '<div class="z-list"><a class="stitle" href="/s/7/1/Story">Story</a></div>'
        from bs4 import BeautifulSoup
        soup = BeautifulSoup((FIXTURES / name).read_text(), "lxml")
        for link in soup.find_all("a", string="Next »"):
            link.decompose()
        return str(soup)

    monkeypatch.setattr(search, "_fetch_search_page", fetch)

    def finish():
        deadline = time.monotonic() + 5
        while parent._downloading and time.monotonic() < deadline:
            try:
                function, args, kwargs = callbacks.get(timeout=0.01)
            except queue.Empty:
                continue
            function(*args, **kwargs)
        assert not parent._downloading, "FFN search did not release its busy state"
        assert not errors, errors

    yield frame, finish, settings
    finish()
    frame.Destroy()
    parent.Destroy()


def choose_mode(frame, mode):
    frame.ffn_mode_ctrl.SetStringSelection(mode)
    frame._on_ffn_mode(None)


def select_row(frame, index):
    previous = frame.results_ctrl.GetFirstSelected()
    if previous >= 0:
        frame.results_ctrl.Select(previous, False)
    frame.results_ctrl.Select(index)
    frame.results_ctrl.Focus(index)
    frame.apply_busy(False)


def test_categories_open_fandoms_and_back_restores_selection_and_filters(browser):
    frame, finish, _ = browser
    frame.filter_ctrls["status"].SetStringSelection("complete")
    frame.filter_ctrls["sort"].SetStringSelection("favorites")
    choose_mode(frame, "Categories")
    frame._on_search()
    finish()
    assert len(frame.results) == 9
    books = next(index for index, row in enumerate(frame.results) if row["category"] == "book")
    select_row(frame, books)
    assert frame.ffn_open_btn.IsEnabled()
    assert not frame.search_dl_btn.IsEnabled()
    frame._on_result_activated()
    finish()
    assert frame.filter_ctrls["category"].GetStringSelection() == "book"
    assert {row["title"] for row in frame.results} == {"Harry Potter", "A Knight's Honor"}
    assert not frame.load_more_btn.IsEnabled()
    harry = next(index for index, row in enumerate(frame.results) if row["title"] == "Harry Potter")
    select_row(frame, harry)
    frame._on_result_activated()
    finish()
    assert frame.ffn_mode_ctrl.GetStringSelection() == "Stories"
    assert frame.last_filters["sort"] == "favorites"
    assert frame.last_filters["status"] == "complete"
    assert frame.last_filters["fandom_path"] == "/book/Harry-Potter/"
    frame._on_ffn_back(None)
    assert frame.ffn_mode_ctrl.GetStringSelection() == "Categories"
    assert frame.results_ctrl.GetFirstSelected() == harry
    assert frame.results[harry]["title"] == "Harry Potter"
    assert not frame.search_dl_btn.IsEnabled()
    frame._on_ffn_back(None)
    assert frame.results_ctrl.GetFirstSelected() == books
    assert frame.results[books]["category"] == "book"
    assert not frame.ffn_back_btn.IsEnabled()


def test_opening_community_uses_story_sorts_and_excludes_unsupported_filters(browser):
    frame, finish, _ = browser
    frame.filter_ctrls["status"].SetStringSelection("complete")
    frame.filter_ctrls["language"].SetStringSelection("english")
    frame.text_ctrls["characters"].ChangeValue("Harry P.")
    choose_mode(frame, "Communities")
    assert frame.text_ctrls["fandom"].IsEnabled()
    frame.filter_ctrls["category"].SetStringSelection("book")
    frame._refresh_ffn_controls()
    assert frame.text_ctrls["fandom"].IsEnabled()
    frame.text_ctrls["fandom"].ChangeValue("Harry Potter")
    frame._refresh_ffn_controls()
    frame.filter_ctrls["sort"].SetStringSelection("stories")
    frame._on_search()
    finish()
    assert frame.results_ctrl.GetColumn(1).GetText() == "Stories"
    assert frame.results_ctrl.GetColumn(2).GetText() == "Followers"
    assert frame.last_filters["language"] == "english"
    assert "characters" not in frame.last_filters
    select_row(frame, 1)
    frame._on_result_activated()
    finish()
    assert frame.ffn_mode_ctrl.GetStringSelection() == "Stories"
    assert frame.filter_ctrls["sort"].GetStringSelection() == "archive date"
    assert "language" not in frame.last_filters
    assert "characters" not in frame.last_filters
    assert frame.last_filters["status"] == "complete"
    assert frame.results_ctrl.GetColumn(0).GetText() == "Title"
    assert frame.search_dl_btn.IsEnabled()
    frame.results_ctrl.CheckItem(0, True)
    frame._on_ffn_back(None)
    assert frame.filter_ctrls["sort"].GetStringSelection() == "stories"
    assert frame.results_ctrl.GetFirstSelected() == 1
    assert frame._checked_rows() == []
    assert frame.results_ctrl.GetColumn(0).GetText() == "Name"
    assert not frame.search_dl_btn.IsEnabled()


def test_keyword_search_offers_only_native_search_sorts_and_retains_query_on_back(browser):
    frame, finish, _ = browser
    choose_mode(frame, "Communities")
    frame.query_ctrl.ChangeValue("harry")
    frame._refresh_ffn_controls()
    assert list(frame.filter_ctrls["sort"].GetItems()) == ["best match", "newest"]
    frame.filter_ctrls["sort"].SetStringSelection("newest")
    frame._on_search()
    finish()
    select_row(frame, 1)
    frame._on_result_activated()
    finish()
    assert frame.query_ctrl.GetValue() == ""
    frame._on_ffn_back(None)
    assert frame.query_ctrl.GetValue() == "harry"
    assert frame.filter_ctrls["sort"].GetStringSelection() == "newest"
    assert frame.results_ctrl.GetFirstSelected() == 1


def test_community_load_more_keeps_scope_sort_and_checked_stories(browser, monkeypatch):
    frame, finish, _ = browser
    from ficary import ffn_browse
    calls = []

    def story_page(query, *, community, page, **filters):
        calls.append((community, page, filters["sort"]))
        count = 25 if page == 1 else 1
        return search.SearchPage([{"title": f"Story {page}-{index}", "url": f"https://www.fanfiction.net/s/{page * 100 + index}/1/Story"} for index in range(count)], exhausted=page == 2)

    monkeypatch.setattr(ffn_browse, "search_community_stories", story_page)
    frame.text_ctrls["community"].ChangeValue("https://www.fanfiction.net/community/Example/123/")
    frame._refresh_ffn_controls()
    frame.filter_ctrls["sort"].SetStringSelection("favorites")
    frame._on_search()
    finish()
    frame.results_ctrl.CheckItem(3, True)
    assert frame.load_more_btn.IsEnabled()
    frame._on_load_more()
    finish()
    assert calls == [("https://www.fanfiction.net/community/Example/123/", 1, "favorites"), ("https://www.fanfiction.net/community/Example/123/", 2, "favorites")]
    assert len(frame.results) == 26
    assert frame._checked_rows() == [3]
    assert not frame.load_more_btn.IsEnabled()


def test_browse_state_persists_without_saving_search_query(browser):
    frame, _, settings = browser
    choose_mode(frame, "Communities")
    frame.filter_ctrls["category"].SetStringSelection("book")
    frame.text_ctrls["fandom"].ChangeValue("Harry Potter")
    frame._refresh_ffn_controls()
    frame.filter_ctrls["sort"].SetStringSelection("followers")
    frame.save_state()
    state = json.loads(settings["search_state_ffn"])
    assert "query" not in state
    replacement = SearchFrame(frame.main_frame, "ffn", _ffn_search_spec())
    try:
        assert replacement.ffn_mode_ctrl.GetStringSelection() == "Communities"
        assert replacement.text_ctrls["fandom"].GetValue() == "Harry Potter"
        assert replacement.filter_ctrls["sort"].GetStringSelection() == "followers"
        assert not replacement.text_ctrls["characters"].IsEnabled()
    finally:
        replacement.Destroy()


def test_back_restores_checked_stories_and_story_sort_across_directories(browser):
    frame, finish, _ = browser
    frame.filter_ctrls["sort"].SetStringSelection("favorites")
    frame.text_ctrls["community"].ChangeValue("https://www.fanfiction.net/community/Example/123/")
    frame._refresh_ffn_controls()
    assert frame.filter_ctrls["sort"].GetStringSelection() == "favorites"
    frame._on_search()
    finish()
    frame.results_ctrl.CheckItem(1, True)
    select_row(frame, 1)
    choose_mode(frame, "Categories")
    frame._on_search()
    finish()
    assert frame._checked_rows() == []
    frame._on_ffn_back(None)
    assert frame._checked_rows() == [1]
    assert frame.results_ctrl.GetFirstSelected() == 1
    assert frame.last_filters["community"].endswith("/Example/123/")
    assert frame.filter_ctrls["sort"].GetStringSelection() == "favorites"
    assert not frame.load_more_btn.IsEnabled()


@pytest.mark.parametrize("mode,columns", [("Categories", ["Name", "Stories"]), ("Communities", ["Name", "Stories", "Followers", "Created", "Founder", "Staff"])])
def test_empty_browse_results_keep_their_directory_columns(browser, monkeypatch, mode, columns):
    frame, finish, _ = browser
    choose_mode(frame, mode)
    frame.filter_ctrls["category"].SetStringSelection("book")
    frame.query_ctrl.ChangeValue("no matching entries")
    frame._refresh_ffn_controls()
    if mode == "Communities":
        monkeypatch.setattr(search, "_fetch_search_page", lambda url: "<title>Search | FanFiction</title>")
    frame._on_search()
    finish()
    assert frame.results == []
    assert [frame.results_ctrl.GetColumn(index).GetText() for index in range(frame.results_ctrl.GetColumnCount())] == columns
    assert not frame.load_more_btn.IsEnabled()
    assert not frame.search_dl_btn.IsEnabled()


def test_reopening_community_browse_restores_visible_scope_and_can_clear_it(browser):
    frame, _, settings = browser
    frame.text_ctrls["community"].ChangeValue("https://www.fanfiction.net/community/Example/123/")
    frame._refresh_ffn_controls()
    frame.filter_ctrls["sort"].SetStringSelection("favorites")
    frame.save_state()
    replacement = SearchFrame(frame.main_frame, "ffn", _ffn_search_spec())
    try:
        assert replacement.text_ctrls["community"].GetValue().endswith("/Example/123/")
        assert "Example" in replacement.ffn_scope_ctrl.GetLabel()
        assert not replacement.text_ctrls["fandom"].IsEnabled()
        assert not replacement.filter_ctrls["category"].IsEnabled()
        replacement.text_ctrls["community"].SetValue("")
        replacement._refresh_ffn_controls()
        assert replacement.text_ctrls["fandom"].IsEnabled()
        assert replacement.filter_ctrls["category"].IsEnabled()
        assert replacement._collect_filters().get("community") is None
    finally:
        replacement.Destroy()
