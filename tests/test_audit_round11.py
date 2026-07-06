"""Round-11 audit fixes — regression tests.

Grouped here the way prior audit rounds kept their additions together.
Covers: the auto-updater cumulative changelog, DownloadJob.from_prefs
seeding + speech_rate typing (H-1/H-2), manifest-owned heal snapshots
(H-3), narrator fallback on a signal-less accent (M-1), XHTML ampersand
preservation (M-4), stat-report tolerance + by-rating render (L-2/3/4),
template error surfacing (L-5), and _resolve_name robustness (L-10).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ficary import heal_manifest as hm
from ficary import self_update as su
from ficary.jobs import DownloadJob, _coerce_speech_rate
from ficary.tts_providers import VoiceInfo


@pytest.fixture
def portable_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("ficary.portable.portable_root", lambda: tmp_path)
    return tmp_path


class _FakePrefs:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)

    def get_bool(self, key, default=False):
        return bool(self._v.get(key, default))


def _rel(tag, body="", prerelease=False):
    return {"tag_name": tag, "body": body, "prerelease": prerelease}


def _voice(vid, locale, gender):
    return VoiceInfo(
        id=vid, provider="edge", short_name=vid.split(":")[-1],
        locale=locale, gender=gender, display=vid,
    )


# ── Auto-updater cumulative changelog ────────────────────────────────

class TestChangelogSince:
    def test_aggregates_newest_first_excludes_current_and_older(self):
        rels = [_rel("v2.7.2", "fix Y"), _rel("v2.8.0", "feat X"),
                _rel("v2.7.1", "your version"), _rel("v2.7.0", "older")]
        out = su.fetch_changelog_since("2.7.1", transport=lambda: rels)
        assert out.index("v2.8.0") < out.index("v2.7.2")
        assert "your version" not in out and "older" not in out
        assert "feat X" in out and "fix Y" in out

    def test_semver_order_not_lexical(self):
        rels = [_rel("v2.9.0", "mid"), _rel("v2.10.0", "later")]
        out = su.fetch_changelog_since("2.8.0", transport=lambda: rels)
        assert out.index("v2.10.0") < out.index("v2.9.0")

    def test_skips_prereleases(self):
        rels = [_rel("v2.8.0-beta", "beta", prerelease=True),
                _rel("v2.8.0", "stable")]
        out = su.fetch_changelog_since("2.7.1", transport=lambda: rels)
        assert "beta" not in out and "stable" in out

    def test_nothing_newer_returns_empty(self):
        out = su.fetch_changelog_since("2.7.1", transport=lambda: [_rel("v2.7.0")])
        assert out == ""

    def test_network_failure_returns_empty(self):
        def boom():
            raise RuntimeError("network down")
        assert su.fetch_changelog_since("2.7.1", transport=boom) == ""

    def test_truncates_and_notes_remainder(self):
        rels = [_rel(f"v2.8.{i}", f"body{i}") for i in range(40)]
        out = su.fetch_changelog_since(
            "2.7.0", transport=lambda: rels, max_versions=5)
        assert "older release" in out


# ── from_prefs seeding + speech_rate typing (H-1 / H-2) ──────────────

class TestFromPrefs:
    def test_speech_rate_default_is_int(self):
        job = DownloadJob()
        assert job.speech_rate == 0 and isinstance(job.speech_rate, int)

    @pytest.mark.parametrize("value,expected", [
        ("0", 0), ("-15", -15), ("-15%", -15), ("", 0),
        (None, 0), ("junk", 0), (10, 10), ("  20  ", 20),
    ])
    def test_coerce_speech_rate(self, value, expected):
        assert _coerce_speech_rate(value) == expected

    def test_seeds_cookies_and_int_rate(self, monkeypatch):
        import ficary.prefs as p
        fake = _FakePrefs({
            p.KEY_AO3_COOKIE: "sess=abc",
            p.KEY_WEBNOVEL_COOKIE: "wn=xyz",
            p.KEY_FICHUB: True,
            p.KEY_SPEECH_RATE: "-10",
            p.KEY_ATTRIBUTION_BACKEND: "fastcoref",
            p.KEY_ATTRIBUTION_MODEL_SIZE: "big",
        })
        monkeypatch.setattr("ficary.prefs.Prefs", lambda: fake)
        job = DownloadJob.from_prefs()
        assert job.ao3_cookie == "sess=abc"
        assert job.webnovel_cookie == "wn=xyz"
        assert job.fichub is True
        assert job.speech_rate == -10 and isinstance(job.speech_rate, int)
        assert job.attribution == "fastcoref"
        assert job.attribution_model_size == "big"

    def test_empty_cookie_pref_becomes_none(self, monkeypatch):
        # None (not "") so _build_scraper's FICARY_*_COOKIE env fallback
        # still applies for cron/CLI users.
        import ficary.prefs as p
        monkeypatch.setattr("ficary.prefs.Prefs",
                            lambda: _FakePrefs({p.KEY_AO3_COOKIE: ""}))
        assert DownloadJob.from_prefs().ao3_cookie is None


# ── Manifest-owned heal snapshots (H-3) ──────────────────────────────

class TestHealSnapshotLifecycle:
    def test_capture_snapshot_is_manifest_owned(self, portable_tmp):
        src = portable_tmp / "library-index.json"
        src.write_text('{"v":1}')
        snap = hm.capture_snapshot(src, "index")
        assert snap is not None and snap.exists()
        assert snap.read_text() == '{"v":1}'
        assert snap.parent == hm.snapshot_dir()
        assert "backup-" not in snap.name  # NOT the rolling backup pool

    def test_capture_missing_source_returns_none(self, portable_tmp):
        assert hm.capture_snapshot(portable_tmp / "nope.json", "watchlist") is None

    def test_write_before_update_after_round_trip(self, portable_tmp):
        m = hm.HealManifest(label="--doctor --heal-all",
                            index_snapshot="/x/snap.json")
        hm.write_manifest(m)
        assert m.path
        m.dropped_index_entries = 4
        hm.update_manifest(m)
        loaded = hm.load_manifest(Path(m.path))
        assert loaded is not None and loaded.dropped_index_entries == 4

    def test_pruning_manifest_unlinks_owned_snapshot(self, portable_tmp):
        src = portable_tmp / "idx.json"
        src.write_text("{}")
        snaps = []
        for i in range(hm._MAX_MANIFESTS + 3):
            s = hm.capture_snapshot(src, f"k{i}")
            snaps.append(s)
            hm.write_manifest(hm.HealManifest(label=f"m{i}",
                                              index_snapshot=str(s)))
        assert len(hm.list_manifests()) == hm._MAX_MANIFESTS
        # The pruned manifests' owned snapshots are gone in lockstep.
        assert sum(1 for s in snaps if s.exists()) == hm._MAX_MANIFESTS


# ── Narrator fallback on a signal-less accent (M-1) ──────────────────

class TestNarratorFallback:
    def test_signalless_accent_prefers_fallback_language(self, monkeypatch):
        from ficary import character_profile as cp
        catalog = [
            _voice("edge:af-ZA-WillemNeural", "af-ZA", "Male"),  # alpha-first
            _voice("edge:en-US-GuyNeural", "en-US", "Male"),
        ]
        monkeypatch.setattr("ficary.tts_providers.all_voices",
                            lambda providers=None: catalog)
        picked = cp.pick_narrator_voice_for_profile(
            profile={"gender": "male", "accent": "any"},
            enabled_providers=None, fallback="edge:en-US-AriaNeural")
        assert picked == "edge:en-US-GuyNeural"  # NOT the af-ZA catalog[0]

    def test_no_matching_language_returns_fallback(self, monkeypatch):
        from ficary import character_profile as cp
        catalog = [_voice("edge:af-ZA-WillemNeural", "af-ZA", "Male")]
        monkeypatch.setattr("ficary.tts_providers.all_voices",
                            lambda providers=None: catalog)
        picked = cp.pick_narrator_voice_for_profile(
            profile={"gender": "male", "accent": ""},
            enabled_providers=None, fallback="edge:en-US-AriaNeural")
        assert picked == "edge:en-US-AriaNeural"

    def test_specific_accent_still_matches(self, monkeypatch):
        from ficary import character_profile as cp
        catalog = [_voice("edge:en-GB-RyanNeural", "en-GB", "Male"),
                   _voice("edge:en-US-GuyNeural", "en-US", "Male")]
        monkeypatch.setattr("ficary.tts_providers.all_voices",
                            lambda providers=None: catalog)
        picked = cp.pick_narrator_voice_for_profile(
            profile={"gender": "male", "accent": "en-gb"},
            enabled_providers=None, fallback="edge:en-US-AriaNeural")
        assert picked == "edge:en-GB-RyanNeural"


# ── XHTML ampersand preservation (M-4) ───────────────────────────────

class TestXhtmlSanitizeAmpersand:
    @pytest.mark.parametrize("html,needle", [
        ("AT&T", "AT&amp;T"),
        ("Plain text ending in Q&A", "Q&amp;A"),
        ("<p>done</p> R&D", "R&amp;D"),
    ])
    def test_trailing_bare_ampersand_preserved(self, html, needle):
        from ficary.exporters import _xhtml_sanitize
        assert needle in _xhtml_sanitize(html)

    def test_bug_case_no_longer_drops_text(self):
        from ficary.exporters import _xhtml_sanitize
        assert "ATT" not in _xhtml_sanitize("AT&T")

    def test_entities_preserved_and_idempotent(self):
        from ficary.exporters import _xhtml_sanitize
        out = _xhtml_sanitize("caf&eacute; &amp; tea &#8212; end")
        assert _xhtml_sanitize(out) == out


# ── Stat-report robustness + by-rating render (L-2 / L-3 / L-4) ──────

class _FakeIndex:
    def __init__(self, entries):
        self._entries = entries

    def stories_in(self, root):
        return list(self._entries.items())

    def untrackable_in(self, root):
        return []


class TestStatsRobustness:
    def test_non_str_timestamps_do_not_crash(self):
        from ficary.library.stats import compute_stats
        idx = _FakeIndex({
            "u1": {"last_probed": 1751000000, "last_checked": 20260705,
                   "chapter_count": 3, "rating": "M"},
        })
        stats = compute_stats(Path("/lib"), idx)  # must not raise
        assert stats.total_stories == 1

    def test_by_rating_rendered(self):
        from ficary.library.stats import compute_stats
        idx = _FakeIndex({
            "u1": {"rating": "M", "chapter_count": 1},
            "u2": {"rating": "E", "chapter_count": 1},
        })
        assert "By rating:" in compute_stats(Path("/lib"), idx).summary()


# ── Template error surfacing (L-5) + _resolve_name (L-10) ────────────

class TestMisc:
    def test_malformed_template_raises_valueerror(self):
        from ficary.library.template import render
        from ficary.updater import FileMetadata
        md = FileMetadata(title="T", author="A", fandoms=["F"], format="epub")
        with pytest.raises(ValueError):
            render(md, template="{title")        # unbalanced brace
        with pytest.raises(ValueError):
            render(md, template="{0}/{title}")   # positional field

    def test_resolve_name_empty_cast_entry_no_crash(self):
        from ficary.character_profile import _resolve_name
        # empty/whitespace cast keys must not IndexError on .split()[0]
        assert _resolve_name("harry", {"": "", "  ": "  "}) is None
        assert _resolve_name(
            "harry", {"harry potter": "Harry Potter"}) == "Harry Potter"


# ── Watchlist store reload-before-mutate merge (M-8) ─────────────────

class TestWatchlistStoreMerge:
    def _watch(self, target):
        from ficary.watchlist import WATCH_TYPE_STORY, Watch
        return Watch(type=WATCH_TYPE_STORY, target=target)

    def test_add_reloads_and_merges(self, tmp_path):
        # A GUI add and a poll add through separate store instances must
        # not clobber each other (the lost-update race M-8 fixes).
        from ficary.watchlist import WatchlistStore
        p = tmp_path / "wl.json"
        poll = WatchlistStore(p)
        gui = WatchlistStore(p)
        poll.add(self._watch("https://x/a"))       # disk: [A]
        gui.add(self._watch("https://x/b"))         # gui reloads -> [A, B]
        poll.add(self._watch("https://x/c"))        # poll's snapshot was [A]
        disk = WatchlistStore(p)
        disk.reload()
        assert {w.target for w in disk.all()} == {
            "https://x/a", "https://x/b", "https://x/c"}

    def test_update_noops_when_removed_concurrently(self, tmp_path):
        # A concurrent delete beats a poll's cooldown update; update() must
        # not resurrect the deleted watch.
        from ficary.watchlist import WatchlistStore
        p = tmp_path / "wl.json"
        s1 = WatchlistStore(p)
        s2 = WatchlistStore(p)
        a = self._watch("https://x/a")
        s1.add(a)
        s2.reload()                                 # s2 sees [A]
        assert s1.remove(a.id) is True              # disk: []
        s2.update(a)                                # A gone on disk -> no-op
        disk = WatchlistStore(p)
        disk.reload()
        assert disk.all() == []


# ── Watchlist auto-download routes through the per-site queue (M-7) ──

class TestWatchDownloadQueueRouting:
    def test_routes_through_download_queues(self, tmp_path, monkeypatch):
        from concurrent.futures import Future

        from ficary import cli
        from ficary.library.index import LibraryIndex
        from ficary.watchlist import WATCH_TYPE_STORY, PollResult, Watch

        monkeypatch.setattr(cli, "check_format_deps", lambda fmt: None)
        monkeypatch.setattr("ficary.prefs.Prefs", lambda: _FakePrefs({}))
        LibraryIndex.load(tmp_path / "index.json").save()
        monkeypatch.setattr("ficary.library.index.default_index_path",
                            lambda: tmp_path / "index.json")

        ran = {}

        def fake_dl(url, job, output_dir, *, update_path=None,
                    on_export=None, **kw):
            ran["url"] = url
            if on_export:
                on_export(output_dir / "f.epub")
            return True

        monkeypatch.setattr(cli, "_download_one", fake_dl)

        captured = {}

        def fake_enqueue(site_name, job_fn, dedupe_key=None):
            captured["site"] = site_name
            captured["dedupe_key"] = dedupe_key
            fut = Future()
            fut.set_result(job_fn())  # run inline for the test
            return fut

        monkeypatch.setattr(cli.DownloadQueues, "enqueue",
                            staticmethod(fake_enqueue))

        dl = cli.make_watch_downloader(_FakePrefs({}))
        url = "https://www.fanfiction.net/s/123"
        saved = dl(Watch(type=WATCH_TYPE_STORY, target=url),
                   PollResult(watch_id="x", ok=True, new_items=[url]))

        assert captured.get("site")        # a per-site queue was used
        assert captured.get("dedupe_key")  # single-flight key passed
        assert ran["url"] == url           # the download actually ran
        assert saved                       # and its path was collected
