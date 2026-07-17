"""Tests for the Playwright-backed Cloudflare challenge fallback.

The live solve path is mocked — unit tests can't launch a real
browser and wouldn't reliably reach a live Cloudflare challenge
anyway. The tests exercise the caching layer, the session-injection
helper, and the scraper's decision logic (when to invoke the solver,
when to skip it).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ficary import cf_solve


# ── Cookie cache round-trip ──────────────────────────────────────


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the cf-cookie cache into a per-test tmp dir so the
    tests don't scribble on the real ``~/.cache/ficary/cf-cookies``."""
    monkeypatch.setattr(cf_solve, "_cookie_cache_dir", lambda: tmp_path)
    return tmp_path


def _sample_result(fetched_at: float | None = None) -> cf_solve.SolveResult:
    return cf_solve.SolveResult(
        cookies=[
            {
                "name": "cf_clearance",
                "value": "abc123",
                "domain": ".fanfiction.net",
                "path": "/",
                "secure": True,
            },
        ],
        user_agent="Mozilla/5.0 (Test)",
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def test_persist_then_load_roundtrips(cache_dir):
    result = _sample_result()
    cf_solve.persist("fanfiction.net", result)
    loaded = cf_solve.load_cached("fanfiction.net")
    assert loaded is not None
    assert loaded.cookies == result.cookies
    assert loaded.user_agent == result.user_agent


def test_load_returns_none_when_missing(cache_dir):
    assert cf_solve.load_cached("never-solved.example") is None


def test_load_respects_ttl(cache_dir):
    # Persist a solve whose timestamp is well outside the TTL window.
    stale = _sample_result(fetched_at=time.time() - cf_solve.COOKIE_CACHE_TTL_S - 1)
    cf_solve.persist("stale.example", stale)
    assert cf_solve.load_cached("stale.example") is None


def test_load_honours_injected_now_for_deterministic_ttl_check(cache_dir):
    """The TTL check takes an ``now`` override so tests don't need to
    sleep. Regression guard: earlier draft used time.time() inline and
    drifted under test parallelism."""
    # Persist a "fresh" entry by modern clock, but the now= override
    # advances past the TTL so the check treats it as stale.
    cf_solve.persist("futuristic.example", _sample_result())
    future = time.time() + cf_solve.COOKIE_CACHE_TTL_S + 1
    assert cf_solve.load_cached("futuristic.example", now=future) is None


def test_load_rejects_non_numeric_fetched_at(cache_dir, monkeypatch):
    """A corrupted cache file with a string ``fetched_at`` must be
    rejected, not coerced — float("garbage") would raise from inside
    ``load_cached`` and mask the real "no usable cache" answer.
    """
    import json
    path = cf_solve._host_cache_path("corrupt.example")
    path.write_text(
        json.dumps({
            "cookies": [{"name": "x", "value": "y", "domain": ".e", "path": "/"}],
            "user_agent": "UA",
            "fetched_at": "not-a-number",
        }),
        encoding="utf-8",
    )
    assert cf_solve.load_cached("corrupt.example") is None


def test_load_rejects_future_fetched_at(cache_dir):
    """A future timestamp (clock skew or hand-edited cache) would
    otherwise pin the entry as ever-fresh and we'd loop forever on a
    cookie the site has already invalidated."""
    far_future = time.time() + 10 * cf_solve.COOKIE_CACHE_TTL_S
    cf_solve.persist("future.example", _sample_result(fetched_at=far_future))
    assert cf_solve.load_cached("future.example") is None


def test_persist_sanitises_hostname(cache_dir):
    """Filesystem path must never escape the cache dir even if the
    caller feeds a path-like host. The host sanitiser replaces
    problem characters before the filename is formed."""
    cf_solve.persist("../evil", _sample_result())
    # Files land in the cache dir, not an ancestor — there must be no
    # file whose path resolves outside ``cache_dir``.
    for child in cache_dir.iterdir():
        assert cache_dir in child.resolve().parents or child.parent == cache_dir


def test_distinct_hosts_get_distinct_cache_files(cache_dir):
    """Two distinct hosts whose old-scheme sanitisation collapsed to
    the same string must now land in different cache files. Without a
    hash suffix, ``café.example`` and ``cafe2.example`` could both
    sanitise to ``cafe_.example``, cross-feeding Cloudflare cookies
    between unrelated hosts."""
    cf_solve.persist("café.example", _sample_result(fetched_at=time.time()))
    cf_solve.persist("cafe2.example", _sample_result(fetched_at=time.time()))
    json_files = sorted(p.name for p in cache_dir.iterdir() if p.suffix == ".json")
    assert len(json_files) == 2, f"expected two distinct cache files, got {json_files}"


def test_cache_filename_avoids_windows_reserved_names(cache_dir):
    """Hostnames like ``con``, ``nul``, ``aux``, ``prn``, ``com1`` are
    Windows reserved device names — ``con.json`` either hangs or
    fails on Windows. The cache scheme prefixes ``host-`` so the
    filename can never equal a reserved name."""
    for host in ("con", "nul", "aux", "prn", "com1", "lpt1"):
        path = cf_solve._host_cache_path(host)
        stem = path.stem
        assert stem.startswith("host-"), f"{host!r} → {stem!r}"
        # Sanity: the bare reserved name doesn't appear as a path
        # component on its own.
        for part in path.parts:
            assert part.lower() != f"{host}.json"


def test_load_rejects_nan_fetched_at(cache_dir):
    """``json.loads`` happily parses ``NaN``. ``NaN <= 0`` is False
    and ``NaN > current`` is False, so a corrupted timestamp would
    otherwise pin the cache as permanently fresh and pull a
    long-revoked cookie on every fetch. The load path must reject
    non-finite timestamps before they reach the comparison."""
    import json as _json
    path = cf_solve._host_cache_path("nan.example")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _json.dumps({
        "cookies": [{"name": "x", "value": "y", "domain": ".example"}],
        "user_agent": "UA",
        "fetched_at": 1.0,
    })
    path.write_text(raw.replace("1.0", "NaN"), encoding="utf-8")
    assert cf_solve.load_cached("nan.example") is None


def test_load_rejects_infinite_fetched_at(cache_dir):
    """Same hazard as NaN but with positive infinity — ``inf > current``
    is True so the future-timestamp guard catches this one already,
    but ``-inf <= 0`` is True so the negative-timestamp guard catches
    that one. The new explicit non-finite check makes the rejection
    independent of the ordering of the existing guards."""
    import json as _json
    path = cf_solve._host_cache_path("inf.example")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({
            "cookies": [{"name": "x", "value": "y"}],
            "user_agent": "UA",
            "fetched_at": 1.0,
        }).replace("1.0", "Infinity"),
        encoding="utf-8",
    )
    assert cf_solve.load_cached("inf.example") is None


