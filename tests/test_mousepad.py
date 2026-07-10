"""The Mousepad (Tapatalk forum) adapter: search, scraper, date sort.

Every mobiquo response here is a hand-built struct using real
``xmlrpc.client`` wrapper types (``Binary`` / ``DateTime``) so the
decode path gets exercised the same way live responses do. No test
touches the network: ``mobiquo_call`` is monkeypatched at both of its
import sites (``ficary.erotica.search`` calls through the module,
``ficary.erotica.mousepad`` binds the name directly).
"""

import xmlrpc.client

import pytest

from ficary.erotica import mousepad as mp
from ficary.erotica import search as es
from ficary.erotica import tapatalk as tt
from ficary.erotica.mousepad import MousepadScraper
from ficary.erotica.search import (
    SparsePage,
    erotica_sort_mode,
    search_erotica,
    search_mousepad,
    sort_rows_by_updated,
)


def B(text: str) -> xmlrpc.client.Binary:
    return xmlrpc.client.Binary(text.encode("utf-8"))


def DT(compact: str) -> xmlrpc.client.DateTime:
    return xmlrpc.client.DateTime(compact)


def topic_row(tid: str, title: str, author: str, when: str,
              teaser: str = "") -> dict:
    return {
        "topic_id": B(tid),
        "topic_title": B(title),
        "topic_author_name": B(author),
        "short_content": B(teaser),
        "post_time": DT(when),
    }


def post(pid: str, author_id: str, author: str, html: str,
         when: str = "20260101T00:00:00+00:00") -> dict:
    return {
        "post_id": B(pid),
        "post_author_id": B(author_id),
        "post_author_name": B(author),
        "post_content": B(html),
        "post_time": DT(when),
    }


# ── tapatalk helpers ─────────────────────────────────────────────


def test_decode_value_unwraps_binary_and_none():
    assert tt.decode_value(B("héllo")) == "héllo"
    assert tt.decode_value(None) == ""
    assert tt.decode_value("plain") == "plain"
    assert tt.decode_value(42) == "42"


def test_iso_datetime_forms():
    assert tt.iso_datetime(DT("20260709T15:46:58")) == "2026-07-09T15:46:58"
    # tz suffix is dropped; the board is UTC-only so stamps stay comparable
    assert tt.iso_datetime("20260312T21:39:05+00:00") == "2026-03-12T21:39:05"
    assert tt.iso_datetime("20260312") == "2026-03-12"
    assert tt.iso_datetime("not a date") == ""
    assert tt.iso_datetime(None) == ""


def test_topic_url_shape():
    assert tt.topic_url("197281") == (
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=197281"
    )
    assert tt.topic_url(198068).endswith("t=198068")


# ── search adapter ───────────────────────────────────────────────


class FakeMobiquo:
    """Callable standing in for ``tapatalk.mobiquo_call``.

    ``listings[forum_id]`` is the forum's full topic list; get_topic
    windows it like the live server, including the clamp-to-tail
    behaviour on out-of-range offsets that the adapter must guard
    against.
    """

    def __init__(self, listings: dict):
        self.listings = listings
        self.calls: list[tuple] = []

    def __call__(self, method, *params):
        self.calls.append((method, *params))
        assert method == "get_topic"
        forum_id, start, end = params
        rows = self.listings[forum_id]
        if start >= len(rows):  # live server clamps instead of emptying
            window = rows[-2:]
        else:
            window = rows[start:end + 1]
        return {"total_topic_num": len(rows), "topics": window}


@pytest.fixture
def fake_board(monkeypatch):
    f72 = [
        topic_row("11", "Something about her", "Bardo",
                  "20260709T15:46:58", "great chapter"),
        topic_row("12", "Her Bitch", "BenjaminSnoppe", "20260708T10:00:00"),
        topic_row("13", "Quiet Connections", "PedroTheVisitor",
                  "20260707T09:00:00", "loved the footdom scene"),
    ]
    f97 = [
        topic_row("21", "Classic: The Duchess", "OldHand",
                  "20250101T12:00:00"),
    ]
    fake = FakeMobiquo({"72": f72, "97": f97})
    monkeypatch.setattr(tt, "mobiquo_call", fake)
    return fake


