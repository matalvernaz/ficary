"""Display numbering skips structural chapters (Prologue, Interlude, …).

Regression tests for the off-by-one Matt hit with FFN s/2567429
("Harry Potter and the Power of Revenge"): the fic opens with a
Prologue, so its first real chapter sat at stored number 2 and every
export labelled it "Chapter 2. A new beginning". Display numbering now
skips structural chapters, and the updater's heading parser accepts
both the display and the stored prefix so pre-fix exports round-trip
clean and heal on merge.
"""

from ficary.exporters import export_html, export_txt
from ficary.models import (
    Chapter,
    Story,
    chapter_display_numbers,
    format_chapter_heading,
    parse_chapter_heading,
)
from ficary.updater import read_chapters


def _prologue_story():
    s = Story(
        id=2567429, title="Power of Revenge", author="GoDD", summary="S",
        url="https://www.fanfiction.net/s/2567429/1/x",
    )
    for n, title in enumerate(
        ["Prologue", "A new beginning", "Defensive measures"], 1,
    ):
        s.chapters.append(Chapter(number=n, title=title, html=f"<p>Body {n}</p>"))
    return s


class TestChapterDisplayNumbers:
    def test_leading_prologue_does_not_consume_a_slot(self):
        ns = chapter_display_numbers(
            enumerate(["Prologue", "A new beginning", "Defensive measures"], 1)
        )
        assert ns[2] == 1
        assert ns[3] == 2

    def test_mid_book_interlude_does_not_consume_a_slot(self):
        ns = chapter_display_numbers(
            enumerate(["Prologue", "One", "Two", "Interlude: Rest", "Three"], 1)
        )
        assert ns[5] == 3

    def test_no_structural_chapters_is_identity(self):
        ns = chapter_display_numbers(enumerate(["A", "B", "C"], 1))
        assert ns == {1: 1, 2: 2, 3: 3}

    def test_partial_range_keeps_stored_numbers(self):
        # --chapters 5-10 must not restart the labels at 1.
        ns = chapter_display_numbers((n, f"Title {n}") for n in range(5, 11))
        assert ns == {n: n for n in range(5, 11)}

    def test_structural_with_subtitle_is_skipped(self):
        ns = chapter_display_numbers(
            enumerate(["Prologue: Before the Fall", "The Fall"], 1)
        )
        assert ns[2] == 1

    def test_author_chapter_titled_entry_consumes_a_slot(self):
        # A verbatim-rendered "Chapter One: X" is still a numbered chapter.
        ns = chapter_display_numbers(
            enumerate(["Prologue", "Chapter One: X", "The Fall"], 1)
        )
        assert ns[3] == 2


class TestParseChapterHeading:
    def test_strips_display_number_prefix(self):
        assert parse_chapter_heading(
            2, "Chapter 1. A new beginning", display_n=1,
        ) == "A new beginning"

    def test_strips_stored_number_prefix_from_old_exports(self):
        # Exports written before this fix carry the stored number.
        assert parse_chapter_heading(
            2, "Chapter 2. A new beginning", display_n=1,
        ) == "A new beginning"

    def test_foreign_chapter_prefix_kept_verbatim(self):
        assert parse_chapter_heading(
            3, "Chapter 9. Flashback", display_n=2,
        ) == "Chapter 9. Flashback"

    def test_no_display_n_behaves_positionally(self):
        assert parse_chapter_heading(3, "Chapter 3. The End") == "The End"


class TestExportHeadings:
    def test_html_export_labels_post_prologue_chapter_one(self, tmp_path):
        from pathlib import Path
        out = Path(export_html(_prologue_story(), str(tmp_path)))
        text = out.read_text(encoding="utf-8")
        assert "Chapter 1. A new beginning" in text
        assert "Chapter 2. A new beginning" not in text
        assert "Chapter 2. Defensive measures" in text
        # Anchors stay keyed to the stored number for merge-in-place.
        assert 'id="chapter-2"' in text

    def test_txt_export_labels_post_prologue_chapter_one(self, tmp_path):
        from pathlib import Path
        out = Path(export_txt(_prologue_story(), str(tmp_path)))
        text = out.read_text(encoding="utf-8")
        assert "--- Chapter 1. A new beginning ---" in text
        assert "--- Prologue ---" in text

    def test_html_round_trip_recovers_raw_titles(self, tmp_path):
        out = export_html(_prologue_story(), str(tmp_path))
        chapters = read_chapters(out)
        assert [c.title for c in chapters] == [
            "Prologue", "A new beginning", "Defensive measures",
        ]

    def test_old_export_heals_on_read(self, tmp_path):
        # Hand-build the pre-fix shape: positional heading after a Prologue.
        html = (
            '<div class="chapter" id="chapter-1"><h2>Prologue</h2>\n'
            "<p>Body 1</p></div><hr>\n"
            '<div class="chapter" id="chapter-2">'
            "<h2>Chapter 2. A new beginning</h2>\n"
            "<p>Body 2</p></div><hr>\n"
        )
        out = tmp_path / "old.html"
        out.write_text(html, encoding="utf-8")
        chapters = read_chapters(out)
        assert chapters[1].title == "A new beginning"


class TestReaderHeadings:
    def test_reader_from_file_shows_display_number(self, tmp_path):
        from ficary.reader.source import StorySource

        out = export_html(_prologue_story(), str(tmp_path))
        source = StorySource.from_file(out)
        assert source.load_chapter(1).heading == "Prologue"
        assert source.load_chapter(2).heading == "Chapter 1. A new beginning"

    def test_reader_from_cache_dir_shows_display_number(self, tmp_path):
        import json

        from ficary.reader.source import StorySource

        cache = tmp_path / "cache"
        cache.mkdir()
        for n, title in enumerate(["Prologue", "A new beginning"], 1):
            (cache / f"ch_{n:04d}.json").write_text(
                json.dumps({"title": title, "html": f"<p>Body {n}</p>"}),
                encoding="utf-8",
            )
        source = StorySource.from_cache_dir(
            cache, "https://www.fanfiction.net/s/2567429/1/x",
        )
        assert source.load_chapter(2).heading == "Chapter 1. A new beginning"