def test_load_rejects_non_mapping_cookie_entries(cache_dir):
    """A hand-edited cache where ``cookies`` contains a string or
    a nested list would previously crash ``dict(c)`` with a
    ``ValueError``. Filter to real mappings instead so the load
    path stays a clean None on malformed data."""
    import json as _json
    path = cf_solve._host_cache_path("mixed.example")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({
            "cookies": [
                "not a dict",
                {"name": "ok", "value": "v", "domain": ".example"},
                ["also", "not", "a", "dict"],
            ],
            "user_agent": "UA",
            "fetched_at": time.time(),
        }),
        encoding="utf-8",
    )
    result = cf_solve.load_cached("mixed.example")
    assert result is not None
    assert [c["name"] for c in result.cookies] == ["ok"]


class _RealSigSession:
    """Session whose ``cookies.set`` mirrors curl_cffi 0.15's ACTUAL
    signature — (name, value, domain, path, secure), no ``expires``. The
    old tests used MagicMock, which swallowed an ``expires`` kwarg and so
    hid the TypeError the real jar raises — that's how the bug (every
    persistent cookie dropped as "rejected by jar") shipped."""

    def __init__(self):
        self.headers = {}
        self.set_calls = []
        self.cookies = self

    def set(self, name, value, domain="", path="/", secure=False):
        self.set_calls.append(dict(
            name=name, value=value, domain=domain, path=path, secure=secure,
        ))