def test_browse_merges_story_forums_with_dates(fake_board):
    rows = search_mousepad("", page=1, tags=["feet"])
    assert [r["title"] for r in rows] == [
        "Something about her", "Her Bitch", "Quiet Connections",
        "Classic: The Duchess",
    ]
    first = rows[0]
    assert first["site"] == "mousepad"
    assert first["author"] == "Bardo"
    assert first["updated"] == "2026-07-09T15:46:58"
    assert first["url"].endswith("viewtopic.php?t=11")


def test_browse_windows_advance_per_page(fake_board):
    search_mousepad("", page=2, tags=["feet"])
    starts = [(call[1], call[2], call[3]) for call in fake_board.calls]
    assert starts == [
        ("72", tt.TOPIC_WINDOW, 2 * tt.TOPIC_WINDOW - 1),
        ("97", tt.TOPIC_WINDOW, 2 * tt.TOPIC_WINDOW - 1),
    ]


def test_out_of_range_window_is_exhausted_despite_server_clamp(fake_board):
    rows = search_mousepad("", page=2, tags=["feet"])
    # Both fixture forums are shorter than one window, so page 2 is
    # past the end; the server clamp hands back tail rows, and the
    # adapter must discard them (plain empty list → exhausted).
    assert rows == []
    assert not isinstance(rows, SparsePage)


def test_offtopic_tag_browse_returns_nothing_without_calls(fake_board):
    assert search_mousepad("", page=1, tags=["bdsm"]) == []
    assert fake_board.calls == []


def test_query_matches_title_author_and_teaser(fake_board):
    assert [r["topic_title"] for r in []] == []  # guard: fixture untouched
    by_title = search_mousepad("bitch", page=1)
    assert [r["title"] for r in by_title] == ["Her Bitch"]
    by_author = search_mousepad("bardo", page=1)
    assert [r["title"] for r in by_author] == ["Something about her"]
    by_teaser = search_mousepad("footdom", page=1)
    assert [r["title"] for r in by_teaser] == ["Quiet Connections"]


def test_dry_filtered_window_returns_sparse_page(fake_board):
    rows = search_mousepad("zzz-no-such-story", page=1)
    assert rows == []
    assert isinstance(rows, SparsePage)


# ── scraper ──────────────────────────────────────────────────────


def test_parse_story_id_accepts_all_pasted_forms():
    base = "https://www.tapatalk.com/groups/themousepad"
    assert MousepadScraper.parse_story_id(
        f"{base}/viewtopic.php?t=197281") == "197281"
    assert MousepadScraper.parse_story_id(
        f"{base}/viewtopic.php?f=72&t=197281") == "197281"
    assert MousepadScraper.parse_story_id(
        f"{base}/something-about-her-t197281.html") == "197281"
    assert MousepadScraper.parse_story_id(
        f"{base}/something-about-her-t197281-s40.html") == "197281"
    assert MousepadScraper.parse_story_id("197281") == "197281"
    with pytest.raises(ValueError):
        MousepadScraper.parse_story_id(
            "https://www.tapatalk.com/groups/othergroup/story-t1.html")


class FakeThreadServer:
    """get_thread windows over a canned post list, like the live API."""

    def __init__(self, posts, author_id="10", title="Fixture Story",
                 author="AuthorPerson"):
        self.posts = posts
        self.head = {
            "topic_title": B(title),
            "topic_author_id": B(author_id),
            "topic_author_name": B(author),
            "forum_name": B("Stories"),
            "total_post_num": len(posts),
        }
        self.calls = []

    def __call__(self, method, *params):
        self.calls.append((method, *params))
        assert method == "get_thread"
        _tid, start, end, _html = params
        return dict(self.head, posts=self.posts[start:end + 1])


INTERLEAVED = [
    post("1", "10", "AuthorPerson", "Chapter one prose.",
         "20260101T00:00:00"),
    post("2", "99", "SomeReader", "Wow, fucking amazing, write more!"),
    post("3", "10", "AuthorPerson", "Chapter two prose."),
    post("4", "77", "OtherReader", "When is the next part?"),
    post("5", "10", "AuthorPerson", "Chapter three prose.",
         "20260315T08:30:00"),
]


@pytest.fixture
def scraper(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "mobiquo_call", FakeThreadServer(INTERLEAVED))
    return MousepadScraper(
        use_cache=False, delay_floor=0.0, delay_start=0.0,
    )


