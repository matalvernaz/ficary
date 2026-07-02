"""Property/fuzz tests (round-10 H3).

Skipped entirely when hypothesis isn't installed (it's a dev-extra, not
a runtime dependency), so the suite stays green on a bare install. The
two production fixes these exercise — the canonical_url urlsplit guard
and the cache-loader RecursionError tuple — ship in the same change, so
this file is green on arrival.
"""
import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import assume, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from ficary import sites  # noqa: E402
from ficary.models import chapter_in_spec, parse_chapter_spec  # noqa: E402
from ficary.tts import _MAX_SEGMENT_CHARS, _split_oversized_text  # noqa: E402

FUZZ = settings(max_examples=150, deadline=None)  # ARM board: no per-example timeout


def _render(ranges):
    """Render a parsed spec back to a chapter-spec string."""
    parts = []
    for lo, hi in ranges:
        if hi is None:
            parts.append(f"{lo}-")
        elif hi == lo:
            parts.append(str(lo))
        else:
            parts.append(f"{lo}-{hi}")
    return ",".join(parts)


# ── (a) chapter-spec parser ───────────────────────────────────────
class TestChapterSpecProperties:
    @FUZZ
    @given(st.text())
    def test_total_safety(self, spec):
        """Returns None or a list of well-formed (lo, hi) tuples, or
        raises exactly ValueError — never anything else."""
        try:
            result = parse_chapter_spec(spec)
        except ValueError:
            return
        assert result is None or isinstance(result, list)
        for lo, hi in result or []:
            assert lo >= 1
            assert hi is None or hi >= lo

    @FUZZ
    @given(st.lists(
        st.tuples(st.integers(1, 10_000),
                  st.one_of(st.none(), st.integers(0, 500))),
        min_size=1, max_size=8,
    ))
    def test_render_parse_roundtrip_membership(self, raw):
        # Normalise hi to lo+delta so ranges are valid, then compare via
        # membership (the parser doesn't merge overlaps, so tuple-list
        # equality is the wrong assertion — chapter_in_spec is the
        # contract callers actually use).
        ranges = [(lo, None if d is None else lo + d) for lo, d in raw]
        reparsed = parse_chapter_spec(_render(ranges))
        assert reparsed is not None
        probe_max = max((hi if hi is not None else lo)
                        for lo, hi in ranges) + 5
        for n in range(1, probe_max + 1):
            assert chapter_in_spec(n, ranges) == chapter_in_spec(n, reparsed)

    @FUZZ
    @given(st.lists(st.integers(1, 999), min_size=1, max_size=6))
    def test_whitespace_invariance(self, nums):
        tight = ",".join(str(n) for n in nums)
        loose = " , ".join(f" {n} " for n in nums)
        assert parse_chapter_spec(tight) == parse_chapter_spec(loose)


# ── (b) URL predicates ────────────────────────────────────────────
_URL_JUNK = st.one_of(
    st.text(),
    st.builds(
        lambda scheme, host, path: f"{scheme}://{host}/{path}",
        st.sampled_from(["http", "https", "ftp", "", "javascript"]),
        st.sampled_from([
            "www.fanfiction.net", "archiveofourown.org",
            "[abc", "[::1", "host:notaport", "", "1.2.3.4",
            "hp.adult-fanfiction.org", "sexstories.com",
        ]),
        st.text(alphabet="abcdefghij0123456789/?=&._-", max_size=30),
    ),
)


class TestUrlPredicateProperties:
    @FUZZ
    @given(_URL_JUNK)
    @example("http://[abc")   # unterminated IPv6 literal — the crasher
    @example("http://[::1")
    def test_no_predicate_raises(self, url):
        # None of these may raise on arbitrary URL-ish input.
        sites.detect_scraper(url)
        sites.canonical_url(url)
        for name in ("is_author_url", "is_series_url"):
            fn = getattr(sites, name, None)
            if fn is not None:
                fn(url)

    @FUZZ
    @given(_URL_JUNK)
    @example("http://[abc")
    def test_canonical_url_idempotent(self, url):
        once = sites.canonical_url(url)
        assert sites.canonical_url(once) == once

    @FUZZ
    @given(_URL_JUNK)
    def test_detect_scraper_returns_a_scraper_class(self, url):
        from ficary.scraper import BaseScraper
        cls = sites.detect_scraper(url)
        assert isinstance(cls, type) and issubclass(cls, BaseScraper)


# ── (c) cache loaders ─────────────────────────────────────────────
class TestCacheLoaderProperties:
    # tmp_path (a pytest fixture) can't be mixed with @given — hypothesis
    # binds strategies to the trailing params and would collide. Each
    # example makes its own tempdir instead.
    def _scraper(self):
        import tempfile
        from pathlib import Path
        from ficary.scraper import FFNScraper
        return FFNScraper(
            cache_dir=Path(tempfile.mkdtemp(prefix="ficary-fuzz-")),
            use_cache=True,
        )

    @FUZZ
    @given(st.binary(max_size=2000))
    @example(b"[" * 20_000)      # RecursionError crasher
    @example(b'"a string"')
    @example(b"[]")
    @example(b"null")
    @example(b"\xff\xfegarbage")
    @example(b'{"title": "only-title"}')  # KeyError branch
    def test_chapter_loader_never_raises(self, payload):
        s = self._scraper()
        cache_dir = s._story_cache_dir(1)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "ch_0001.json"
        path.write_bytes(payload)
        result = s._load_chapter_cache(1, 1)  # must not raise
        if result is None:
            assert not path.exists()  # invalid file was unlinked
        else:
            from ficary.models import Chapter
            assert isinstance(result, Chapter)

    @FUZZ
    @given(st.binary(max_size=2000))
    @example(b"[" * 20_000)
    def test_meta_loader_never_raises(self, payload):
        s = self._scraper()
        cache_dir = s._story_cache_dir(2)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "meta.json"
        path.write_bytes(payload)
        result = s._load_meta_cache(2)
        assert result is None or isinstance(result, dict)


# ── (d) TTS chunking ──────────────────────────────────────────────
class TestSplitOversizedProperties:
    @FUZZ
    @given(st.text(), st.integers(5, 300))
    @example("x" * 950, 100)             # single overlong token
    @example("a\tb\tc " * 40, 50)        # tabs
    @example("   \n\n  ", 10)            # whitespace-only
    def test_chunks_bounded_and_content_preserved(self, text, max_len):
        parts = _split_oversized_text(text, max_len)
        for p in parts:
            assert len(p) <= max_len
        # The splitter collapses whitespace by design (see
        # test_tts_split), so compare whitespace-insensitively.
        import re
        joined = re.sub(r"\s+", "", "".join(parts))
        assert joined == re.sub(r"\s+", "", text)

    @FUZZ
    @given(st.text(min_size=1))
    def test_default_max_len_bound(self, text):
        for p in _split_oversized_text(text):
            assert len(p) <= _MAX_SEGMENT_CHARS