def test_inject_applies_clearance_cookie_that_carries_expiry():
    """A solved cf_clearance carries an ``expires`` timestamp, but
    curl_cffi's Cookies.set() has no ``expires`` param — inject must not
    pass it, or set() raises and the cleared challenge never reaches the
    session (the shipped 403-forever bug)."""
    sess = _RealSigSession()
    result = cf_solve.SolveResult(
        cookies=[{
            "name": "cf_clearance", "value": "tok", "domain": ".example",
            "path": "/", "secure": True, "expires": time.time() + 3600,
        }],
        user_agent="UA",
        fetched_at=time.time(),
    )
    cf_solve.inject_into_session(sess, result)  # must not raise
    assert len(sess.set_calls) == 1
    call = sess.set_calls[0]
    assert call["name"] == "cf_clearance"
    assert call["value"] == "tok"
    assert call["domain"] == ".example"
    assert call["secure"] is True
    assert sess.headers["User-Agent"] == "UA"


def test_inject_into_real_curl_cffi_session_lands_in_jar():
    """End-to-end against the actual curl_cffi jar — the check the
    MagicMock-based test skipped: a cf_clearance with an expiry must
    actually be present in the session's cookie jar after injection."""
    from curl_cffi import requests as _cr
    sess = _cr.Session(impersonate="chrome")
    result = cf_solve.SolveResult(
        cookies=[{
            "name": "cf_clearance", "value": "tok",
            "domain": ".archiveofourown.org",
            "path": "/", "secure": True, "expires": time.time() + 3600,
        }],
        user_agent="UA/1",
        fetched_at=time.time(),
    )
    cf_solve.inject_into_session(sess, result)
    jar_names = {c.name for c in sess.cookies.jar}
    assert "cf_clearance" in jar_names
    assert sess.headers["User-Agent"] == "UA/1"


# ── solve() ─────────────────────────────────────────────────────


def test_solve_invokes_launcher_and_returns_result():
    def fake_launcher(url, timeout_s):
        return [
            {"name": "cf_clearance", "value": "x", "domain": ".example",
             "path": "/", "secure": True},
        ], "Mozilla/5.0 UA"
    result = cf_solve.solve("https://example.com/", launcher=fake_launcher)
    assert result.user_agent == "Mozilla/5.0 UA"
    assert result.cookies[0]["name"] == "cf_clearance"


def test_solve_raises_unavailable_on_import_error():
    def fake_launcher(url, timeout_s):
        raise ImportError("playwright not installed")
    with pytest.raises(cf_solve.SolverUnavailable):
        cf_solve.solve("https://example.com/", launcher=fake_launcher)


def test_solve_raises_on_empty_cookies():
    def fake_launcher(url, timeout_s):
        return [], "UA"
    with pytest.raises(RuntimeError):
        cf_solve.solve("https://example.com/", launcher=fake_launcher)


# ── inject_into_session ─────────────────────────────────────────


def test_inject_into_session_sets_cookies_and_ua():
    sess = MagicMock()
    sess.headers = {}
    result = _sample_result()
    cf_solve.inject_into_session(sess, result)
    sess.cookies.set.assert_called()
    assert sess.headers["User-Agent"] == result.user_agent


def test_inject_skips_malformed_cookies():
    """A cookie record missing a name/value shouldn't crash the jar
    set; we just log and move on. Regression guard for the case where
    Playwright returns a stub entry for a partially-blocked response."""
    sess = MagicMock()
    sess.headers = {}
    result = cf_solve.SolveResult(
        cookies=[
            {"name": "", "value": "x", "domain": ".example", "path": "/"},
            {"name": "cf", "value": None, "domain": ".example", "path": "/"},
            {"name": "ok", "value": "v", "domain": ".example", "path": "/"},
        ],
        user_agent="UA",
        fetched_at=time.time(),
    )
    cf_solve.inject_into_session(sess, result)
    # Only the "ok" cookie should have been forwarded to the jar.
    calls = sess.cookies.set.call_args_list
    names = [c.kwargs.get("name") for c in calls]
    assert names == ["ok"]


# ── Scraper integration ────────────────────────────────────────


class _ProbeScraper:
    """Subclass hook — pytest's ``import_module`` on the real scraper
    triggers the curl_cffi session setup even when the test never
    fetches. The scraper has to be fully constructed to exercise the
    cf-solve plumbing, so we instantiate the real one."""


@pytest.fixture
def scraper(cache_dir, monkeypatch):
    # cache_dir monkeypatches _cookie_cache_dir — nothing more to do.
    from ficary.scraper import BaseScraper

    class _Scr(BaseScraper):
        site_name = "probe"

    return _Scr(cf_solve=True, max_retries=2)


def test_host_extraction_normalises_www(scraper):
    assert scraper._host_for_url("https://www.fanfiction.net/s/1") == "fanfiction.net"
    assert scraper._host_for_url("https://example.org/") == "example.org"


