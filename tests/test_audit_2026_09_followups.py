"""Regression tests for defects found while fixing the 2026-09-08 audit.

These are not audit findings. They are the same defect classes the audit
named, found on surfaces it did not cover.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ficary import portable, prefs as prefs_mod
from ficary.scraper import FFNScraper, _normalise_chapter_title


# ── Ordinal chapter caches on sites with no stable chapter id ──────

_BODY = '<div class="storytext" id="storytext"><p>{}</p></div>'


def _ffn_page(titles: dict, body: str) -> str:
    options = "".join(
        f'<option value="{n}">{n}. {t}</option>' for n, t in titles.items()
    )
    return (
        '<html><body><div id="profile_top">'
        '<b class="xcontrast_txt">A Story</b>'
        '<a class="xcontrast_txt" href="/u/1/Author">Author</a>'
        '<div class="xcontrast_txt">Summary here.</div>'
        '<span class="xgray xcontrast_txt">Rated: Fiction  T - English - '
        f'Chapters: {len(titles)} - Words: 100 - Published: 1/1/2020</span>'
        '</div>'
        f'<select id="chap_select">{options}</select>'
        + _BODY.format(body) +
        '</body></html>'
    )


@pytest.fixture
def ffn(tmp_path):
    scraper = FFNScraper(cache_dir=tmp_path, delay_range=(0, 0))
    scraper._delay = lambda *a, **k: None
    return scraper


def _run(scraper, titles, bodies, fetched):
    pages = {
        f"https://www.fanfiction.net/s/1/{n}": _ffn_page(titles, bodies[n])
        for n in titles
    }

    def fake_fetch(url, **kw):
        fetched.append(url)
        return pages[url]

    with patch.object(scraper, "_fetch", fake_fetch):
        return scraper.download(1)


def test_an_inserted_ffn_chapter_is_not_served_from_the_old_ordinal(ffn):
    """FFN numbers chapters by position and offers no stable id, so an
    insertion shifts every later chapter. The cached title is the only
    signal that the ordinal now means a different chapter."""
    fetched: list[str] = []
    _run(ffn, {1: "A", 2: "B"}, {1: "body A", 2: "body B"}, fetched)

    fetched.clear()
    story = _run(
        ffn, {1: "A", 2: "INSERTED", 3: "B"},
        {1: "body A", 2: "body INSERTED", 3: "body B"}, fetched,
    )
    assert [c.title for c in story.chapters] == ["A", "INSERTED", "B"]
    assert [c.html for c in story.chapters] == [
        "<p>body A</p>", "<p>body INSERTED</p>", "<p>body B</p>",
    ]


def test_an_unchanged_ffn_story_still_serves_from_cache(ffn):
    """The title guard must not cost a refetch when nothing moved."""
    fetched: list[str] = []
    titles, bodies = {1: "A", 2: "B"}, {1: "body A", 2: "body B"}
    _run(ffn, titles, bodies, fetched)

    fetched.clear()
    _run(ffn, titles, bodies, fetched)
    # Only the metadata page, which doubles as chapter 1.
    assert fetched == ["https://www.fanfiction.net/s/1/1"]


@pytest.mark.parametrize("cached,current", [
    ("Homecoming", "3. Homecoming"),
    ("  Homecoming  ", "homecoming"),
    ("The  Long   Road", "The Long Road"),
])
def test_cosmetic_title_differences_do_not_force_a_refetch(cached, current):
    assert _normalise_chapter_title(cached) == _normalise_chapter_title(current)


def test_a_genuinely_different_title_is_a_cache_miss():
    assert _normalise_chapter_title("Homecoming") != _normalise_chapter_title(
        "Departure",
    )


# ── Command-line runs must see the application's settings ──────────

@pytest.fixture
def portable_root(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(portable, "_cached_root", Path(tmp))
        yield Path(tmp)


def test_the_cli_reads_the_settings_the_app_wrote(portable_root):
    portable.settings_file().write_text(
        "library_path=/home/matt/Fanfic\n"
        "cf_solve=1\n"
        "name_template={title} - {author}\n",
        encoding="utf-8",
    )
    prefs = prefs_mod.Prefs()
    assert prefs.get("library_path") == "/home/matt/Fanfic"
    assert prefs.get_bool("cf_solve") is True
    assert prefs.get("name_template") == "{title} - {author}"
    # An unset key still falls back to the shipped default.
    assert prefs.get("html_style") == "modern"


def test_settings_written_from_the_cli_are_readable_again(portable_root):
    prefs = prefs_mod.Prefs()
    prefs.set("ao3_cookie", "session=abc; other=def")
    prefs.set_bool("fichub", False)
    assert prefs.last_save_error == ""

    body = portable.settings_file().read_text(encoding="utf-8")
    assert "fichub=0" in body, "bools must use wx's 1/0 form"

    reread = prefs_mod.Prefs()
    assert reread.get("ao3_cookie") == "session=abc; other=def"
    assert reread.get_bool("fichub") is False


def test_a_missing_settings_file_is_not_an_error(portable_root):
    prefs = prefs_mod.Prefs()
    assert prefs.get("library_path") in (None, "")
    assert prefs.get_bool("cf_solve") is False


# ── Release plumbing ───────────────────────────────────────────────

def test_cli_reports_its_version():
    """A packaged build's smoke test relies on this."""
    import subprocess
    import sys

    from ficary import __version__

    out = subprocess.run(
        [sys.executable, "-m", "ficary", "--version"],
        capture_output=True, text=True, check=True,
    )
    assert __version__ in out.stdout


