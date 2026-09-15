"""Regression tests for the 2026-09-08 audit's library and watch findings.

Each test names the finding it pins and asserts the corrected
behaviour, not the reproduction the audit captured.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ficary import cache_doctor
from ficary.library import backup as B
from ficary.library.index import IndexConflictError, LibraryIndex
from ficary.library.template import _final_segment
from ficary.models import Chapter
from ficary.watchlist import (
    WATCH_TYPE_STORY,
    Notification,
    PollResult,
    Watch,
    WatchlistStore,
    run_once,
)


# ── L01: restoring a backup must not evict it ──────────────────────

def test_restoring_the_oldest_backup_keeps_it(tmp_path):
    idx = tmp_path / "index.json"
    for i in range(11):
        idx.write_text(json.dumps({"generation": i}))
        B.backup(idx)
        time.sleep(0.01)
    pool = B.list_backups(idx)
    assert len(pool) == B._MAX_BACKUPS
    oldest = pool[-1]
    idx.write_text(json.dumps({"generation": "current"}))

    safety = B.restore(oldest, idx)

    assert oldest.exists(), "the chosen recovery point must survive the restore"
    assert safety is not None and safety.exists()
    assert json.loads(idx.read_text())["generation"] == 1


def test_backups_taken_in_one_second_stay_in_creation_order(tmp_path, monkeypatch):
    """The embedded timestamp is second-resolution, so six backups
    written in a tight loop all carry the same stamp. Ordering then fell
    through to directory order, and on a hashed-directory filesystem
    that is the random uuid salt — so "newest first" was a lie and
    ``_prune`` could drop a newer backup while keeping an older one.

    Small directories on this developer's filesystem happen to enumerate
    in creation order, which is why only CI caught it; the adverse order
    is forced here so the test does not depend on the host.
    """
    idx = tmp_path / "index.json"
    for i in range(6):
        idx.write_text(json.dumps({"generation": i}))
        B.backup(idx)

    real_iterdir = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda self: iter(list(real_iterdir(self))[::-1])
    )

    generations = [
        json.loads(entry.read_text())["generation"]
        for entry in B.list_backups(idx)
    ]
    assert generations == [5, 4, 3, 2, 1, 0]


# ── L06: rescans must not undo manual classifications ──────────────

def test_record_preserves_manual_adult_and_abandoned_marks(tmp_path):
    from ficary.library.candidate import Confidence, StoryCandidate
    from ficary.updater import FileMetadata

    root = tmp_path / "lib"
    root.mkdir()
    story = root / "A Story.html"
    story.write_text("<html></html>")
    index = LibraryIndex(tmp_path / "index.json", {"version": 1, "libraries": {}})
    md = FileMetadata(source_url="https://www.fanfiction.net/s/1", title="A Story")
    candidate = StoryCandidate(
        path=story, metadata=md, confidence=Confidence.HIGH, adapter_name="ffn",
    )
    index.record(root, candidate)
    entry = index.lookup_by_url(root, md.source_url)
    entry["abandoned_at"] = "2026-01-01T00:00:00+00:00"
    entry["adult"] = True

    index.record(root, candidate)

    entry = index.lookup_by_url(root, md.source_url)
    assert entry["abandoned_at"] == "2026-01-01T00:00:00+00:00"
    assert entry["adult"] is True


# ── L08: quarantine failure must not delete the source ─────────────

def test_quarantine_failure_leaves_the_cache_in_place(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    entry = cache / "ffn_1"
    entry.mkdir()
    (entry / "chapters.json").write_text("payload")
    report = cache_doctor.CacheReport(cache_root=cache, orphan_entries=[entry])

    with patch("os.replace", side_effect=OSError("locked")):
        result = cache_doctor.prune(report)

    assert entry.is_dir(), "a failed quarantine must never delete the source"
    assert (entry / "chapters.json").read_text() == "payload"
    assert result.pruned == 0
    assert result.failed == [entry]
    assert "in place" in result.summary()


def test_two_prunes_in_the_same_second_use_different_batches(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()

    def prune_one():
        entry = cache / "ffn_1"
        entry.mkdir()
        (entry / "chapters.json").write_text("generation")
        return cache_doctor.prune(
            cache_doctor.CacheReport(cache_root=cache, orphan_entries=[entry])
        )

    first = prune_one()
    second = prune_one()
    assert first.quarantine_dir != second.quarantine_dir
    assert first.quarantine_dir.exists() and second.quarantine_dir.exists()


# ── L11 / L12: index persistence at the storage boundary ───────────

def test_two_first_time_writers_cannot_overwrite_each_other(tmp_path):
    idx_path = tmp_path / "index.json"
    root = tmp_path / "lib"
    root.mkdir()
    a = LibraryIndex.load(idx_path)
    b = LibraryIndex.load(idx_path)
    a.library_state(root)["stories"]["https://x/1"] = {"relpath": "A.html"}
    a.save()
    b.library_state(root)["stories"]["https://x/2"] = {"relpath": "B.html"}

    with pytest.raises(IndexConflictError):
        b.save()

    stored = LibraryIndex.load(idx_path)
    assert stored.lookup_by_url(root, "https://x/1") is not None


def test_load_never_raises_on_malformed_storage(tmp_path):
    bad_bytes = tmp_path / "bad.json"
    bad_bytes.write_bytes(b"\xff\xfe\x00")
    assert LibraryIndex.load(bad_bytes).save_blocker is not None

    from ficary.library.index import SCHEMA_VERSION

    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps({"version": SCHEMA_VERSION, "libraries": {"/x": None}})
    )
    inst = LibraryIndex.load(nested)   # must not raise AttributeError
    assert list(inst.library_roots()) == []


# ── L13: a surviving copy keeps the story indexed ──────────────────

def test_heal_promotes_a_surviving_duplicate_instead_of_dropping(tmp_path):
    from ficary.library.doctor import check_integrity, heal

    root = tmp_path / "lib"
    root.mkdir()
    (root / "renamed.html").write_text("<html></html>")
    index = LibraryIndex(tmp_path / "index.json", {"version": 1, "libraries": {}})
    index.library_state(root)["stories"]["https://x/1"] = {
        "relpath": "gone.html",
        "duplicate_relpaths": ["renamed.html"],
    }

    report = check_integrity(root, index)
    result = heal(root, index, report, drop_missing=True)

    entry = index.lookup_by_url(root, "https://x/1")
    assert entry is not None, "a story with a live copy must stay indexed"
    assert entry["relpath"] == "renamed.html"
    assert result.removed_missing == 0
    assert result.promoted_duplicates == 1


# ── L14: filename limits are byte limits ───────────────────────────

def test_long_unicode_segment_fits_the_filesystem(tmp_path):
    name = _final_segment("章" * 100 + ".html")
    assert len(name.encode("utf-8")) <= 200
    assert name.endswith(".html")
    (tmp_path / name).write_text("x")   # must not raise ENAMETOOLONG


def test_ascii_names_are_unchanged():
    assert _final_segment("Ordinary Title.epub") == "Ordinary Title.epub"


# ── L15: search follows the current files ──────────────────────────

def test_search_reconciles_moved_and_removed_stories(tmp_path):
    from ficary.library.fulltext import FullTextIndex

    root = tmp_path / "lib"
    root.mkdir()
    idx_path = tmp_path / "index.json"
    url = "https://www.fanfiction.net/s/1"
    index = LibraryIndex.load(idx_path)
    index.library_state(root)["stories"][url] = {
        "relpath": "new/place.html", "title": "T",
    }
    index.save()

    with FullTextIndex(tmp_path / "search.sqlite3") as fti:
        fti.index_story(
            root=str(root), url=url, relpath="old/place.html",
            title="T", author="A",
            chapters=[Chapter(number=1, title="One",
                              html="<p>a dragon over the keep</p>")],
        )
        assert fti.search("dragon")[0].relpath == "old/place.html"
        fixed = fti.search("dragon", reconcile=LibraryIndex.load(idx_path))
        assert fixed[0].relpath == "new/place.html"

        assert fti.update_relpath(str(root), url, "new/place.html") == 1
        assert fti.search("dragon")[0].relpath == "new/place.html"

    index = LibraryIndex.load(idx_path)
    index.remove(root, url)
    index.save()
    with FullTextIndex(tmp_path / "search.sqlite3") as fti:
        assert fti.search(
            "dragon", reconcile=LibraryIndex.load(idx_path)
        ) == []


# ── L16: edit scans must not read from the chapter cache ───────────

def test_edit_scan_disables_chapter_caching(tmp_path):
    from ficary.library.edits import _scraper_for_url

    cache: dict = {}
    scraper = _scraper_for_url("https://www.fanfiction.net/s/1", cache)
    assert scraper.use_cache is False

    scraper.use_cache = True     # a caller handing in a warm instance
    again = _scraper_for_url("https://www.fanfiction.net/s/1", cache)
    assert again is scraper
    assert again.use_cache is False


# ── L02 / L03 / L04: poll state versus user state ──────────────────

def _reloaded(path) -> WatchlistStore:
    store = WatchlistStore(path)
    store.reload()
    return store


def _story_watch(tmp_path, **kwargs):
    store = WatchlistStore(tmp_path / "watches.json")
    watch = Watch(
        type=WATCH_TYPE_STORY, site="ffn",
        target="https://www.fanfiction.net/s/1",
        channels=["email"], last_seen=2, **kwargs,
    )
    store.add(watch)
    return store, watch


def _scraper_factory(count):
    class _Scraper:
        def get_chapter_count(self, url):
            return count

    return lambda url: _Scraper()


def test_poll_does_not_undo_a_user_edit_made_during_the_poll(tmp_path):
    store, watch = _story_watch(tmp_path, label="", enabled=True)

    def edit_midway(url):
        # Stand in for the GUI saving an edit while the poll runs.
        other = _reloaded(store.path)
        row = other.get(watch.id)
        row.enabled = False
        row.label = "New label"
        other.update(row)

        class _Scraper:
            def get_chapter_count(self, u):
                return 3

        return _Scraper()

    run_once(store, None, scraper_factory=edit_midway, notifier=lambda *a: ([], []))

    saved = _reloaded(store.path).get(watch.id)
    assert saved.enabled is False
    assert saved.label == "New label"


def test_a_failed_auto_download_is_retried_on_the_next_poll(tmp_path):
    store, watch = _story_watch(tmp_path, auto_download=True)
    attempts = []

    def downloader(w, result):
        attempts.append(list(result.new_items))
        raise OSError("simulated disk failure")

    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []), downloader=downloader)
    saved = _reloaded(store.path).get(watch.id)
    assert saved.pending_downloads == [watch.target]
    assert "auto-download failed" in saved.last_error

    # The second poll sees nothing new, and must retry anyway.
    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []), downloader=downloader)
    assert len(attempts) == 2
    assert _reloaded(store.path).get(watch.id).pending_downloads == [
        watch.target
    ]


def test_a_successful_retry_clears_the_pending_download(tmp_path):
    store, watch = _story_watch(tmp_path, auto_download=True)
    calls = {"n": 0}

    def downloader(w, result):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk failure")
        return [Path("/tmp/story.epub")]

    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []), downloader=downloader)
    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []), downloader=downloader)

    saved = _reloaded(store.path).get(watch.id)
    assert saved.pending_downloads == []
    assert calls["n"] == 2


def test_a_failed_notification_is_not_consumed(tmp_path):
    store, watch = _story_watch(tmp_path)
    sent = []

    def failing_notifier(channels, notification, prefs=None):
        sent.append(notification.title)
        return ([], [("email", "relay refused")])

    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=failing_notifier)

    saved = _reloaded(store.path).get(watch.id)
    assert saved.cooldown_until == "", "a failed send must not start a cooldown"
    assert "delivery failed" in saved.last_error
    assert saved.pending_notification

    # A later poll with nothing new still retries the held alert.
    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=failing_notifier)
    assert len(sent) == 2


def test_a_delivered_notification_clears_the_pending_alert(tmp_path):
    store, watch = _story_watch(tmp_path)

    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []))

    saved = _reloaded(store.path).get(watch.id)
    assert saved.pending_notification == {}
    assert saved.cooldown_until != ""
    assert saved.last_error == ""


# ── L05: autopoll off then on during a poll keeps polling ──────────

def test_reenabling_autopoll_cancels_an_unobserved_stop():
    from ficary import prefs as _p
    from ficary.watchlist_poller import WatchlistPoller

    class _Prefs:
        def __init__(self):
            self._d = {_p.KEY_WATCH_AUTOPOLL: True}

        def get_bool(self, key):
            return bool(self._d.get(key))

        def get(self, key, default=None):
            return self._d.get(key, default)

        def set_bool(self, key, value):
            self._d[key] = value

    poller = WatchlistPoller(_Prefs())

    class _Alive:
        def is_alive(self):
            return True

    poller._thread = _Alive()
    poller.stop()                    # worker hasn't observed it yet
    assert poller._stop.is_set()
    poller.reconfigure()             # user switched autopoll back on
    assert not poller._stop.is_set()


# ── L10: a joined download reports its own result ──────────────────

def test_outcome_of_treats_a_silent_join_as_success():
    from ficary.download_queue import DownloadOutcome, outcome_of

    assert outcome_of(None).ok is True
    assert outcome_of(False).ok is False
    assert outcome_of(DownloadOutcome(ok=True, saved_paths=["/x"])).saved_paths == [
        "/x"
    ]


# ── Follow-up: pending work belongs only to downloading watches ────

def test_a_notify_only_watch_never_accumulates_pending_downloads(tmp_path):
    store, watch = _story_watch(tmp_path, auto_download=False)

    run_once(store, None, scraper_factory=_scraper_factory(3),
             notifier=lambda *a: (["email"], []))

    saved = _reloaded(store.path).get(watch.id)
    assert saved.pending_downloads == []


def test_a_pause_saved_during_a_poll_stops_delivery(tmp_path):
    store, watch = _story_watch(tmp_path)
    sent = []

    def pause_midway(url):
        other = _reloaded(store.path)
        row = other.get(watch.id)
        row.enabled = False
        other.update(row)

        class _Scraper:
            def get_chapter_count(self, u):
                return 3

        return _Scraper()

    run_once(store, None, scraper_factory=pause_midway,
             notifier=lambda *a: sent.append(a) or (["email"], []))

    assert sent == [], "a watch paused during the poll must not notify"
    saved = _reloaded(store.path).get(watch.id)
    assert saved.enabled is False
    assert saved.last_seen == 3, "the observation itself is still recorded"