def test_maybe_seed_cf_cookies_applies_cache(cache_dir, scraper, monkeypatch):
    """A prior persisted solve is applied before Playwright is even
    considered — this is the free path that makes the feature fast
    on repeat runs."""
    cf_solve.persist("fanfiction.net", _sample_result())
    injected = []
    monkeypatch.setattr(
        cf_solve,
        "inject_into_session",
        lambda sess, result: injected.append(result),
    )
    sess = MagicMock()
    sess.headers = {}
    applied = scraper._maybe_seed_cf_cookies(sess, "https://www.fanfiction.net/s/1")
    assert applied is True
    assert len(injected) == 1


def test_maybe_seed_skips_when_solver_disabled(cache_dir, monkeypatch):
    from ficary.scraper import BaseScraper

    class _Scr(BaseScraper):
        site_name = "probe"

    scr = _Scr(cf_solve=False)
    cf_solve.persist("fanfiction.net", _sample_result())
    sess = MagicMock()
    sess.headers = {}
    applied = scr._maybe_seed_cf_cookies(sess, "https://www.fanfiction.net/s/1")
    assert applied is False


def test_invoke_cf_solver_success_injects_and_persists(
    cache_dir, scraper, monkeypatch,
):
    def fake_solve(url, **_):
        return _sample_result()
    monkeypatch.setattr(cf_solve, "solve", fake_solve)

    injected = []
    monkeypatch.setattr(
        cf_solve,
        "inject_into_session",
        lambda sess, result: injected.append(result),
    )
    persisted = []
    monkeypatch.setattr(
        cf_solve,
        "persist",
        lambda host, result: persisted.append((host, result)),
    )

    sess = MagicMock()
    sess.headers = {}
    ok = scraper._invoke_cf_solver(sess, "https://www.fanfiction.net/s/1")
    assert ok is True
    assert len(injected) == 1
    assert persisted[0][0] == "fanfiction.net"


def test_invoke_cf_solver_deduplicates_per_host(
    cache_dir, scraper, monkeypatch,
):
    """Two concurrent 403s on the same host must invoke Playwright
    once, not twice. The in-process state map is the throttle."""
    call_count = {"n": 0}

    def fake_solve(url, **_):
        call_count["n"] += 1
        return _sample_result()

    monkeypatch.setattr(cf_solve, "solve", fake_solve)
    monkeypatch.setattr(cf_solve, "inject_into_session", lambda s, r: None)
    monkeypatch.setattr(cf_solve, "persist", lambda h, r: None)

    sess = MagicMock(); sess.headers = {}
    scraper._invoke_cf_solver(sess, "https://www.fanfiction.net/s/1")
    scraper._invoke_cf_solver(sess, "https://www.fanfiction.net/s/2")
    assert call_count["n"] == 1


def test_invoke_cf_solver_returns_false_when_disabled(cache_dir, monkeypatch):
    from ficary.scraper import BaseScraper

    class _Scr(BaseScraper):
        site_name = "probe"

    scr = _Scr(cf_solve=False)
    called = {"n": 0}

    def fake_solve(url, **_):
        called["n"] += 1
        return _sample_result()

    monkeypatch.setattr(cf_solve, "solve", fake_solve)
    sess = MagicMock(); sess.headers = {}
    assert scr._invoke_cf_solver(sess, "https://www.fanfiction.net/s/1") is False
    assert called["n"] == 0


def test_invoke_cf_solver_handles_unavailable(cache_dir, scraper, monkeypatch):
    def raise_unavail(url, **_):
        raise cf_solve.SolverUnavailable("playwright missing")
    monkeypatch.setattr(cf_solve, "solve", raise_unavail)

    sess = MagicMock(); sess.headers = {}
    assert scraper._invoke_cf_solver(
        sess, "https://www.fanfiction.net/s/1",
    ) is False


def test_invoke_cf_solver_handles_generic_failure(
    cache_dir, scraper, monkeypatch,
):
    """A live solve can fail for a dozen reasons (network, browser
    crash, human-only captcha). None of them should propagate up and
    crash the caller's fetch loop — we log and fall back to the
    built-in retries."""
    def boom(url, **_):
        raise RuntimeError("browser crashed")
    monkeypatch.setattr(cf_solve, "solve", boom)

    sess = MagicMock(); sess.headers = {}
    assert scraper._invoke_cf_solver(
        sess, "https://www.fanfiction.net/s/1",
    ) is False