def test_download_cuts_out_non_author_posts(scraper):
    story = scraper.download("197281")
    assert story.title == "Fixture Story"
    assert story.author == "AuthorPerson"
    assert len(story.chapters) == 3
    assert [c.number for c in story.chapters] == [1, 2, 3]
    joined = " ".join(c.html for c in story.chapters)
    assert "Chapter two prose." in joined
    assert "fucking amazing" not in joined
    assert "next part" not in joined
    assert story.metadata["updated"] == "2026-03-15T08:30:00"
    assert story.metadata["total_posts"] == len(INTERLEAVED)
    assert story.summary == "Chapter one prose."


def test_download_honours_skip_and_spec(scraper):
    story = scraper.download("197281", skip_chapters=2)
    assert [c.number for c in story.chapters] == [3]
    story2 = scraper.download("197281", chapters=[(2, 2)])
    assert [c.number for c in story2.chapters] == [2]


def test_get_chapter_count_counts_author_posts(scraper):
    assert scraper.get_chapter_count("197281") == 3


def test_thread_walk_advances_windows(monkeypatch):
    many = [
        post(str(i), "10" if i % 2 else "99", "A", f"post {i}")
        for i in range(1, 121)
    ]
    server = FakeThreadServer(many)
    monkeypatch.setattr(mp, "mobiquo_call", server)
    s = MousepadScraper(use_cache=False, delay_floor=0.0, delay_start=0.0)
    story = s.download("5")
    offsets = [(c[2], c[3]) for c in server.calls]
    assert offsets == [(0, 49), (50, 99), (100, 149)]
    assert len(story.chapters) == 60  # odd-numbered posts are the author's


def test_download_all_comments_no_author_posts_raises(monkeypatch):
    lonely = [post("1", "99", "Reader", "first!")]
    server = FakeThreadServer(lonely, author_id="10")
    monkeypatch.setattr(mp, "mobiquo_call", server)
    s = MousepadScraper(use_cache=False, delay_floor=0.0, delay_start=0.0)
    with pytest.raises(ValueError):
        s.download("42")


# ── header lift / quote rendering / comment-reply gate ──────────


def test_lift_title_bold_and_hash_headers():
    t, body = mp._lift_title("<b>Author's Confession</b><br /><br />First …")
    assert t == "Author's Confession"
    assert body == "First …"
    t2, body2 = mp._lift_title("# Something About Her<br /><br />Charles …")
    assert t2 == "Something About Her"
    assert body2 == "Charles …"


def test_lift_title_leaves_non_headers_alone():
    long_bold = "<b>" + "x" * 120 + "</b><br />rest"
    assert mp._lift_title(long_bold) == ("", long_bold)
    mid = "She said <b>no</b><br />and left."
    assert mp._lift_title(mid) == ("", mid)
    stars = "**<br />Early in the morning …"
    assert mp._lift_title(stars) == ("", stars)


def test_render_quotes_to_blockquote_with_attribution():
    html = (
        '[quote uid=10796411 name="PretentiousOne" post=1312855]'
        "great story[/quote]<br />Thank you!"
    )
    out = mp._render_quotes(html)
    assert "[quote" not in out and "[/quote]" not in out
    assert "<blockquote><p><em>PretentiousOne wrote:</em></p>great story</blockquote>" in out
    nested = '[quote name="A"]outer [quote name="B"]inner[/quote] tail[/quote]'
    out2 = mp._render_quotes(nested)
    assert out2.count("<blockquote>") == 2
    assert "[quote" not in out2


def test_comment_reply_gate_needs_both_signals():
    a_id, a_name = "10796142", "Cassandra Main"
    reply = (
        '[quote uid=10796411 name="PretentiousOne" post=1312855]'
        "great story[/quote]Thank you for such feedback"
    )
    assert mp._is_comment_reply(reply, a_id, a_name) is True
    long_reply = reply + " " + "word " * 80
    assert mp._is_comment_reply(long_reply, a_id, a_name) is False
    self_quote = (
        f'[quote uid={a_id} name="{a_name}" post=1]her foot pressed[/quote]'
        "short continuation"
    )
    assert mp._is_comment_reply(self_quote, a_id, a_name) is False
    self_quote_by_name = (
        f'[quote uid=0 name="{a_name}"]her foot pressed[/quote]short'
    )
    assert mp._is_comment_reply(self_quote_by_name, a_id, a_name) is False
    no_quote = "Thanks everyone, next part on Friday!"
    assert mp._is_comment_reply(no_quote, a_id, a_name) is False


