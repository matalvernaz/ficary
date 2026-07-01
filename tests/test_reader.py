"""Tests for the in-app reader (source, state, chunker, theme)."""
import json

import pytest

from ficary.models import Chapter
from ficary.reader import chunker, source, theme
from ficary.reader.state import ReaderStateDB


class TestStorySourceCache:
    def _make_cache(self, tmp_path):
        d = tmp_path / "ffn_12345"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": "T", "author": "A"}), encoding="utf-8")
        (d / "ch_0001.json").write_text(
            json.dumps({"title": "Beginnings", "html": "<p>Hello</p><p>World</p>"}), encoding="utf-8")
        (d / "ch_0002.json").write_text(
            json.dumps({"title": "", "html": "<p>Second</p>"}), encoding="utf-8")
        return d

    def test_counts_and_loads(self, tmp_path):
        d = self._make_cache(tmp_path)
        src = source.StorySource.from_cache_dir(d, "https://www.fanfiction.net/s/12345/1/T")
        assert src.chapter_count() == 2
        assert src.title == "T" and src.author == "A"
        assert "12345" in src.story_key

        ch1 = src.load_chapter(1)
        assert ch1.heading == "Chapter 1. Beginnings"
        assert "Hello" in ch1.text and "World" in ch1.text
        # paragraphs preserved as a blank-line gap
        assert "\n\n" in ch1.text

        ch2 = src.load_chapter(2)
        assert ch2.heading == "Chapter 2"  # empty title collapses

    def test_memoizes(self, tmp_path):
        d = self._make_cache(tmp_path)
        src = source.StorySource.from_cache_dir(d, "https://www.fanfiction.net/s/12345")
        assert src.load_chapter(1) is src.load_chapter(1)

    def test_empty_cache_raises(self, tmp_path):
        d = tmp_path / "ffn_9"
        d.mkdir()
        with pytest.raises(source.ReaderSourceError):
            source.StorySource.from_cache_dir(d, "https://www.fanfiction.net/s/9")


class TestStorySourceFile:
    def test_from_file_delegates_to_read_chapters(self, tmp_path, monkeypatch):
        chapters = [Chapter(1, "One", "<p>Alpha</p>"), Chapter(2, "Two", "<p>Beta</p>")]
        monkeypatch.setattr("ficary.updater.read_chapters", lambda p: chapters)
        f = tmp_path / "story.html"
        f.write_text("x", encoding="utf-8")

        src = source.StorySource.from_file(f, title="My Fic", author="Me")
        assert src.chapter_count() == 2
        assert src.title == "My Fic"
        assert "Alpha" in src.load_chapter(1).text


class TestReaderState:
    def test_position_round_trip(self, tmp_path):
        db = ReaderStateDB(tmp_path / "r.db")
        assert db.load_position("k") is None
        db.save_position("k", 5, 120, title="T")
        assert db.load_position("k") == (5, 120)
        db.save_position("k", 6, 0)
        assert db.load_position("k") == (6, 0)
        db.close()

    def test_bookmarks(self, tmp_path):
        db = ReaderStateDB(tmp_path / "r.db")
        bid = db.add_bookmark("k", "spicy bit", 3, 42, "…excerpt…")
        marks = db.list_bookmarks("k")
        assert len(marks) == 1
        assert marks[0].name == "spicy bit" and marks[0].chapter_number == 3
        db.delete_bookmark(bid)
        assert db.list_bookmarks("k") == []
        db.close()

    def test_bookmarks_ordered_by_position(self, tmp_path):
        db = ReaderStateDB(tmp_path / "r.db")
        db.add_bookmark("k", "b", 5, 0)
        db.add_bookmark("k", "a", 2, 10)
        chapters = [m.chapter_number for m in db.list_bookmarks("k")]
        assert chapters == [2, 5]
        db.close()


class TestChunker:
    def test_offsets_are_exact(self, tmp_path):
        text = "First para.\n\nSecond para here."
        chunks = chunker.chunk_text(text)
        assert len(chunks) == 2
        for c in chunks:
            assert text[c.start:c.end] == c.text

    def test_oversized_paragraph_sub_splits(self):
        para = "This is a sentence. " * 40  # ~800 chars, one paragraph
        chunks = chunker.chunk_text(para, max_chars=200)
        assert len(chunks) > 1
        for c in chunks:
            assert para[c.start:c.end] == c.text

    def test_blank_input(self):
        assert chunker.chunk_text("") == []
        assert chunker.chunk_text("\n\n   \n\n") == []


class TestTheme:
    def test_palette_has_required_keys(self):
        for name in theme.THEMES:
            pal = theme.palette(name)
            assert {"fg", "bg", "hl_fg", "hl_bg"} <= set(pal)

    def test_unknown_theme_falls_back(self):
        assert theme.palette("nonexistent") == theme.palette(theme.DEFAULT_THEME)

    def test_next_theme_cycles(self):
        seen = [theme.DEFAULT_THEME]
        for _ in theme.THEMES:
            seen.append(theme.next_theme(seen[-1]))
        assert set(seen) == set(theme.THEMES)

    def test_clamp_font(self):
        assert theme.clamp_font_pt(2) == theme.MIN_FONT_PT
        assert theme.clamp_font_pt(999) == theme.MAX_FONT_PT
        assert theme.clamp_font_pt("bad") == theme.DEFAULT_FONT_PT
        assert theme.clamp_font_pt(16) == 16
