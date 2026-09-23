"""Tests for sites.canonical_url and its effect on the library index.

Two files on disk for the same story can embed slightly different URL
forms — ``/s/N`` vs ``/s/N/1/`` on FFN, http vs https on AO3, etc. The
library index keys entries by canonical URL so those variants collapse
to a single entry, and duplicate detection fires correctly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ficary.library.index import LibraryIndex, SCHEMA_VERSION
from ficary.sites import canonical_url


# ---------------------------------------------------------------------------
# canonical_url itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # FFN: http/https, trailing slash, chapter suffix, slug suffix.
        ("https://www.fanfiction.net/s/12345", "https://www.fanfiction.net/s/12345"),
        ("http://www.fanfiction.net/s/12345", "https://www.fanfiction.net/s/12345"),
        ("https://www.fanfiction.net/s/12345/", "https://www.fanfiction.net/s/12345"),
        ("https://www.fanfiction.net/s/12345/1/", "https://www.fanfiction.net/s/12345"),
        (
            "https://www.fanfiction.net/s/12345/1/Some-Title-Slug",
            "https://www.fanfiction.net/s/12345",
        ),
        # AO3: http vs https, short-host form.
        ("http://archiveofourown.org/works/7", "https://archiveofourown.org/works/7"),
        ("https://archiveofourown.org/works/7", "https://archiveofourown.org/works/7"),
        ("https://ao3.org/works/7", "https://archiveofourown.org/works/7"),
        # Royal Road, FicWad — straightforward.
        ("https://royalroad.com/fiction/42", "https://www.royalroad.com/fiction/42"),
        ("http://ficwad.com/story/100", "https://ficwad.com/story/100"),
        # Literotica: slug-based IDs.
        (
            "https://www.literotica.com/s/my-fic-ch-02",
            "https://www.literotica.com/s/my-fic-ch-02",
        ),
        # Wattpad: /story/<id> and /<id>-<slug> both collapse to /story/<id>.
        (
            "https://www.wattpad.com/story/42",
            "https://www.wattpad.com/story/42",
        ),
        (
            "https://www.wattpad.com/42-some-title",
            "https://www.wattpad.com/story/42",
        ),
        # Webnovel: /book/<id> and /book/<slug>_<id> both collapse.
        (
            "https://www.webnovel.com/book/release-that-witch_7931338406001705",
            "https://www.webnovel.com/book/7931338406001705",
        ),
        # Scheme-optional + www-optional: a bare host pasted without a
        # scheme still canonicalises (regression guard for the loosened
        # URL detection).
        ("fanfiction.net/s/12345", "https://www.fanfiction.net/s/12345"),
        ("www.royalroad.com/fiction/42", "https://www.royalroad.com/fiction/42"),
        ("webnovel.com/book/7931338406001705", "https://www.webnovel.com/book/7931338406001705"),
    ],
)
def test_canonical_url_collapses_known_variants(raw, expected):
    assert canonical_url(raw) == expected


def test_canonical_url_returns_empty_for_empty():
    assert canonical_url("") == ""


def test_canonical_url_unknown_host_is_still_normalised():
    """Non-supported hosts still get scheme + trailing-slash normalised
    so two variants of a hand-typed URL don't masquerade as distinct."""
    result = canonical_url("HTTP://Example.COM/path/")
    assert result == "https://example.com/path"


# ---------------------------------------------------------------------------
# Duplicate detection inside LibraryIndex.record
# ---------------------------------------------------------------------------


class _FakeMeta:
    """Minimal FileMetadata stand-in for ``record`` tests."""

    def __init__(self, url):
        self.title = "T"
        self.author = "A"
        self.source_url = url
        self.fandoms = []
        self.rating = None
        self.status = None
        self.chapter_count = 1
        self.format = "html"
        self.updated = None


class _FakeCandidate:
    def __init__(self, path, url, confidence_value="high"):
        from ficary.library.candidate import Confidence

        self.path = path
        self.metadata = _FakeMeta(url)
        self.adapter_name = "ffn"
        self.confidence = Confidence.HIGH if confidence_value == "high" else Confidence.LOW
        self.is_trackable = True
        self.notes = []


