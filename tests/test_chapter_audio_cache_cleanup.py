"""Finished chapter audio that no render has used for a while is cleared out.

Chapter audio is kept so a render that failed part-way can carry on, and so a book can be made again when chapters are added. Nothing used to remove it: every chapter of every audiobook ever made stayed on disk, about 22 MB for each hour of audio.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from ficary import tts
from ficary.models import Chapter, Story

DAY = 86400.0
NOW = 1_800_000_000.0


def cached(folder: Path, name: str, *, days_old: float, size: int = 1000) -> Path:
    """A file in the cache that was last used ``days_old`` days before NOW."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    used = NOW - days_old * DAY
    os.utime(path, (used, used))
    return path


@pytest.fixture
def base(tmp_path):
    return tmp_path / "cache" / "chapter_audio"


def sweep(base, keep_days=30.0):
    return tts._sweep_chapter_audio_cache(base=base, keep_days=keep_days, now=NOW)


class TestTheSweep:
    def test_audio_not_used_for_longer_than_the_keep_time_is_removed(self, base):
        old = cached(base / "v1" / "story_one", "aaa.mp3", days_old=45, size=4000)
        recent = cached(base / "v1" / "story_one", "bbb.mp3", days_old=3)
        assert sweep(base) == (1, 4000)
        assert not old.exists()
        assert recent.exists()

    def test_the_line_is_drawn_at_exactly_the_keep_time(self, base):
        on_the_line = cached(base / "v1" / "story", "on-the-line.mp3", days_old=30)
        just_inside = cached(base / "v1" / "story", "just-inside.mp3", days_old=30 - 1 / DAY)
        sweep(base)
        assert not on_the_line.exists()
        assert just_inside.exists()

    def test_a_story_folder_left_empty_is_removed_and_one_still_in_use_is_not(self, base):
        cached(base / "v1" / "finished_long_ago", "aaa.mp3", days_old=90)
        cached(base / "v1" / "finished_long_ago", "bbb.mp3", days_old=90)
        cached(base / "v1" / "still_going", "ccc.mp3", days_old=90)
        cached(base / "v1" / "still_going", "ddd.mp3", days_old=1)
        assert sweep(base)[0] == 3
        assert not (base / "v1" / "finished_long_ago").exists()
        assert [path.name for path in (base / "v1" / "still_going").iterdir()] == ["ddd.mp3"]

    def test_every_version_of_the_cache_is_cleared_the_same_way(self, base):
        """A folder from an older layout would otherwise never be visited again."""
        older_layout = cached(base / "v0" / "story", "aaa.mp3", days_old=200)
        assert sweep(base)[0] == 1
        assert not older_layout.exists()

    def test_only_chapter_audio_is_removed(self, base):
        notes = cached(base / "v1" / "story", "notes.txt", days_old=400)
        beside_the_versions = cached(base, "readme.mp3", days_old=400)
        beside_the_stories = cached(base / "v1", "stray.mp3", days_old=400)
        deeper = cached(base / "v1" / "story" / "deeper", "nested.mp3", days_old=400)
        assert sweep(base) == (0, 0)
        for kept in (notes, beside_the_versions, beside_the_stories, deeper):
            assert kept.exists()

    def test_a_link_is_never_followed_or_removed(self, base, tmp_path):
        outside = tmp_path / "outside"
        precious = cached(outside, "precious.mp3", days_old=400)
        story = base / "v1" / "story"
        story.mkdir(parents=True)
        (story / "link.mp3").symlink_to(precious)
        (base / "v1" / "linked_story").symlink_to(outside, target_is_directory=True)
        assert sweep(base) == (0, 0)
        assert precious.exists()
        assert (story / "link.mp3").is_symlink()

    def test_a_cache_that_is_not_there_is_nothing_to_do(self, tmp_path):
        assert sweep(tmp_path / "no-such-folder") == (0, 0)

    def test_keeping_forever_removes_nothing(self, base):
        ancient = cached(base / "v1" / "story", "aaa.mp3", days_old=2000)
        assert tts._sweep_chapter_audio_cache(base=base, keep_days=None, now=NOW) == (0, 0)
        assert ancient.exists()

    def test_a_shorter_keep_time_is_honoured(self, base):
        week_old = cached(base / "v1" / "story", "aaa.mp3", days_old=8)
        assert sweep(base, keep_days=7.0)[0] == 1
        assert not week_old.exists()

    def test_a_file_that_cannot_be_removed_does_not_stop_the_rest(self, base, monkeypatch):
        stuck = cached(base / "v1" / "story", "aaa-stuck.mp3", days_old=90)
        free = cached(base / "v1" / "story", "bbb-free.mp3", days_old=90, size=700)
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if self.name == "aaa-stuck.mp3":
                raise PermissionError("in use by another program")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert sweep(base) == (1, 700)
        assert stuck.exists()
        assert not free.exists()

    def test_what_was_removed_is_put_in_the_log_once(self, base, caplog):
        cached(base / "v1" / "story", "aaa.mp3", days_old=90, size=3_000_000)
        cached(base / "v1" / "story", "bbb.mp3", days_old=90, size=2_000_000)
        with caplog.at_level(logging.INFO, logger="ficary.tts"):
            sweep(base)
        lines = [record.getMessage() for record in caplog.records if "Chapter audio cache: removed" in record.getMessage()]
        assert len(lines) == 1
        assert "removed 2 chapters not used for 30 days, freeing 5 MB" in lines[0]

    def test_nothing_is_logged_when_nothing_was_removed(self, base, caplog):
        cached(base / "v1" / "story", "aaa.mp3", days_old=2)
        with caplog.at_level(logging.INFO, logger="ficary.tts"):
            sweep(base)
        assert "Chapter audio cache: removed" not in caplog.text