def test_release_builds_gate_on_the_test_workflow():
    """A tag must not publish an artifact from an untested revision."""
    import yaml
    from pathlib import Path

    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    tests = yaml.safe_load((workflows / "tests.yml").read_text())
    # PyYAML parses a bare ``on:`` key as the boolean True.
    triggers = tests.get("on", tests.get(True))
    assert "workflow_call" in triggers, "tests.yml must be callable"

    for name in ("build-linux.yml", "build-macos.yml", "build-windows.yml"):
        data = yaml.safe_load((workflows / name).read_text())
        build = data["jobs"]["build"]
        assert build.get("needs") == "test", f"{name} does not gate on tests"
        assert data["jobs"]["test"]["uses"].endswith("tests.yml")


def test_the_macos_build_fetches_an_arm64_ffmpeg():
    """The bundle is arm64; a translated helper needs Rosetta 2."""
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github" / "workflows" / "build-macos.yml"
    ).read_text()
    downloads = [
        line for line in workflow.splitlines()
        if "https://" in line and not line.lstrip().startswith("#")
    ]
    assert not any("evermeet.cx" in line for line in downloads), (
        "Evermeet publishes Intel binaries only"
    )
    assert "ffmpeg9arm.zip" in workflow and "ffprobe9arm.zip" in workflow
    assert "lipo -archs" in workflow, "the architecture must be verified"


def test_a_synthesised_chapter_title_is_not_used_as_a_guard():
    """``"Chapter 2"`` comes from the ordinal, so it always matches the
    cached placeholder and would make the guard silently useless. The
    adapters must pass the site's own title, or nothing."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ficary"
    sources = [root / "scraper.py"] + sorted((root / "erotica").glob("*.py"))
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"expect_title=(\w+)", text):
            name = match.group(1)
            assert name != "ch_title", (
                f"{path.name} guards the chapter cache with a possibly "
                "synthesised title; pass the site's own title instead"
            )


def test_a_cancelled_library_sweep_does_not_report_completion():
    """The counts describe what was reached, not the whole library."""
    import threading
    from types import SimpleNamespace

    from ficary import cli

    lines: list[str] = []
    cancel = threading.Event()
    cancel.set()   # already cancelled: the wait loop exits on its first pass

    # The real job shape, so this exercises the production call path
    # rather than a hand-built stand-in that drifts from it.
    from ficary.jobs import DownloadJob

    args = DownloadJob(dry_run=False, skip_complete=False)
    queue = [
        {"path": f"/tmp/story-{n}.epub", "rel": f"story-{n}.epub",
         "url": f"https://www.fanfiction.net/s/{n}", "local": 1}
        for n in range(3)
    ]
    cli._run_update_queue(
        queue, args, workers=1, skipped_count=0,
        progress=lines.append, cancel_event=cancel,
    )

    joined = "\n".join(lines).lower()
    assert "cancelled" in joined
    assert "update-all complete" not in joined
    assert "not checked" in joined, "say how much of the library was skipped"


def test_a_cancelled_render_is_reported_as_cancelled_not_as_an_error():
    """Pressing Cancel is not a failure, and must not read like one."""
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from ficary import gui as gui_mod, tts

    frame = MagicMock()
    frame._render_cancel = None
    frame._render_cancels = []
    frame._render_cancel_lock = __import__("threading").Lock()
    logged: list[str] = []
    frame._log = logged.append
    frame._resolve_output_dir.return_value = "/tmp/unused"

    # The production snapshot type, so this exercises the real call
    # path instead of a stand-in that drifts from it.
    params = gui_mod._DownloadParams(
        fmt="audio", raw_output_dir="/tmp/unused",
        filename_template="{title}", hr_as_stars=False, strip_notes=False,
        llm_strip_notes=False, audio_backend="builtin", speech_rate=0,
    )

    from ficary.models import Chapter, Story

    story = Story(
        1, "A Story", "Author", "", "https://www.fanfiction.net/s/1",
        chapters=[Chapter(1, "One", "<p>text</p>")],
    )

    class _Scraper:
        """A plain double: a MagicMock answers every predicate truthily
        and the download is routed into the author/series branches."""

        site_name = "ffn"

        def download(self, *a, **k):
            return story

        @staticmethod
        def parse_story_id(url_or_id):
            return 1

        @staticmethod
        def is_author_url(url):
            return False

        @staticmethod
        def is_series_url(url):
            return False

        @staticmethod
        def is_bookmarks_url(url):
            return False

    frame._scraper_for.return_value = _Scraper()
    frame._url_opens_picker.return_value = False
    # The real export, so the translation from the audio stack's
    # cancellation to the download path's is actually exercised.
    frame._export_story = lambda *a, **k: gui_mod.MainFrame._export_story(
        frame, *a, **k,
    )

    def cancelled(*a, **k):
        raise tts.AudiobookCancelled("Audiobook render cancelled by user.")

    with patch.object(tts, "generate_audiobook", cancelled), \
         patch("ficary.gui.wx.CallAfter", side_effect=lambda cb, *a, **k: cb(*a, **k)):
        outcome = gui_mod.MainFrame._run_download(
            frame, "https://www.fanfiction.net/s/1", params=params,
        )

    assert outcome.ok is False
    assert outcome.reason == "cancelled"
    joined = "\n".join(logged)
    assert "cancelled" in joined.lower()
    assert "Error:" not in joined


def test_the_audio_stack_is_not_imported_at_application_startup():
    """It costs about a third of a second, and most launches never
    render audio."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, ficary.gui; print('ficary.tts' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"
