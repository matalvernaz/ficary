"""Per-site Cloudflare-challenge circuit breaker in the bulk update.

An interactive challenge (``cf-mitigated: challenge``) is served
per-site and per-client: once a site starts challenging, every further
probe burns its whole retry budget and fails with the same five-line
guidance paragraph. Before the breaker, a 340-story FFN group that
went shields-up mid-run meant an hour of guaranteed-futile probing and
hundreds of repeated paragraphs (probe line, Phase-3 classification,
final summary — three prints per story) through a screen reader.

These tests pin the breaker contract in ``cli._run_update_queue``:

* Trips after ``_CF_BREAKER_THRESHOLD`` *consecutive*
  ``CloudflareChallengeError`` probe failures; remaining probes for
  that site never contact it.
* A definitive upstream answer resets the streak; neutral failures
  (rate limit, timeout) neither feed nor reset it.
* Breaker-skipped entries are never stamped (``on_probe_complete``),
  so the TTL retries them on the next update.
* Queued downloads against a tripped site are suppressed with an
  aggregate line instead of grinding into the same wall.
"""

import types
from pathlib import Path

from ficary.scraper import CloudflareChallengeError, RateLimitError


class _FakeSiteClass:
    """Partition key for _run_update_queue (reads ``site_name`` off
    the class, not an instance)."""
    site_name = "fake"


def _challenge():
    return CloudflareChallengeError("https://example.com is challenged")


def _fake_scraper(results):
    """Scraper stub whose ``probe_chapter_count`` returns the next
    scripted value or raises a scripted exception, recording each call.

    Scripting exactly as many results as expected probes doubles as an
    over-probe guard: an unexpected extra call raises StopIteration,
    which the probe loop doesn't catch, failing the test loudly.
    """
    scraper = types.SimpleNamespace()
    scraper.site_name = "fake"
    scraper.concurrency = 1
    iterator = iter(results)
    calls: list[str] = []

    def probe(url):
        calls.append(url)
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    scraper.probe_chapter_count = probe
    scraper.get_chapter_count = probe
    scraper.calls = calls
    return scraper


def _queue(n, **extra):
    return [
        {
            "path": Path(f"/tmp/x{i}.epub"), "rel": f"x{i}.epub",
            "url": f"https://example.com/s/{i}", "local": 5, **extra,
        }
        for i in range(1, n + 1)
    ]


def _run(monkeypatch, fake, probe_queue, *, dry_run=True):
    from ficary import cli

    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: fake)
    monkeypatch.setattr(cli, "_detect_site", lambda url: _FakeSiteClass)

    lines: list[str] = []
    stamped: list[tuple[str, int | None]] = []

    def on_complete(url, remote_count=None):
        stamped.append((url, remote_count))

    args = types.SimpleNamespace(dry_run=dry_run, format="html")
    code = cli._run_update_queue(
        probe_queue, args, workers=1,
        skipped_count=0,
        label="test",
        progress=lines.append,
        on_probe_complete=on_complete,
    )
    return code, lines, stamped


def test_breaker_trips_after_three_consecutive_challenges(monkeypatch):
    fake = _fake_scraper([_challenge(), _challenge(), _challenge()])
    probe_queue = _queue(10)

    code, lines, stamped = _run(monkeypatch, fake, probe_queue)

    # Exactly threshold probes hit the site; the other 7 never did.
    assert len(fake.calls) == 3
    assert [bool(e.get("cf_skipped")) for e in probe_queue] == (
        [False] * 3 + [True] * 7
    )
    # Nothing answered → nothing stamped → TTL retries all next run.
    assert stamped == []
    # One trip notice, one group-skip count, and the aggregate line.
    assert sum("consecutive" in l for l in lines) == 1
    assert any("skipped 7 remaining probe(s)" in l for l in lines)
    assert any("fake: skipped 7 probe(s)" in l for l in lines)
    # The three real failures keep the run's exit code honest.
    assert code == 1


def test_definitive_answer_resets_streak(monkeypatch):
    fake = _fake_scraper([
        _challenge(), _challenge(),          # streak 2
        12,                                  # definitive → streak 0
        _challenge(), _challenge(), _challenge(),  # streak 3 → trip
    ])
    probe_queue = _queue(8)

    code, lines, stamped = _run(monkeypatch, fake, probe_queue)

    assert len(fake.calls) == 6
    assert [bool(e.get("cf_skipped")) for e in probe_queue] == (
        [False] * 6 + [True] * 2
    )
    # Only the successful probe stamped.
    assert stamped == [("https://example.com/s/3", 12)]


def test_neutral_errors_neither_feed_nor_reset_streak(monkeypatch):
    fake = _fake_scraper([
        _challenge(),            # streak 1
        RateLimitError("429"),   # neutral — streak stays 1
        _challenge(),            # streak 2
        _challenge(),            # streak 3 → trip
    ])
    probe_queue = _queue(6)

    _run(monkeypatch, fake, probe_queue)

    assert len(fake.calls) == 4
    assert [bool(e.get("cf_skipped")) for e in probe_queue] == (
        [False] * 4 + [True] * 2
    )


def test_below_threshold_probes_everything(monkeypatch):
    fake = _fake_scraper([_challenge(), _challenge(), 10, 11, 12])
    probe_queue = _queue(5)

    code, lines, _ = _run(monkeypatch, fake, probe_queue)

    assert len(fake.calls) == 5
    assert not any(e.get("cf_skipped") for e in probe_queue)
    assert not any("consecutive" in l for l in lines)


def test_tripped_site_suppresses_queued_downloads(monkeypatch):
    from ficary import cli

    # Entry 1 resumes from the index (remote pre-filled, no probe);
    # entries 2-4 challenge and trip the breaker; entry 5 is skipped.
    probe_queue = _queue(5)
    probe_queue[0]["remote"] = 20

    fake = _fake_scraper([_challenge(), _challenge(), _challenge()])

    downloads: list[str] = []
    monkeypatch.setattr(
        cli, "_download_one",
        lambda url, *a, **kw: downloads.append(url) or True,
    )

    code, lines, _ = _run(monkeypatch, fake, probe_queue, dry_run=False)

    # The resumable entry would have downloaded 15 new chapters — but
    # the site is challenging every request, so it must not be tried.
    assert downloads == []
    assert any("queued download(s)" in l for l in lines)
    # Summary carries the per-site skip count for the run's tail.
    assert any(
        "1 download(s) skipped (Cloudflare challenge)" in l for l in lines
    )
    assert code == 1