LABELED_THREAD = [
    post("1", "10", "AuthorPerson",
         "# The Opener<br /><br />" + "Long opening prose. " * 20),
    post("2", "99", "SomeReader", "Amazing, write more!"),
    post("3", "10", "AuthorPerson",
         '[quote uid=99 name="SomeReader" post=2]Amazing, write more!'
         "[/quote]<br />Thank you so much, glad you liked it!"),
    post("4", "10", "AuthorPerson",
         "<b>Taking a break</b><br /><br />" + "Interlude prose. " * 20),
]


def test_download_lifts_titles_and_skips_comment_replies(monkeypatch):
    monkeypatch.setattr(mp, "mobiquo_call", FakeThreadServer(LABELED_THREAD))
    s = MousepadScraper(use_cache=False, delay_floor=0.0, delay_start=0.0)
    story = s.download("77")
    assert [(c.number, c.title) for c in story.chapters] == [
        (1, "The Opener"), (2, "Taking a break"),
    ]
    assert "# The Opener" not in story.chapters[0].html
    assert story.summary.startswith("Long opening prose.")
    skipped = story.metadata["skipped_posts"]
    assert [e["post_id"] for e in skipped] == ["3"]
    assert "Thank you so much" in skipped[0]["preview"]
    joined = " ".join(c.html for c in story.chapters)
    assert "glad you liked it" not in joined
    assert s.get_chapter_count("77") == 2


# ── date sort ────────────────────────────────────────────────────


def test_sort_rows_by_updated_orders_and_keeps_undated_last():
    rows = [
        {"title": "a", "site": "aff"},
        {"title": "old", "site": "mousepad", "updated": "2025-01-01T00:00:00"},
        {"title": "b", "site": "nifty"},
        {"title": "new", "site": "mousepad", "updated": "2026-07-09T12:00:00"},
    ]
    ordered = sort_rows_by_updated(rows)
    assert [r["title"] for r in ordered] == ["new", "old", "a", "b"]


def test_erotica_sort_mode_accepts_labels_and_bare_modes():
    assert erotica_sort_mode("Newest first") == "date"
    assert erotica_sort_mode("date") == "date"
    assert erotica_sort_mode("Site & title") == "site"
    assert erotica_sort_mode("") == "site"
    assert erotica_sort_mode(None) == "site"
    assert erotica_sort_mode("bogus") == "site"


def test_search_erotica_date_sort_and_sparse_page(monkeypatch):
    dated = [
        {"title": "old forum", "url": "u1", "site": "mousepad",
         "updated": "2025-05-05T00:00:00"},
        {"title": "new forum", "url": "u2", "site": "mousepad",
         "updated": "2026-07-01T00:00:00"},
    ]
    undated = [{"title": "archive hit", "url": "u3", "site": "aff"}]
    fns = {
        "mousepad": lambda q, **kw: list(dated),
        "aff": lambda q, **kw: list(undated),
        "nifty": lambda q, **kw: SparsePage(),
        "greatfeet": lambda q, **kw: [],
    }
    monkeypatch.setattr(es, "_SITE_FNS", fns)

    res = search_erotica(
        "x", sites=["mousepad", "aff", "nifty", "greatfeet"],
        sort="Newest first",
    )
    assert [r["title"] for r in res] == [
        "new forum", "old forum", "archive hit",
    ]
    # plain [] exhausts; SparsePage stays eligible and flags the batch
    assert "greatfeet" in res.exhausted_sites
    assert "nifty" not in res.exhausted_sites
    assert res.more_available is True

    default = search_erotica(
        "x", sites=["mousepad", "aff", "nifty", "greatfeet"],
    )
    assert [r["title"] for r in default] == [
        "archive hit", "new forum", "old forum",
    ]


def test_mousepad_registered_everywhere():
    assert "mousepad" in es.EROTICA_SITE_SLUGS
    assert "mousepad" in es._SITE_FNS
    assert "mousepad" in es.EROTICA_SITE_LABELS
    assert "mousepad" in es.TAG_SITE_COVERAGE["feet"]
    assert "mousepad" in es.TAG_SITE_COVERAGE["femdom"]

    from ficary.sites import detect_scraper
    cls = detect_scraper(
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=1",
    )
    assert cls is MousepadScraper