def _tmp_library(tmp_path: Path, filename: str) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    sub = filename.split("/")[0] if "/" in filename else ""
    if sub:
        (lib / sub).mkdir(exist_ok=True)
    path = lib / filename
    path.write_text("<html></html>", encoding="utf-8")
    return lib


def test_same_story_different_url_shapes_is_detected_as_duplicate(tmp_path):
    """The core bug from Matt's library: ``/s/N`` and ``/s/N/1/`` are
    the same story but the pre-canonical index treated them as two
    separate entries. After canonical_url, both collapse to one and
    the second copy lands in ``duplicate_relpaths``."""
    lib = _tmp_library(tmp_path, "Harry Potter/foo.html")
    (lib / "misc").mkdir()
    (lib / "misc/foo.html").write_text("<html></html>", encoding="utf-8")

    idx = LibraryIndex(tmp_path / "idx.json", {"version": SCHEMA_VERSION, "libraries": {}})

    # First file: clean FFN URL.
    first = _FakeCandidate(
        lib / "Harry Potter/foo.html",
        "https://www.fanfiction.net/s/9215532",
    )
    is_new_1 = idx.record(lib, first)

    # Second file: same story, embedded URL has the /1/ chapter suffix.
    second = _FakeCandidate(
        lib / "misc/foo.html",
        "https://www.fanfiction.net/s/9215532/1/",
    )
    is_new_2 = idx.record(lib, second)

    assert is_new_1 is True
    assert is_new_2 is False

    stories = list(idx.stories_in(lib))
    assert len(stories) == 1
    url, entry = stories[0]
    assert url == "https://www.fanfiction.net/s/9215532"
    assert entry["relpath"] == "Harry Potter/foo.html"
    assert entry["duplicate_relpaths"] == ["misc/foo.html"]


def test_duplicate_relpaths_deduplicates_on_rescan(tmp_path):
    """Re-scanning the library shouldn't pile up the same path in
    ``duplicate_relpaths`` — we append only when it's genuinely new."""
    lib = _tmp_library(tmp_path, "A/s.html")
    (lib / "B").mkdir()
    (lib / "B/s.html").write_text("<html></html>", encoding="utf-8")

    idx = LibraryIndex(tmp_path / "idx.json", {"version": SCHEMA_VERSION, "libraries": {}})

    a = _FakeCandidate(lib / "A/s.html", "https://www.fanfiction.net/s/1")
    b = _FakeCandidate(lib / "B/s.html", "https://www.fanfiction.net/s/1/1/")

    idx.record(lib, a)
    idx.record(lib, b)
    # Scan again — duplicate_relpaths should still contain just one entry.
    idx.record(lib, a)
    idx.record(lib, b)

    [(_, entry)] = list(idx.stories_in(lib))
    assert entry["duplicate_relpaths"] == ["B/s.html"]


# ---------------------------------------------------------------------------
# Load-time migration of non-canonical keys in an existing index file
# ---------------------------------------------------------------------------