def test_seed_rechecks_cache_on_every_call_for_cross_thread_pickup(
    cache_dir, scraper, monkeypatch,
):
    """Regression: an earlier design cached "already tried to seed
    host X" in a per-scraper set. Under concurrent library updates
    that short-circuited every thread that ran its first 403 check
    *before* the worker that solved the challenge persisted cookies
    — those threads were marked 'seeded' with no cookies applied and
    never re-checked the disk cache, so each had to exhaust its own
    retry budget before failing. Fix: re-read the cache on every
    call; the cost is one JSON load per 403, which is negligible."""
    sess_a = MagicMock(); sess_a.headers = {}
    sess_b = MagicMock(); sess_b.headers = {}

    # First 403 for both threads: nothing in cache → both return False.
    assert scraper._maybe_seed_cf_cookies(sess_a, "https://www.fanfiction.net/s/1") is False
    assert scraper._maybe_seed_cf_cookies(sess_b, "https://www.fanfiction.net/s/2") is False

    # Worker A "solves" the challenge and persists cookies.
    cf_solve.persist("fanfiction.net", _sample_result())

    # Worker B's next 403 MUST pick up A's persisted cookies even
    # though _maybe_seed_cf_cookies already ran once for this scraper.
    applied: list = []
    monkeypatch.setattr(
        cf_solve,
        "inject_into_session",
        lambda s, r: applied.append(r),
    )
    assert scraper._maybe_seed_cf_cookies(
        sess_b, "https://www.fanfiction.net/s/3",
    ) is True
    assert len(applied) == 1


def test_persist_sets_restrictive_permissions(cache_dir):
    """cf_clearance is a session secret; on a shared Linux host the
    default umask would leave the cache file group/world-readable.
    The persist path chmods it to 0600 so other local users can't
    replay the token."""
    import os
    import stat
    import platform
    cf_solve.persist("perm-test.example", _sample_result())
    path = cf_solve._host_cache_path("perm-test.example")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if platform.system() == "Windows":
        # Windows ignores POSIX mode bits; we only assert on real
        # POSIX systems. The file still exists; that's all we can
        # guarantee cross-platform.
        assert path.exists()
    else:
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_fetch_403_then_solver_success_produces_body(
    cache_dir, monkeypatch,
):
    """End-to-end: the fetch loop hits 403, the on-disk cache path
    doesn't apply, the solver runs on the second-to-last attempt,
    cookies go into the session, and the next fetch returns 200.
    The loop returns the 200 body rather than exhausting retries."""
    from ficary.scraper import BaseScraper

    class _Scr(BaseScraper):
        site_name = "probe"

    # max_retries=3 so "attempt >= max_retries - 2" fires on attempt 1+.
    scr = _Scr(cf_solve=True, max_retries=3)

    # Mock the session's GET so attempts 1-2 return 403 and the third
    # (after the solver run) returns 200.
    body_iter = iter([
        MagicMock(status_code=403, text="forbidden", headers={}),
        MagicMock(status_code=403, text="forbidden", headers={}),
        MagicMock(status_code=200, text="<html>hello</html>", headers={}),
    ])

    fake_session = MagicMock()
    fake_session.get.side_effect = lambda *a, **kw: next(body_iter)
    fake_session.headers = {}
    fake_session.cookies.jar = []
    monkeypatch.setattr(scr, "_session", lambda: fake_session)
    # Avoid real sleeps
    monkeypatch.setattr("ficary.scraper.time.sleep", lambda *_: None)
    # Solver returns one cookie — just verify it was called.
    solved = {"n": 0}

    def fake_solve(url, **_):
        solved["n"] += 1
        return _sample_result()

    monkeypatch.setattr(cf_solve, "solve", fake_solve)
    monkeypatch.setattr(cf_solve, "inject_into_session", lambda s, r: None)
    monkeypatch.setattr(cf_solve, "persist", lambda h, r: None)

    body = scr._fetch("https://www.fanfiction.net/s/1")
    assert body == "<html>hello</html>"
    assert solved["n"] == 1