class TestTheSetting:
    def test_thirty_days_unless_told_otherwise(self, monkeypatch):
        monkeypatch.delenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", raising=False)
        assert tts._chapter_audio_keep_days() == 30.0

    @pytest.mark.parametrize("text, days", [("7", 7.0), (" 90 ", 90.0), ("0.5", 0.5)])
    def test_a_number_of_days_is_used(self, monkeypatch, text, days):
        monkeypatch.setenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", text)
        assert tts._chapter_audio_keep_days() == days

    @pytest.mark.parametrize("text", ["0", "forever", "never", "Forever"])
    def test_keeping_everything_can_be_asked_for(self, monkeypatch, text):
        monkeypatch.setenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", text)
        assert tts._chapter_audio_keep_days() is None

    @pytest.mark.parametrize("text", ["a month", "-3", "nan", "inf", "-inf"])
    def test_a_setting_that_cannot_be_read_gives_thirty_days(self, monkeypatch, text):
        """A typing mistake must not turn into keeping everything, or into deleting everything."""
        monkeypatch.setenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", text)
        assert tts._chapter_audio_keep_days() == 30.0


# ── In a render ──────────────────────────────────────────────────


def story(story_id: int, chapters: int) -> Story:
    return Story(
        id=story_id,
        title=f"Story {story_id}",
        author="Nobody In Particular",
        summary="",
        url=f"https://example.invalid/story/{story_id}",
        chapters=[
            Chapter(number=number, title=f"Part {number}", html=f"<p>This is the whole of part {number} of story {story_id}, and it is long enough to be spoken.</p>")
            for number in range(1, chapters + 1)
        ],
    )


@pytest.fixture
def render(monkeypatch, tmp_path):
    """A render with stand-ins for the voices and for ffmpeg, and its cache in a scratch folder."""
    base = tmp_path / "cache" / "chapter_audio"
    synthesised: list[int] = []

    async def chapter_audio(segments, voice_mapper, output_path, chapter_num=0, **options):
        synthesised.append(chapter_num)
        Path(output_path).write_bytes(b"chapter audio")
        return tts.ChapterAudioResult(True, 1, 1)

    async def no_heading(*args, **kwargs):
        return False

    def build_m4b(chapter_files, story, m4b_path, cover_path=None, intro_file=None):
        Path(m4b_path).write_bytes(b"audiobook")

    monkeypatch.delenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", raising=False)
    monkeypatch.setattr(tts, "_check_ffmpeg", lambda *args, **kwargs: None)
    monkeypatch.setattr(tts, "_chapter_audio_cache_base", lambda: base)
    monkeypatch.setattr(tts, "_attr_cache_root", lambda: tmp_path / "cache" / "attribution")
    monkeypatch.setattr(tts, "generate_chapter_audio", chapter_audio)
    monkeypatch.setattr(tts, "_synthesize_heading", no_heading)
    monkeypatch.setattr(tts, "build_m4b", build_m4b)

    def run(the_story):
        return tts.generate_audiobook(the_story, str(tmp_path / "out"), attribution_backend="builtin")

    run.base = base
    run.synthesised = synthesised
    return run


def make_old(folder: Path, days: float) -> None:
    used = time.time() - days * DAY
    for path in folder.iterdir():
        os.utime(path, (used, used))


class TestInARender:
    def test_a_book_made_again_keeps_its_chapters_and_does_not_synthesise_them_twice(self, render):
        render(story(1, 3))
        assert render.synthesised == [1, 2, 3]
        folder = render.base / "v1" / "1"
        assert len(list(folder.glob("*.mp3"))) == 3
        make_old(folder, 45)
        render.synthesised.clear()

        render(story(1, 3))
        # Reused, so they count as used today, and the clean-up at the end of this very render leaves them.
        assert render.synthesised == []
        files = list(folder.glob("*.mp3"))
        assert len(files) == 3
        assert all(time.time() - path.stat().st_mtime < 3600 for path in files)

    def test_another_books_old_chapters_go_and_its_recent_ones_stay(self, render):
        other = render.base / "v1" / "2"
        long_ago = cached(other, "long-ago.mp3", days_old=0)
        last_week = cached(other, "last-week.mp3", days_old=0)
        os.utime(long_ago, (time.time() - 60 * DAY,) * 2)
        os.utime(last_week, (time.time() - 6 * DAY,) * 2)

        render(story(1, 2))
        assert not long_ago.exists()
        assert last_week.exists()

    def test_asking_to_keep_everything_is_honoured(self, render, monkeypatch):
        monkeypatch.setenv("FICARY_CHAPTER_AUDIO_KEEP_DAYS", "forever")
        long_ago = cached(render.base / "v1" / "2", "long-ago.mp3", days_old=0)
        os.utime(long_ago, (time.time() - 600 * DAY,) * 2)
        render(story(1, 2))
        assert long_ago.exists()

    def test_a_clean_up_that_goes_wrong_does_not_fail_the_render(self, render, monkeypatch):
        def broken(**options):
            raise RuntimeError("the disk went away")

        monkeypatch.setattr(tts, "_sweep_chapter_audio_cache", broken)
        made = render(story(1, 2))
        assert Path(made).name.endswith(".m4b")
        assert Path(made).exists()

    def test_the_cache_folder_for_a_story_is_under_the_shared_base(self, render):
        assert tts._chapter_cache_root(77) == render.base / f"v{tts._CHAPTER_CACHE_VERSION}" / "77"
