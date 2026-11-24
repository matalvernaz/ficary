"""Ficary's own files when they are damaged: by a power cut part-way
through a write, by a full disk, or by a hand edit saved in the wrong
shape or encoding.

What each must do: Ficary keeps working, the damaged copy is kept
rather than written over, and the user is told. Files Ficary writes
itself go through ``ficary.atomic``, so a write that does not finish
leaves the previous copy in place.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from ficary import tts


def corrupt_copies(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if ".corrupt-" in p.name)


# ── Voice map: which voice each character gets ───────────────────


class TestVoiceMap:
    def test_a_map_saved_as_utf16_is_set_aside_instead_of_ending_the_render(self, tmp_path):
        """Notepad saves as UTF-16 when asked for "Unicode"."""
        path = tmp_path / "voices.json"
        original = json.dumps({"Harry": "edge:en-GB-RyanNeural"}).encode("utf-16")
        path.write_bytes(original)
        mapper = tts.VoiceMapper(path)
        assert mapper.mapping == {}
        [kept] = corrupt_copies(path)
        assert kept.read_bytes() == original

    def test_a_map_that_is_a_list_is_set_aside(self, tmp_path):
        path = tmp_path / "voices.json"
        path.write_text('["Harry", "Ron"]', encoding="utf-8")
        mapper = tts.VoiceMapper(path)
        assert mapper.mapping == {}
        [kept] = corrupt_copies(path)
        assert kept.read_text(encoding="utf-8") == '["Harry", "Ron"]'

    def test_a_voice_that_is_not_text_is_left_out(self, tmp_path):
        path = tmp_path / "voices.json"
        path.write_text(
            json.dumps({"Harry": "edge:en-GB-RyanNeural", "Ron": 5, "Hermione": None}),
            encoding="utf-8",
        )
        mapper = tts.VoiceMapper(path)
        assert mapper.mapping == {"Harry": "edge:en-GB-RyanNeural"}
        assert corrupt_copies(path) == []


# ── Pronunciation list: the user's own spellings for the voices ──


class TestPronunciationList:
    def test_suggestions_are_not_written_over_a_list_that_cannot_be_read(self, tmp_path):
        """The list is typed by hand; a missing comma made it unreadable,
        and the AI's suggestions used to replace everything in it."""
        path = tmp_path / ".ficary-pronunciations-7.json"
        typed = '{\n  "Hermione": "Her-my-oh-nee",,\n  "Siobhan": "Shiv-awn"\n}\n'
        path.write_text(typed, encoding="utf-8")
        assert tts._seed_pronunciation_file(path, {"Ron": "Ronn"}) is False
        assert path.read_text(encoding="utf-8") == typed

    @pytest.mark.parametrize("body", [b'["Hermione"]', '{"Hermione": "x"}'.encode("utf-16")], ids=["a list", "utf-16"])
    def test_a_list_in_the_wrong_shape_or_encoding_is_left_alone(self, tmp_path, body):
        path = tmp_path / ".ficary-pronunciations-7.json"
        path.write_bytes(body)
        assert tts._seed_pronunciation_file(path, {"Ron": "Ronn"}) is False
        assert path.read_bytes() == body

    def test_suggestions_join_the_users_own_entries_without_replacing_them(self, tmp_path):
        path = tmp_path / ".ficary-pronunciations-7.json"
        path.write_text(json.dumps({"_comment": "mine", "Hermione": "Her-my-oh-nee"}), encoding="utf-8")
        assert tts._seed_pronunciation_file(path, {"Hermione": "Hermy-own", "Ron": "Ronn"}) is True
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "_comment": "mine", "Hermione": "Her-my-oh-nee", "Ron": "Ronn",
        }

    def test_a_write_that_does_not_finish_keeps_the_old_list(self, tmp_path, monkeypatch):
        path = tmp_path / ".ficary-pronunciations-7.json"
        path.write_text(json.dumps({"_comment": "only the comment so far"}), encoding="utf-8")
        before = path.read_bytes()

        def power_cut(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(os, "replace", power_cut)
        with pytest.raises(OSError):
            tts._seed_pronunciation_file(path, {"Ron": "Ronn"})
        assert path.read_bytes() == before

    def test_the_render_writes_the_list_only_through_the_safe_paths(self):
        source = inspect.getsource(tts.generate_audiobook)
        assert "_seed_pronunciation_file(pron_path" in source
        assert "pron_path.write_text" not in source


# ── Soundscapes ──────────────────────────────────────────────────


@pytest.fixture
def soundscape_folder(monkeypatch, tmp_path):
    from ficary import portable

    folder = tmp_path / "soundscapes"
    folder.mkdir()
    monkeypatch.setattr(portable, "soundscapes_dir", lambda: folder)
    return folder


class TestSoundscapes:
    def test_a_save_that_does_not_finish_keeps_the_old_soundscape(self, soundscape_folder, monkeypatch):
        from ficary.soundscape import library
        from ficary.soundscape.model import Soundscape, Sound

        library.save(Soundscape(name="Rain", sounds=[Sound(source="bundled:rain")]))
        path = soundscape_folder / "rain.json"
        before = path.read_bytes()

        def power_cut(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(os, "replace", power_cut)
        with pytest.raises(OSError):
            library.save(Soundscape(name="Rain", sounds=[Sound(source="bundled:rain"), Sound(source="bundled:wind")]))
        assert path.read_bytes() == before

    def test_a_hand_edit_with_a_list_for_a_number_is_skipped_not_fatal(self, soundscape_folder):
        from ficary.soundscape import library

        (soundscape_folder / "mine.json").write_text(
            json.dumps({"name": "Mine", "sounds": [{"source": "rain", "azimuth": [1, 2]}]}),
            encoding="utf-8",
        )
        assert library.load("mine") is None