class TestLaunchKwargs:
    """The solver must launch a *visible* browser by default (headless is
    fingerprinted and blocked by Cloudflare's under-attack challenges) and
    strip the automation tells the challenge's bot probe looks for."""

    def test_visible_by_default(self, monkeypatch):
        monkeypatch.delenv("FICARY_CF_SOLVE_HEADLESS", raising=False)
        kw = cf_solve._launch_kwargs()
        assert kw["headless"] is False
        assert "--disable-blink-features=AutomationControlled" in kw["args"]
        assert "--enable-automation" in kw["ignore_default_args"]

    def test_env_forces_headless(self, monkeypatch):
        monkeypatch.setenv("FICARY_CF_SOLVE_HEADLESS", "1")
        assert cf_solve._launch_kwargs()["headless"] is True

    def test_non_one_value_stays_visible(self, monkeypatch):
        # Only the exact sentinel "1" opts into headless; anything else
        # (e.g. "0", "false") keeps the reliable visible default.
        monkeypatch.setenv("FICARY_CF_SOLVE_HEADLESS", "0")
        assert cf_solve._launch_kwargs()["headless"] is False




# ── browser-fetch (the robust path) ─────────────────────────────────

_FAKE_WORKER_OK = (
    "import argparse, json, sys" + chr(10) +
    "ap = argparse.ArgumentParser()" + chr(10) +
    "ap.add_argument('--url'); ap.add_argument('--out')" + chr(10) +
    "ap.add_argument('--profile', default=''); ap.add_argument('--timeout')" + chr(10) +
    "ap.add_argument('--headless', action='store_true')" + chr(10) +
    "a = ap.parse_args()" + chr(10) +
    "open(a.out, 'w', encoding='utf-8').write('<html>WORK ' + a.url + '</html>')" + chr(10) +
    "sys.stdout.write(json.dumps({'status': 'await_human', 'message': 'click'}) + chr(10))" + chr(10) +
    "sys.stdout.write(json.dumps({'status': 'ok', 'bytes': 10}) + chr(10))" + chr(10)
)

_FAKE_WORKER_TIMEOUT = (
    "import json, sys" + chr(10) +
    "sys.stdout.write(json.dumps({'status': 'timeout'}) + chr(10))" + chr(10) +
    "sys.exit(3)" + chr(10)
)

_FAKE_WORKER_NO_PW = (
    "import json, sys" + chr(10) +
    "sys.stdout.write(json.dumps({'status': 'error', 'error': "
    "'ModuleNotFoundError: No module named playwright'}) + chr(10))" + chr(10) +
    "sys.exit(1)" + chr(10)
)


def _install_fake_worker(monkeypatch, tmp_path, source):
    script = tmp_path / "fake_worker.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(cf_solve, "_worker_script", lambda: script)
    monkeypatch.setattr(cf_solve, "_worker_python", lambda: sys.executable)
    monkeypatch.setattr(cf_solve, "_browser_profile_dir", lambda: None)


def test_fetch_spawns_worker_and_returns_html(monkeypatch, tmp_path):
    _install_fake_worker(monkeypatch, tmp_path, _FAKE_WORKER_OK)
    seen = []
    html = cf_solve.fetch("https://example.invalid/x", log_callback=seen.append)
    assert html == "<html>WORK https://example.invalid/x</html>"
    assert any("click" in m for m in seen)


def test_fetch_timeout_raises_runtimeerror(monkeypatch, tmp_path):
    _install_fake_worker(monkeypatch, tmp_path, _FAKE_WORKER_TIMEOUT)
    with pytest.raises(RuntimeError, match="completed in time"):
        cf_solve.fetch("https://example.invalid/x")


def test_fetch_missing_playwright_raises_unavailable(monkeypatch, tmp_path):
    _install_fake_worker(monkeypatch, tmp_path, _FAKE_WORKER_NO_PW)
    with pytest.raises(cf_solve.SolverUnavailable):
        cf_solve.fetch("https://example.invalid/x")


def test_fetch_missing_worker_script_raises_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_solve, "_worker_script", lambda: tmp_path / "nope.py")
    with pytest.raises(cf_solve.SolverUnavailable, match="worker script missing"):
        cf_solve.fetch("https://example.invalid/x")


def test_real_worker_script_ships_on_disk():
    assert cf_solve._worker_script().exists()


# ── Worker probe/escalation logic (fake playwright) ──────────────


class _FakeSite:
    """Scripted challenge state shared across worker attempts."""

    def __init__(self, clears_headless: bool, clears_headed: bool = True):
        self.clears_headless = clears_headless
        self.clears_headed = clears_headed
        self.launches: list[bool] = []  # headless flag per launch


