"""A failed download says why, where the caller will see it.

``_download_one`` reported failures with ``print(..., file=sys.stderr)``.
The command line reads stderr; the GUI passes a ``status_callback`` and
never does, so a library update summarised a failed story as "download
failed (see log above)" with nothing above to see — and, in the frozen
desktop build with its console detached, the print itself raised.
"""

from __future__ import annotations

import argparse

import pytest

from ficary import cli
from ficary.library.template import DEFAULT_TEMPLATE

REASON = (
    "FanFiction.net answered 'Chapter not found' for "
    "https://www.fanfiction.net/s/1/66/. Still the same answer after 5 "
    "attempts over 130s; the story stays queued and the next update run "
    "retries it."
)


class _FailingScraper:
    site_name = "ffn"

    def parse_story_id(self, url):
        return 1

    def download(self, *args, **kwargs):
        raise ValueError(REASON)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        format="html",
        name=DEFAULT_TEMPLATE,
        chapters=None,
        max_retries=5,
        no_cache=True,
        delay_min=None,
        delay_max=None,
        chunk_size=None,
        use_wayback=False,
        cf_solve=False,
        refetch_all=False,
        hr_as_stars=False,
        strip_notes=False,
        send_to_kindle=None,
        clean_cache=False,
        speech_rate="0",
        attribution="builtin",
        attribution_model_size="",
    )


@pytest.fixture
def failing_scraper(monkeypatch):
    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: _FailingScraper())


def test_the_reason_reaches_a_gui_status_callback(tmp_path, failing_scraper, capsys):
    lines: list[str] = []
    ok = cli._download_one(
        "https://www.fanfiction.net/s/1/", _args(), tmp_path,
        status_callback=lines.append,
    )
    assert ok is False
    assert any(line == f"Error: {REASON}" for line in lines), lines
    # Not duplicated onto stderr when a callback took it.
    assert "Chapter not found" not in capsys.readouterr().err


def test_the_reason_still_goes_to_stderr_on_the_command_line(
    tmp_path, failing_scraper, capsys,
):
    ok = cli._download_one("https://www.fanfiction.net/s/1/", _args(), tmp_path)
    assert ok is False
    assert f"Error: {REASON}" in capsys.readouterr().err