def test_load_migrates_non_canonical_keys(tmp_path):
    """An index written by 1.20.x (no canonicalisation) loads into the
    new build with keys rewritten and colliding entries merged."""
    path = tmp_path / "idx.json"
    raw = {
        "version": SCHEMA_VERSION,
        "libraries": {
            str(tmp_path / "lib"): {
                "last_scan": None,
                "stories": {
                    "https://www.fanfiction.net/s/9215532": {
                        "relpath": "Harry Potter/fic.html",
                        "title": "Proper Title",
                        "author": "Someone",
                        "chapter_count": 17,
                        "adapter": "ffn",
                        "confidence": "high",
                        "format": "html",
                    },
                    "https://www.fanfiction.net/s/9215532/1/": {
                        "relpath": "misc/fic.html",
                        "title": None,
                        "author": None,
                        "chapter_count": 0,
                        "adapter": "ffn",
                        "confidence": "high",
                        "format": "html",
                    },
                },
                "untrackable": [],
            }
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    idx = LibraryIndex.load(path)
    lib_state = idx.library_state(tmp_path / "lib")
    assert list(lib_state["stories"].keys()) == [
        "https://www.fanfiction.net/s/9215532"
    ]
    primary = lib_state["stories"]["https://www.fanfiction.net/s/9215532"]
    # The richer entry (with title/author/chapter_count) wins the primary slot.
    assert primary["relpath"] == "Harry Potter/fic.html"
    assert primary["duplicate_relpaths"] == ["misc/fic.html"]
    assert primary["chapter_count"] == 17


# ---------------------------------------------------------------------------
# Registry-wide invariant: a canonical URL must stay downloadable
# ---------------------------------------------------------------------------

# One representative story URL per host in ``sites._HOSTNAME_TO_SCRAPER``.
# These are URL *shapes*, not live stories — nothing here hits the network.
_HOST_SAMPLE_URLS: dict[str, str] = {
    "ficwad.com": "https://ficwad.com/story/100",
    "archiveofourown.org": "https://archiveofourown.org/works/7",
    "ao3.org": "https://ao3.org/works/7",
    "royalroad.com": "https://www.royalroad.com/fiction/42/slug",
    "scribblehub.com": "https://www.scribblehub.com/series/12345/some-slug/",
    "subscribestar.adult": "https://subscribestar.adult/posts/123456",
    "mediaminer.org": "https://www.mediaminer.org/fanfic/view_st.php/123456",
    "literotica.com": "https://www.literotica.com/s/my-fic-ch-02",
    "wattpad.com": "https://www.wattpad.com/story/42-some-title",
    "webnovel.com": "https://www.webnovel.com/book/7931338406001705",
    "adult-fanfiction.org": (
        "https://anime.adult-fanfiction.org/story.php?no=600091"
    ),
    "storiesonline.net": "https://storiesonline.net/s/12345",
    "nifty.org": "https://www.nifty.org/nifty/gay/college/story-name",
    "sexstories.com": "https://www.sexstories.com/story/123456/title",
    "mcstories.com": "https://mcstories.com/SomeStory/index.html",
    "lushstories.com": (
        "https://www.lushstories.com/stories/category/some-story-title"
    ),
    "fictionmania.tv": (
        "https://fictionmania.tv/stories/readhtmlstory.html?storyID=123456"
    ),
    "tgstorytime.com": "https://www.tgstorytime.com/viewstory.php?sid=1234",
    "chyoa.com": "https://chyoa.com/story/Some-Story.12345",
    "darkwanderer.net": "https://darkwanderer.net/threads/some-thread.12345/",
    "greatfeet.com": "https://www.greatfeet.com/stories/ts123.htm",
    "bdsmlibrary.com": (
        "https://www.bdsmlibrary.com/stories/story.php?storyid=1234"
    ),
    "tapatalk.com": (
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198149"
    ),
    "readonlymind.com": "https://readonlymind.com/@someone/some-story",
    "giantessworld.net": "https://giantessworld.net/viewstory.php?sid=1234",
    "chastitymansion.com": (
        "https://chastitymansion.com/forums/index.php?threads/my-story.12345/"
    ),
    "ticklingforum.com": (
        "https://www.ticklingforum.com/threads/some-thread.12345/"
    ),
}


def _hostname_registry():
    from ficary.sites import _HOSTNAME_TO_SCRAPER

    return _HOSTNAME_TO_SCRAPER


def test_every_registered_host_has_a_sample_url():
    """Keeps :data:`_HOST_SAMPLE_URLS` honest as sites are added — a new
    scraper with no sample would otherwise skip the invariant below
    silently."""
    missing = [
        host for host, _ in _hostname_registry()
        if host not in _HOST_SAMPLE_URLS
    ]
    assert not missing, (
        f"add a sample story URL for {missing} to _HOST_SAMPLE_URLS"
    )


@pytest.mark.parametrize(
    "host, cls",
    [(h, c) for h, c in _hostname_registry()],
    ids=[h for h, _ in _hostname_registry()],
)
def test_canonical_url_stays_parseable_by_its_scraper(host, cls):
    """The canonical form of a story URL must still carry its story id.

    The library index keys entries by ``canonical_url`` and the update
    probe feeds that same stored string back to ``parse_story_id``. A
    canonicalisation that drops the id therefore breaks three things at
    once: every story on the site collapses onto one index key (the
    second download is filed as a duplicate of the first), the update
    probe can never resolve the story, and the download queue's
    single-flight dedupe key stops distinguishing stories.

    Hit for real on The Mousepad, whose ids live in ``?t=<N>`` — the
    default canonicalisation drops query strings, so every topic
    canonicalised to the bare ``viewtopic.php``. AFF, Fictionmania,
    TGStorytime and Chastity Mansion each needed a hand-written rule
    for the same reason; this test is the guard that makes the next one
    fail loudly instead of silently eating a library.
    """
    sample = _HOST_SAMPLE_URLS[host]
    canonical = canonical_url(sample)
    cls.parse_story_id(canonical)


@pytest.mark.parametrize(
    "host",
    list(_HOST_SAMPLE_URLS),
    ids=list(_HOST_SAMPLE_URLS),
)
def test_canonical_url_is_idempotent(host):
    """Canonicalising an already-canonical URL must be a no-op, or the
    index key drifts depending on which form happened to be recorded."""
    once = canonical_url(_HOST_SAMPLE_URLS[host])
    assert canonical_url(once) == once


def test_distinct_mousepad_topics_get_distinct_canonical_urls():
    """Regression: two different Mousepad stories used to canonicalise
    to the identical string, so the library index held one entry for
    the whole site and filed every other story as its duplicate."""
    first = canonical_url(
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198149"
    )
    second = canonical_url(
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198606"
    )
    assert first != second


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198149",
        "http://tapatalk.com/groups/themousepad/viewtopic.php?t=198149",
        # phpBB puts the forum id in front of the topic id on links
        # followed out of a forum listing.
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?f=72&t=198149",
        # SEO permalink shape Tapatalk renders for every topic, with and
        # without the paging offset.
        (
            "https://www.tapatalk.com/groups/themousepad/"
            "the-keyholders-college-days-t198149.html"
        ),
        (
            "https://www.tapatalk.com/groups/themousepad/"
            "the-keyholders-college-days-t198149-s20.html"
        ),
    ],
)
def test_mousepad_url_variants_collapse_to_one_key(raw):
    """Every shape a Mousepad topic link arrives in — pasted from the
    address bar, followed out of a forum listing, or copied as an SEO
    permalink — must collapse to the same index key, or one story lands
    in the library twice."""
    assert canonical_url(raw) == (
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=198149"
    )


