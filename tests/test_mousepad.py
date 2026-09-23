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


def test_cli_erotica_search_spec_maps_flags():
    from ficary.cli import _build_parser, _build_search_spec

    args = _build_parser().parse_args([
        "--search", "", "--site", "erotica", "--tags", "feet,femdom",
        "--erotica-site", "mousepad", "--sort", "date",
    ])
    label, fn, filters = _build_search_spec(args)
    assert "mousepad" in label
    assert filters["tags"] == "feet,femdom"
    assert filters["sites"] == ["mousepad"]
    assert filters["sort"] == "date"
    assert fn is search_erotica


def test_cli_erotica_tag_browse_counts_as_search_mode():
    from ficary.cli import _build_parser, _is_search_mode

    with_tags = _build_parser().parse_args(
        ["--site", "erotica", "--tags", "feet"],
    )
    assert _is_search_mode(with_tags)
    # Tags on a non-erotica site must NOT flip into search mode.
    ffn = _build_parser().parse_args(["--site", "ffn", "--tags", "feet"])
    assert not _is_search_mode(ffn)


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


def test_single_site_scope_with_unclaimed_tag_browses_bare(monkeypatch):
    """A stale tag in the restored Tags box must not silently zero an
    explicit one-site browse — the tag gate is for fan-outs. The
    scoped site falls back to its bare listing."""
    calls = {}

    def fake_mousepad(query, *, page=1, tags=None, **_):
        calls["tags"] = tags
        return [{"title": "t", "url": "u", "site": "mousepad"}]

    monkeypatch.setitem(es._SITE_FNS, "mousepad", fake_mousepad)
    rows = search_erotica("", sites=["mousepad"], tags=["bdsm"])
    assert calls["tags"] == []
    assert len(rows) == 1
    # A tag the site DOES claim still passes through.
    search_erotica("", sites=["mousepad"], tags=["feet"])
    assert calls["tags"] == ["feet"]


MANGLED_THREAD = [
    # Mirrors live t=157450: the original first post was lost to a
    # mod split, so the API's post #1 is a READER comment and the
    # topic_author points at that reader — the story lives in a
    # different account's posts.
    post("1", "93", "rubbermac", "Wow, great start, many thanks!"),
    post("2", "104", "Corvinus",
         "Chapter 2 <br /><br />" + "Actual story prose here. " * 30),
    post("3", "104", "Corvinus",
         "Chapter 3 <br /><br />" + "More story prose follows. " * 30),
    post("4", "93", "rubbermac", "Fantastic story Corvinus, thanks!"),
]


def test_download_survives_thread_whose_starter_is_a_commenter(monkeypatch):
    server = FakeThreadServer(
        MANGLED_THREAD, author_id="93", author="rubbermac",
        title="Lost and found days",
    )
    monkeypatch.setattr(mp, "mobiquo_call", server)
    s = MousepadScraper(use_cache=False, delay_floor=0.0, delay_start=0.0)
    story = s.download("157450")
    assert story.author == "Corvinus"
    assert [c.title for c in story.chapters] == ["Chapter 2", "Chapter 3"]
    joined = " ".join(c.html for c in story.chapters)
    assert "Actual story prose" in joined
    assert "great start" not in joined
    assert "Fantastic story" not in joined
    assert s.get_chapter_count("157450") == 2


def test_short_commenter_never_hijacks_a_real_author(monkeypatch):
    normal = [
        post("1", "10", "AuthorPerson", "Story opener. " * 40),
        post("2", "99", "Chatty", "long comment words " * 30),
        post("3", "10", "AuthorPerson", "Second chapter. " * 40),
    ]
    monkeypatch.setattr(mp, "mobiquo_call", FakeThreadServer(normal))
    s = MousepadScraper(use_cache=False, delay_floor=0.0, delay_start=0.0)
    story = s.download("5")
    assert story.author == "AuthorPerson"
    assert len(story.chapters) == 2


def test_lift_title_plain_chapter_line():
    t, body = mp._lift_title("Chapter 12 <br /><br />She woke early …")
    assert t == "Chapter 12"
    assert body.startswith("She woke early")
    t2, _ = mp._lift_title("Part 3 - The Beach<br />prose")
    assert t2 == "Part 3 - The Beach"
    # A sentence merely starting with "Chapter" mid-flow isn't a title.
    prose = "Chapter after chapter she read on with no br until much later " * 3
    assert mp._lift_title(prose) == ("", prose)


# ── Forum sections: listing the board, listing a section ─────────


