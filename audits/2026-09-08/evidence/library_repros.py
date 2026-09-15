"""Isolated audit reproductions; no network or real user state."""
from pathlib import Path
import json
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from ficary.library import backup
from ficary.library.index import LibraryIndex
from ficary.library.scanner import scan
from ficary.library.abandoned import mark_abandoned_urls
from ficary.library.doctor import check_integrity, heal
from ficary.library.refresh import build_refresh_queue
from ficary.watchlist import Watch, WatchlistStore, run_once
from ficary.watchlist_poller import WatchlistPoller
from ficary import cache_doctor
from tests.library_fixtures import ficary_html


def report(name, **values):
    print(json.dumps({"case": name, **values}, sort_keys=True))


def run(base):
    # Ten distinct past stamps avoid any reliance on filesystem order.
    directory = base / "backup"
    directory.mkdir()
    index_path = directory / "index.json"
    index_path.write_text('{"current": true}')
    for i in range(10):
        (directory / f"index.backup-202601{i+1:02d}-000000-1234abcd.json").write_text(json.dumps({"generation": i}))
    oldest = backup.list_backups(index_path)[-1]
    try:
        backup.restore(oldest, index_path)
        error = "none"
    except Exception as exc:
        error = type(exc).__name__
    report("restore_oldest", error=error, selected_backup_survives=oldest.exists(), current_index=json.loads(index_path.read_text()))

    store = WatchlistStore(base / "watches-edit.json")
    watch = Watch(target="https://www.fanfiction.net/s/12345/1/", last_seen=2, channels=["email"])
    store.add(watch)
    editor = WatchlistStore(store.path)
    def edit_during_poll(url):
        editor.reload()
        edited = editor.get(watch.id)
        edited.enabled = False
        edited.label = "New label"
        editor.update(edited)
        return 2
    run_once(store, None, scraper_factory=lambda url: SimpleNamespace(get_chapter_count=edit_during_poll), notifier=lambda *a: ([], []))
    store.reload()
    after = store.get(watch.id)
    report("poll_overwrites_user_edit", enabled=after.enabled, label=after.label)

    store = WatchlistStore(base / "watches-download.json")
    watch = Watch(target="https://www.fanfiction.net/s/12345/1/", last_seen=2, auto_download=True)
    store.add(watch)
    calls = []
    def failed_download(watch, result):
        calls.append(watch.id)
        raise OSError("isolated simulated disk failure")
    kwargs = dict(scraper_factory=lambda url: SimpleNamespace(get_chapter_count=lambda u: 3), notifier=lambda *a: ([], []), downloader=failed_download, now=lambda: 1000000000)
    first = run_once(store, None, **kwargs)[0]
    second = run_once(store, None, **kwargs)[0]
    store.reload()
    report("failed_download_no_retry", download_attempts=len(calls), first_error=first.download_error, second_new_items=second.new_items, persisted_error=store.get(watch.id).last_error, last_seen=store.get(watch.id).last_seen)

    store = WatchlistStore(base / "watches-notification.json")
    watch = Watch(target="https://www.fanfiction.net/s/12345/1/", last_seen=2, channels=["email"])
    store.add(watch)
    deliveries = []
    def failed_delivery(*args):
        deliveries.append(1)
        return ([], [("email", "isolated failure")])
    kwargs = dict(scraper_factory=lambda url: SimpleNamespace(get_chapter_count=lambda u: 3), notifier=failed_delivery, now=lambda: 1000000000)
    run_once(store, None, **kwargs)
    run_once(store, None, **kwargs)
    store.reload()
    report("delivery_failure_consumed", attempts=len(deliveries), last_seen=store.get(watch.id).last_seen, last_error=store.get(watch.id).last_error, cooldown=store.get(watch.id).cooldown_until)

    prefs = SimpleNamespace(enabled=True, get=lambda key: "300", get_bool=lambda key: prefs.enabled)
    poller = WatchlistPoller(prefs)
    entered = threading.Event()
    release = threading.Event()
    poller._read_interval = lambda: 0.001
    poller._do_poll = lambda: (entered.set(), release.wait(2))
    poller.start()
    assert entered.wait(2)
    worker = poller._thread
    prefs.enabled = False
    poller.reconfigure()
    prefs.enabled = True
    poller.reconfigure()
    stop_set_after_reenable = poller._stop.is_set()
    release.set()
    worker.join(2)
    report("autopoll_off_on", pref_enabled=prefs.enabled, stop_set_after_reenable=stop_set_after_reenable, worker_alive=worker.is_alive())

    root = base / "library"
    root.mkdir()
    index_path = base / "library-index.json"
    path = ficary_html(root, chapters=2)
    with patch("ficary.library.scanner._resolve_bucket_folders", return_value=("Adult", "Original")):
        scan(root, index_path=index_path, abandoned_after_days=0)
        index = LibraryIndex.load(index_path)
        url = next(index.stories_in(root))[0]
        mark_abandoned_urls(index, [url], roots=[root])
        index.save()
        before = index.lookup_by_url(root, url).get("abandoned_at")
        scan(root, index_path=index_path, abandoned_after_days=0)
        after = LibraryIndex.load(index_path).lookup_by_url(root, url).get("abandoned_at")
        report("scan_erases_abandoned", before=bool(before), after=bool(after))

        ficary_html(root, chapters=3)
        index = LibraryIndex.load(index_path)
        before_queue, _ = build_refresh_queue(root, index_path=index_path, progress=lambda s: None)
        drift = check_integrity(root, index)
        healed = heal(root, index, drift, refresh_drift=True)
        index.save()
        after_queue, _ = build_refresh_queue(root, index_path=index_path, progress=lambda s: None)
        report("heal_stamps_stale_chapter_count", drift_entries=len(drift.drifted_entries), refreshed=healed.refreshed_drift, local_before_heal=before_queue[0]["local"], local_after_heal=after_queue[0]["local"])

    cache = base / "cache"
    cache.mkdir()
    entry = cache / "ffn_12345"
    entry.mkdir()
    (entry / "chapters.json").write_text("first generation")
    cache_report = cache_doctor.CacheReport(cache_root=cache, orphan_entries=[entry])
    with patch("time.strftime", return_value="20260908-010101"):
        first = cache_doctor.prune(cache_report)
        entry.mkdir()
        (entry / "chapters.json").write_text("second generation")
        second = cache_doctor.prune(cache_report)
    report("cache_quarantine_collision", first_pruned=first.pruned, second_pruned=second.pruned, active_exists=entry.exists(), quarantined_content=(second.quarantine_dir / "ffn_12345" / "chapters.json").read_text())


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ficary-library-audit-") as directory:
        run(Path(directory))
