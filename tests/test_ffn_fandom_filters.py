"""Tests for FFN fandom-browse character / world / pairing / exclusion /
time filters and per-fandom name resolution (no network)."""

import logging

import pytest

from ffn_dl.search import (
    _build_ffn_fandom_url,
    _parse_fandom_filter_options,
    _resolve_named,
    search_ffn,
)

# Minimal stand-in for the per-fandom filter <select>s FFN renders.
_FANDOM_OPTIONS_HTML = """
<select name="characterid1">
  <option value="0">All Characters (A)</option>
  <option value="1">Harry P.</option>
  <option value="3">Hermione G.</option>
  <option value="2">Ron W.</option>
  <option value="155">Tom R. Jr.</option>
</select>
<select name="verseid1">
  <option value="0">World: All</option>
  <option value="447">Hogwarts</option>
  <option value="490">Founders</option>
</select>
"""

OPTIONS = _parse_fandom_filter_options(_FANDOM_OPTIONS_HTML)


class TestParseFandomOptions:
    def test_characters_and_worlds_parsed(self):
        assert OPTIONS["characters"]["Harry P."] == "1"
        assert OPTIONS["characters"]["Hermione G."] == "3"
        assert OPTIONS["worlds"]["Hogwarts"] == "447"

    def test_all_option_dropped(self):
        # The value="0" "All" placeholder must not become a real option.
        assert "All Characters (A)" not in OPTIONS["characters"]
        assert "World: All" not in OPTIONS["worlds"]


class TestNameResolution:
    def test_exact_case_insensitive(self):
        assert _resolve_named("harry p.", OPTIONS["characters"], "character") == "1"

    def test_unique_substring(self):
        assert _resolve_named("Hermione", OPTIONS["characters"], "character") == "3"

    def test_numeric_id_passthrough(self):
        assert _resolve_named("999", OPTIONS["characters"], "character") == "999"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown character"):
            _resolve_named("Gandalf", OPTIONS["characters"], "character")

    def test_ambiguous_raises(self):
        # "r." appears in "Harry P."? no — use a substring matching two.
        with pytest.raises(ValueError, match="Ambiguous"):
            _resolve_named("h", OPTIONS["characters"], "character")


class TestFandomUrl:
    def test_characters_world_pairing_time_exclusions(self):
        url = _build_ffn_fandom_url(
            "book", "Harry-Potter",
            {
                "time": "updated 1 week", "characters": "Harry P., Hermione G.",
                "world": "Hogwarts", "exclude_characters": "Ron W.",
                "exclude_world": "Founders", "exclude_genre": "horror",
                "pairing": True, "min_words": "10k+",
            },
            page=1, options=OPTIONS,
        )
        for frag in ("t=2", "c1=1", "c2=3", "v1=447", "_c1=2", "_v1=490",
                     "_g1=8", "pm=1", "len=10"):
            assert frag in url, f"missing {frag} in {url}"

    def test_caps_characters_at_four(self):
        url = _build_ffn_fandom_url(
            "book", "Harry-Potter",
            {"characters": "Harry P., Hermione G., Ron W., Tom R. Jr., Harry P."},
            page=1, options=OPTIONS,
        )
        assert "c4=" in url and "c5=" not in url

    def test_word_length_uses_len_not_words(self):
        # Regression guard: fandom browse must send ``len`` (was a latent
        # ``w=`` bug that silently no-op'd the word-length filter).
        url = _build_ffn_fandom_url("book", "Harry-Potter", {"min_words": "5k+"}, page=1)
        assert "len=5" in url and "w=" not in url


class TestKeywordModeWarning:
    def test_warns_on_fandom_only_filters_without_fandom(self, monkeypatch, caplog):
        from ffn_dl import search as S
        monkeypatch.setattr(S, "_build_search_url", lambda *a, **k: "http://x")
        monkeypatch.setattr(S, "_fetch_search_page", lambda url: "<html></html>")
        monkeypatch.setattr(S, "_parse_results", lambda html: [])
        with caplog.at_level(logging.WARNING):
            search_ffn("dragons", characters="Harry P.")
        assert any("fandom-only filters" in r.message for r in caplog.records)