def forum_node(fid: str, name: str, desc: str = "", *,
               sub_only: bool = False, children: list | None = None) -> dict:
    """One node of a ``get_forum`` reply. Categories set ``sub_only``
    and hold no threads of their own."""
    return {
        "forum_id": B(fid),
        "forum_name": B(name),
        "description": B(desc),
        "sub_only": "true" if sub_only else "false",
        "child": children or [],
    }


# The board's real shape (verified live 2026-09-23): the fiction
# category holds Stories, which in turn nests Story Requests and the
# Classic Story Library; Experiences sits beside Stories. Everything
# outside the fiction category is off-topic and must not be listed.
BOARD_TREE = [
    forum_node("133", "Fetish", sub_only=True, children=[
        forum_node("66", "Foot Model Content"),
    ]),
    forum_node("135", "Stories", sub_only=True, children=[
        forum_node("72", "Stories", "Share your foot stories here.",
                   children=[
                       forum_node("94", "Story Requests", "Request old ones."),
                       forum_node("97", "Classic Story Library", "An archive."),
                   ]),
        forum_node("96", "Experiences and Anecdotes", "Quick accounts."),
    ]),
    forum_node("129", "International Pads", sub_only=True, children=[
        forum_node("74", "Das MausPad"),
    ]),
]


class FakeBoard:
    """``mobiquo_call`` stand-in answering get_forum and get_topic."""

    def __init__(self, tree, listings, *, forum_error: bool = False):
        self.tree = tree
        self.listings = listings
        self.forum_error = forum_error
        self.calls: list[tuple] = []

    def __call__(self, method, *params):
        self.calls.append((method, *params))
        if method == "get_forum":
            if self.forum_error:
                raise OSError("board unreachable")
            return self.tree
        assert method == "get_topic"
        forum_id, start, end = params
        rows = self.listings.get(forum_id, [])
        if start >= len(rows):  # live server clamps instead of emptying
            window = rows[-2:]
        else:
            window = rows[start:end + 1]
        return {
            "total_topic_num": len(rows),
            "forum_name": B(f"Forum {forum_id}"),
            "topics": window,
        }


@pytest.fixture
def fake_forum_board(monkeypatch):
    listings = {
        "72": [
            topic_row(str(100 + i), f"Story {i}", "Bardo",
                      "20260709T15:46:58", "teaser")
            for i in range(120)
        ],
        "94": [topic_row("300", "Looking for a story", "Asker",
                         "20260101T00:00:00")],
        "96": [topic_row("400", "At the beach", "Walker",
                         "20260101T00:00:00")],
        "97": [topic_row("500", "Classic: The Duchess", "OldHand",
                         "20250101T12:00:00")],
    }
    board = FakeBoard(BOARD_TREE, listings)
    monkeypatch.setattr(mp, "mobiquo_call", board)
    return board


@pytest.mark.parametrize(
    "url, expected_id",
    [
        # Permalink shape the address bar shows while browsing.
        ("https://www.tapatalk.com/groups/themousepad/stories-f72/", "72"),
        (
            "https://www.tapatalk.com/groups/themousepad/"
            "classic-story-library-f97/",
            "97",
        ),
        # phpBB's own section URL.
        ("https://www.tapatalk.com/groups/themousepad/viewforum.php?f=94", "94"),
        # The board front page names no section.
        ("https://www.tapatalk.com/groups/themousepad/", ""),
        ("https://www.tapatalk.com/groups/themousepad", ""),
    ],
)
def test_forum_urls_are_recognised(url, expected_id):
    assert MousepadScraper.is_forum_url(url) is True
    assert MousepadScraper.parse_forum_id(url) == expected_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198149",
        # A story link followed out of a section listing carries the
        # section id too. Treating this as a section would open the
        # picker instead of downloading the story the user clicked.
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?f=72&t=198149",
        "https://www.tapatalk.com/groups/themousepad/some-slug-t198149.html",
        "https://www.tapatalk.com/groups/themousepad/some-slug-t198149-s20.html",
    ],
)
def test_topic_urls_are_not_forum_urls(url):
    assert MousepadScraper.is_forum_url(url) is False
    # And they must still resolve as stories.
    assert MousepadScraper.parse_story_id(url) == "198149"


def test_story_forums_lists_the_fiction_sections_only(fake_forum_board):
    """Only the fiction category's sections are offered. The board also
    carries picture and non-English sections, which aren't stories."""
    forums = MousepadScraper.story_forums()
    assert [(f["id"], f["name"]) for f in forums] == [
        ("72", "Stories"),
        ("94", "Story Requests"),
        ("97", "Classic Story Library"),
        ("96", "Experiences and Anecdotes"),
    ]
    # Categories hold no threads and must not be offered as a download.
    assert "135" not in {f["id"] for f in forums}
    assert forums[0]["url"] == (
        "https://www.tapatalk.com/groups/themousepad/viewforum.php?f=72"
    )
    assert forums[0]["topics"] == 120
    assert forums[2]["topics"] == 1