class _FakePage:
    def __init__(self, context):
        self.context = context

    def goto(self, url, **kw):
        pass

    def title(self):
        return "Ouroboros" if self.context._cleared() else "Just a moment..."

    def wait_for_timeout(self, ms):
        time.sleep(min(ms, 20) / 1000)

    def wait_for_load_state(self, *a, **kw):
        pass

    def content(self):
        return "<html>REAL PAGE</html>"


class _FakeContext:
    def __init__(self, site, headless):
        self._site = site
        self._headless = headless
        self.pages = []

    def _cleared(self):
        return (self._site.clears_headless if self._headless
                else self._site.clears_headed)

    def cookies(self):
        if self._cleared():
            return [{"name": "cf_clearance", "value": "tok"}]
        return []

    def new_page(self):
        return _FakePage(self)

    def add_init_script(self, script):
        pass

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, site, headless):
        self._site = site
        self._headless = headless

    def new_context(self):
        return _FakeContext(self._site, self._headless)

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, site):
        self._site = site

    def launch_persistent_context(self, profile, **kw):
        self._site.launches.append(kw["headless"])
        return _FakeContext(self._site, kw["headless"])

    def launch(self, **kw):
        self._site.launches.append(kw["headless"])
        return _FakeBrowser(self._site, kw["headless"])


class _FakePlaywrightCM:
    def __init__(self, site):
        self.chromium = _FakeChromium(site)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_playwright(monkeypatch, site):
    import types

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _FakePlaywrightCM(site)
    pw_mod = types.ModuleType("playwright")
    pw_mod.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", pw_mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    from ficary import _cf_worker

    emissions = []
    monkeypatch.setattr(_cf_worker, "_emit", emissions.append)
    monkeypatch.setattr(_cf_worker, "_PROBE_TIMEOUT_S", 0.05)
    out = tmp_path / "page.html"
    return _cf_worker, emissions, str(out)


def test_probe_clears_without_window(worker_env, monkeypatch):
    worker, emissions, out = worker_env
    site = _FakeSite(clears_headless=True)
    _install_fake_playwright(monkeypatch, site)
    rc = worker.run("https://a.example/w", out, "/prof", 5.0, headless=False)
    assert rc == 0
    assert site.launches == [True]  # single quiet headless pass
    statuses = [m["status"] for m in emissions]
    assert "opening" not in statuses and "await_human" not in statuses
    assert Path(out).read_text() == "<html>REAL PAGE</html>"


def test_probe_escalates_to_headed_window(worker_env, monkeypatch):
    worker, emissions, out = worker_env
    site = _FakeSite(clears_headless=False, clears_headed=True)
    _install_fake_playwright(monkeypatch, site)
    rc = worker.run("https://a.example/w", out, "/prof", 5.0, headless=False)
    assert rc == 0
    assert site.launches == [True, False]  # probe, then headed escalation
    statuses = [m["status"] for m in emissions]
    assert statuses.index("probe") < statuses.index("opening")
    assert Path(out).read_text() == "<html>REAL PAGE</html>"


def test_interactive_timeout_after_failed_probe(worker_env, monkeypatch):
    worker, emissions, out = worker_env
    site = _FakeSite(clears_headless=False, clears_headed=False)
    _install_fake_playwright(monkeypatch, site)
    rc = worker.run("https://a.example/w", out, "/prof", 0.05, headless=False)
    assert rc == 3
    assert [m["status"] for m in emissions][-1] == "timeout"


def test_forced_headless_skips_probe(worker_env, monkeypatch):
    worker, emissions, out = worker_env
    site = _FakeSite(clears_headless=True)
    _install_fake_playwright(monkeypatch, site)
    rc = worker.run("https://a.example/w", out, "/prof", 5.0, headless=True)
    assert rc == 0
    assert site.launches == [True]
    # Forced-headless is the interactive attempt, not a probe.
    assert [m["status"] for m in emissions][0] == "opening"


def test_no_profile_goes_straight_to_headed(worker_env, monkeypatch):
    worker, emissions, out = worker_env
    site = _FakeSite(clears_headless=False, clears_headed=True)
    _install_fake_playwright(monkeypatch, site)
    rc = worker.run("https://a.example/w", out, None, 5.0, headless=False)
    assert rc == 0
    assert site.launches == [False]  # no probe without a stored profile
