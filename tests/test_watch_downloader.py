"""Round-10 F2: cli.make_watch_downloader routing (library vs fresh)."""
from pathlib import Path

import pytest

from ficary import cli
from ficary.watchlist import WATCH_TYPE_STORY, Watch


class _Prefs:
    def get(self, key, default=None):
        return {"format": "epub"}.get(key, default)
    def get_bool(self, key, default=False):
        return default


@pytest.fixture(autouse=True)
def _no_dep_check(monkeypatch):
    monkeypatch.setattr(cli, "check_format_deps", lambda fmt: None)


def _make_result(new_items):
    from ficary.watchlist import PollResult
    return PollResult(watch_id="x", ok=True, new_items=list(new_items))


def test_story_in_library_updates_in_place(tmp_path, monkeypatch):
    from ficary.library.index import LibraryIndex
    root = tmp_path / "lib"
    (root / "HP").mkdir(parents=True)
    (root / "HP" / "story.epub").write_text("x", encoding="utf-8")

    idx = LibraryIndex.load(tmp_path / "index.json")
    lib = idx.library_state(root.resolve())
    lib["stories"] = {
        "https://example/works/1": {"relpath": "HP/story.epub"}
    }
    idx.save()

    monkeypatch.setattr("ficary.library.index.default_index_path",
                        lambda: tmp_path / "index.json")
    monkeypatch.setattr(LibraryIndex, "library_roots",
                        lambda self: [str(root.resolve())])

    seen = {}

    def fake_download_one(url, job, output_dir, *, update_path=None,
                          on_export=None, **kw):
        seen["url"] = url
        seen["update_path"] = update_path
        seen["output_dir"] = output_dir
        seen["format"] = job.format
        if on_export:
            on_export(update_path or (output_dir / "new.epub"))
        return True

    monkeypatch.setattr(cli, "_download_one", fake_download_one)

    downloader = cli.make_watch_downloader(_Prefs())
    watch = Watch(type=WATCH_TYPE_STORY, target="https://example/works/1")
    saved = downloader(watch, _make_result(["https://example/works/1"]))

    assert seen["update_path"] == root.resolve() / "HP" / "story.epub"
    assert seen["output_dir"] == root.resolve() / "HP"
    assert seen["format"] == "epub"
    assert [str(p) for p in saved] == [str(root.resolve() / "HP" / "story.epub")]


def test_story_not_in_library_fresh_download(tmp_path, monkeypatch):
    from ficary.library.index import LibraryIndex
    idx = LibraryIndex.load(tmp_path / "index.json")
    idx.save()
    monkeypatch.setattr("ficary.library.index.default_index_path",
                        lambda: tmp_path / "index.json")

    calls = []

    def fake_download_one(url, job, output_dir, *, update_path=None,
                          on_export=None, **kw):
        calls.append((url, update_path))
        if on_export:
            on_export(output_dir / "fresh.epub")
        return True

    monkeypatch.setattr(cli, "_download_one", fake_download_one)

    downloader = cli.make_watch_downloader(_Prefs())
    watch = Watch(type=WATCH_TYPE_STORY, target="https://new/works/9")
    downloader(watch, _make_result(["https://new/works/9"]))

    assert calls == [("https://new/works/9", None)]  # fresh: no update_path


def test_author_watch_downloads_each_new_item(tmp_path, monkeypatch):
    from ficary.library.index import LibraryIndex
    idx = LibraryIndex.load(tmp_path / "index.json")
    idx.save()
    monkeypatch.setattr("ficary.library.index.default_index_path",
                        lambda: tmp_path / "index.json")

    urls = []
    monkeypatch.setattr(cli, "_download_one",
                        lambda url, job, od, **kw: urls.append(url) or True)

    downloader = cli.make_watch_downloader(_Prefs())
    watch = Watch(type="author", target="https://author/page")
    downloader(watch, _make_result(["https://a/1", "https://a/2"]))
    assert urls == ["https://a/1", "https://a/2"]