def test_story_forums_can_skip_the_count_requests(fake_forum_board):
    forums = MousepadScraper.story_forums(with_counts=False)
    assert all(f["topics"] is None for f in forums)
    assert [c[0] for c in fake_forum_board.calls] == ["get_forum"]


def test_story_forums_falls_back_when_the_board_is_unreachable(monkeypatch):
    """An empty section list would read as "this board has no stories".
    A slightly stale list is the better lie, and the failure is said out
    loud rather than swallowed."""
    board = FakeBoard(BOARD_TREE, {}, forum_error=True)
    monkeypatch.setattr(mp, "mobiquo_call", board)
    said: list[str] = []
    forums = MousepadScraper.story_forums(
        with_counts=False, progress=said.append,
    )
    assert [f["id"] for f in forums] == ["72", "94", "97", "96"]
    assert any("Couldn't read the forum list" in line for line in said)


def test_scrape_forum_works_pages_through_the_whole_section(fake_forum_board):
    scraper = MousepadScraper()
    name, works = scraper.scrape_forum_works(
        "https://www.tapatalk.com/groups/themousepad/stories-f72/",
    )
    assert name == "Forum 72"
    assert len(works) == 120
    # Every thread exactly once: the live server clamps an out-of-range
    # window to the listing's tail, so a walk that trusted the server to
    # run dry would re-collect the last rows forever.
    assert len({w["url"] for w in works}) == 120
    assert works[0]["url"].endswith("viewtopic.php?t=100")
    assert works[0]["section"] == "Forum 72"
    assert works[0]["updated"] == "2026-07-09T15:46:58"
    # reply_number counts everyone's posts, not the author's, so the
    # picker must show no chapter count rather than a wrong one.
    assert works[0]["chapters"] == ""


def test_scrape_forum_works_reports_each_window(fake_forum_board):
    said: list[str] = []
    MousepadScraper().scrape_forum_works(
        "https://www.tapatalk.com/groups/themousepad/stories-f72/",
        progress=said.append,
    )
    assert said, "a minute-long listing must not run in silence"
    assert "listed 50 of 120" in said[0]
    assert "listed 120 of 120" in said[-1]


def test_scrape_forum_works_keeps_a_partial_listing_and_says_so(monkeypatch):
    """A section that fails half way through is worth keeping — but the
    caller has to be told it's half, or a partial listing is
    indistinguishable from a short section."""
    listings = {
        "72": [
            topic_row(str(100 + i), f"Story {i}", "Bardo",
                      "20260709T15:46:58")
            for i in range(120)
        ],
    }
    board = FakeBoard(BOARD_TREE, listings)
    calls = {"n": 0}
    real = board.__call__

    def flaky(method, *params):
        if method == "get_topic":
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("connection reset")
        return real(method, *params)

    monkeypatch.setattr(mp, "mobiquo_call", flaky)
    said: list[str] = []
    name, works = MousepadScraper().scrape_forum_works(
        "https://www.tapatalk.com/groups/themousepad/stories-f72/",
        progress=said.append,
    )
    assert len(works) == 50
    assert any("stopped after 50 of 120" in line for line in said)


def test_scrape_forum_works_rejects_the_board_front_page(fake_forum_board):
    """The front page names no section, so there is nothing to list.
    Callers answer it with the section list instead."""
    with pytest.raises(ValueError, match="names the board"):
        MousepadScraper().scrape_forum_works(
            "https://www.tapatalk.com/groups/themousepad/",
        )


def test_forum_url_classifies_as_a_forum_list_page():
    from ficary.url_classifier import classify

    ref = classify("https://www.tapatalk.com/groups/themousepad/stories-f72/")
    assert ref.kind == "forum"
    assert ref.extractor == "scrape_forum_works"
    assert ref.site_name == "mousepad"
    assert ref.scraper_cls is MousepadScraper


def test_sites_is_forum_url_only_fires_for_sections():
    from ficary.sites import is_forum_url

    assert is_forum_url(
        "https://www.tapatalk.com/groups/themousepad/stories-f72/"
    )
    assert not is_forum_url(
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=1"
    )
    assert not is_forum_url("https://www.fanfiction.net/s/12345")


def test_search_fan_out_still_covers_only_the_two_fiction_archives():
    """Requests and Experiences are reachable from the section picker on
    purpose, but they must stay out of the search fan-out: one is
    want-ads, the other blog-style anecdotes, and both would surface as
    stories in an unrelated keyword search."""
    assert es._MOUSEPAD_STORY_FORUMS == ("72", "97")
