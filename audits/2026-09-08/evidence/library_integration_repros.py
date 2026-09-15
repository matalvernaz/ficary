"""Additional integration audit checks; all state stays in a temporary tree."""
from pathlib import Path
import json
import tempfile
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from ficary import cli, prefs, single_flight
from ficary.jobs import DownloadJob
from ficary.library.index import LibraryIndex
from ficary.library.scanner import scan
from ficary.library.doctor import check_integrity, heal
from ficary.library.template import render
from ficary.library.refresh import build_refresh_queue
from ficary.library.fulltext import FullTextIndex, populate_from_library
from ficary.library.reorganizer import apply, MoveOp
from ficary.watchlist import Watch, PollResult
from ficary.updater import FileMetadata
from tests.library_fixtures import ficary_html


def report(case, **values):
    print(json.dumps({"case": case, **values}, sort_keys=True))


def run(base):
    root = base / "library"
    root.mkdir()
    idx_path = base / "index.json"
    values = {
        prefs.KEY_LIBRARY_PATH: str(root),
        prefs.KEY_LIBRARY_ADULT_PATH: str(root / "private"),
        prefs.KEY_CF_SOLVE: "true",
        prefs.KEY_SCRIBBLEHUB_COOKIE: "synthetic-cookie",
        prefs.KEY_SUBSCRIBESTAR_COOKIE: "synthetic-cookie",
        prefs.KEY_FORMAT: "html",
    }
    fake_prefs = SimpleNamespace(get=lambda key, default="": values.get(key, default), get_bool=lambda key: values.get(key) == "true")
    captures = []
    def capture_download(url, job, output_dir, **kwargs):
        captures.append({"output_dir": str(output_dir), "autosort": bool(getattr(job, "_library_autosort", False)), "cf_solve": job.cf_solve, "scribblehub_cookie_present": bool(getattr(job, "scribblehub_cookie", None)), "subscribestar_cookie_present": bool(getattr(job, "subscribestar_cookie", None))})
        return True
    with patch("ficary.prefs.Prefs", return_value=fake_prefs), patch("ficary.library.index.default_index_path", return_value=idx_path), patch("ficary.cli._download_one", side_effect=capture_download):
        downloader = cli.make_watch_downloader(fake_prefs)
        watch = Watch(target="https://www.fanfiction.net/s/987654/1/")
        downloader(watch, PollResult(watch_id=watch.id, ok=True, new_items=[watch.target]))
        report("watch_download_ignores_output_and_auth_prefs", **captures[-1])

        # A GUI job returns None on success; emulate an already claimed
        # in-flight Future so the watch's real queue enqueue joins it.
        from ficary.sites import canonical_url
        joined = Future()
        key = canonical_url(watch.target)
        assert single_flight.claim(key, joined) is None
        prior = len(captures)
        from ficary.download_queue import DownloadQueues
        real_enqueue = DownloadQueues.enqueue
        def enqueue_and_finish(*args, **kwargs):
            future = real_enqueue(*args, **kwargs)
            assert future is joined
            joined.set_result(None)
            return future
        try:
            with patch.object(DownloadQueues, "enqueue", side_effect=enqueue_and_finish):
                downloader(watch, PollResult(watch_id=watch.id, ok=True, new_items=[watch.target]))
            error = "none"
        except Exception as exc:
            error = str(exc)
        finally:
            single_flight.release(key, joined)
        report("watch_join_gui_success_none", error=error, own_download_executed=len(captures) != prior)

    path = ficary_html(root, chapters=2)
    with patch("ficary.library.scanner._resolve_bucket_folders", return_value=("Adult", "Original")):
        scan(root, index_path=idx_path, abandoned_after_days=0)
        index = LibraryIndex.load(idx_path)
        url, entry = next(index.stories_in(root))
        entry["adult"] = True
        index.save()
        scan(root, index_path=idx_path, abandoned_after_days=0)
        entry = LibraryIndex.load(idx_path).lookup_by_url(root, url)
        report("scan_erases_manual_adult", adult_override=entry.get("adult"), adapter=entry["adapter"])

        moved = root / "renamed.html"
        path.rename(moved)
        scan(root, index_path=idx_path, abandoned_after_days=0)
        index = LibraryIndex.load(idx_path)
        entry = index.lookup_by_url(root, url)
        checked = check_integrity(root, index)
        primary_missing = len(checked.missing_files)
        duplicate_paths = list(entry.get("duplicate_relpaths", []))
        fixed = heal(root, index, checked, drop_missing=True, scan_orphans=True)
        index.save()
        report("heal_drops_live_duplicate", missing_before=primary_missing, duplicates_before=duplicate_paths, removed=fixed.removed_missing, orphan_scans=fixed.scanned_orphans, story_still_indexed=index.lookup_by_url(root, url) is not None, renamed_file_survives=moved.exists())

    concurrent_path = base / "new-index.json"
    a = LibraryIndex.load(concurrent_path)
    b = LibraryIndex.load(concurrent_path)
    a.library_state(root)["stories"]["https://www.fanfiction.net/s/1/1/"] = {"relpath": "a.html"}
    b.library_state(root)["stories"]["https://www.fanfiction.net/s/2/1/"] = {"relpath": "b.html"}
    a.save()
    b.save()
    report("first_write_conflict_missed", surviving_urls=[url for url, _ in LibraryIndex.load(concurrent_path).stories_in(root)])

    for name, content in [("invalid-utf8", b"\xff"), ("null-library", json.dumps({"version": 1, "libraries": {str(root): None}}).encode())]:
        damaged = base / f"{name}.json"
        damaged.write_bytes(content)
        try:
            LibraryIndex.load(damaged)
            error = "none"
        except Exception as exc:
            error = type(exc).__name__
        report("index_corruption_" + name, error=error)

    name = render(FileMetadata(title="界" * 100, author="A", fandoms=[], format="html"), template="{title}.{ext}")
    try:
        (root / name).touch()
        error = "none"
    except OSError as exc:
        error = f"{type(exc).__name__}: errno={exc.errno}"
    report("unicode_filename_limit", characters=len(name.name), utf8_bytes=len(name.name.encode()), error=error)

    with patch("ficary.library.scanner._resolve_bucket_folders", return_value=("Adult", "Original")):
        scan(root, index_path=idx_path, abandoned_after_days=0)
    index = LibraryIndex.load(idx_path)
    url, entry = next(index.stories_in(root))
    original = root / entry["relpath"]
    with FullTextIndex(base / "search.db") as fti:
        populate_from_library(fti, root, index_path=idx_path)
        destination = root / "organized" / original.name
        result = apply(root, [MoveOp(source=original, target=destination, source_url=url)], index_path=idx_path)
        hits = fti.search("text", root=str(root))
        report("reorganize_stale_fulltext_paths", moved=result.applied, search_hits=len(hits), hit_relpath=hits[0].relpath, hit_path_exists=(root / hits[0].relpath).exists(), current_index_relpath=LibraryIndex.load(idx_path).lookup_by_url(root, url)["relpath"])
        index = LibraryIndex.load(idx_path)
        index.remove(root, url)
        index.save()
        report("remove_leaves_fulltext_ghost", hits_after_removal=len(fti.search("text", root=str(root))))

    from ficary.scraper import FFNScraper
    from ficary.library.edits import scan_edits
    from ficary.content_hash import story_chapter_hashes
    remote_version = {"second": "old chapter two"}
    fetches = []
    scraper = FFNScraper(cache_dir=base / "edit-cache")
    metadata = {"title": "Fixture", "author": "Fixture Author", "summary": "", "num_chapters": 2, "chapter_titles": {"1": "One", "2": "Two"}, "extra": {}}
    def fetch(url):
        fetches.append(url)
        body = "chapter one" if url.endswith("/1") else remote_version["second"]
        return f'<html><div id="storytext"><p>{body}</p></div></html>'
    with patch.object(scraper, "_fetch", side_effect=fetch), patch.object(scraper, "_parse_metadata", return_value=metadata), patch.object(scraper, "_delay"):
        url = "https://www.fanfiction.net/s/987654"
        original = scraper.download(url)
        index = LibraryIndex.load(base / "edits-index.json")
        index.library_state(root)["stories"][url] = {"relpath": "fixture.html", "chapter_count": 2, "chapter_hashes": story_chapter_hashes(original)}
        remote_version["second"] = "revised chapter two"
        fetches.clear()
        cached_result = scan_edits(root, index, scraper_cache={"ffn": scraper})
        fetched_cached = list(fetches)
        scraper.use_cache = False
        fetches.clear()
        uncached_result = scan_edits(root, index, scraper_cache={"ffn": scraper})
        report("silent_edits_reuses_cache", default_unchanged=cached_result.unchanged, default_changed=len(cached_result.silent_edits), default_fetches=fetched_cached, forced_changed_chapters=uncached_result.silent_edits[0].changed_chapters, forced_fetches=fetches)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ficary-library-integration-audit-") as directory:
        run(Path(directory))