def test_idless_forum_entry_is_dropped_on_load(tmp_path: Path, caplog):
    """An index written before the Tapatalk rule existed holds one
    id-less ``viewtopic.php`` entry standing in for the whole board, with
    every other Mousepad story demoted into its ``duplicate_relpaths``.

    The key carries no topic id, so it can't be repaired in place — load
    drops it (loudly, naming the files) and the next library scan
    rebuilds one entry per story from the URL inside each file.
    """
    path = tmp_path / "library-index.json"
    lib_root = tmp_path / "lib"
    raw = {
        "version": SCHEMA_VERSION,
        "libraries": {
            str(lib_root): {
                "last_scan": "2026-09-01T00:00:00Z",
                "stories": {
                    "https://tapatalk.com/groups/themousepad/viewtopic.php": {
                        "relpath": "Adult/perma-single.epub",
                        "duplicate_relpaths": ["Adult/college-days.epub"],
                        "title": "Perma Single",
                        "author": "txkenpo1",
                        "chapter_count": 1,
                        "adapter": "mousepad",
                        "confidence": "high",
                        "format": "epub",
                    },
                    # A healthy entry on the same board must survive.
                    "https://www.tapatalk.com/groups/themousepad/"
                    "viewtopic.php?t=57803": {
                        "relpath": "Adult/evil-employment.epub",
                        "title": "The Evil Employment Epic",
                        "author": "darrio",
                        "chapter_count": 77,
                        "adapter": "mousepad",
                        "confidence": "high",
                        "format": "epub",
                    },
                },
                "untrackable": [],
            }
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    with caplog.at_level("WARNING"):
        idx = LibraryIndex.load(path)

    stories = idx.library_state(lib_root)["stories"]
    assert list(stories) == [
        "https://www.tapatalk.com/groups/themousepad/viewtopic.php?t=57803"
    ]
    # The drop must be audible: both orphaned filenames are named so the
    # user can see what needs re-scanning rather than noticing two
    # stories missing from the library list.
    assert "perma-single.epub" in caplog.text
    assert "college-days.epub" in caplog.text
    assert "Re-scan" in caplog.text
