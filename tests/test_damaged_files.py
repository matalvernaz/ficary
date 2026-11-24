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


# ── The library index ────────────────────────────────────────────

from ficary.library import index as index_module  # noqa: E402
from ficary.library.index import LibraryIndex, SCHEMA_VERSION  # noqa: E402

DAMAGED_INDEXES = {
    "empty": b"",
    "all zeros": b"\x00" * 512,
    "cut off": b'{"version": 1, "libraries": {"/home/someone/Stories": {"stori',
    "utf-16": json.dumps({"version": 1, "libraries": {}}).encode("utf-16"),
    # json.loads raises RecursionError for this, not a ValueError.
    "nested too deep": b"[" * 200_000,
}


@pytest.fixture
def told(monkeypatch):
    """Every line the index has told the user, and a clean slate for "once per run"."""
    lines: list[str] = []
    monkeypatch.setattr(index_module, "_told", set(), raising=False)
    real = index_module.logger.warning

    def warning(message, *args, **kwargs):
        lines.append(message % args if args else message)
        real(message, *args, **kwargs)

    monkeypatch.setattr(index_module.logger, "warning", warning)
    return lines


def set_aside(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.stem}.unreadable-*{path.suffix}"))


class TestLibraryIndex:
    @pytest.mark.parametrize("body", DAMAGED_INDEXES.values(), ids=DAMAGED_INDEXES.keys())
    def test_a_damaged_index_is_kept_aside_and_a_new_one_started(self, tmp_path, told, body):
        """It used to refuse every save from then on, with nothing in the
        app saying why the library looked empty."""
        path = tmp_path / "library-index.json"
        path.write_bytes(body)
        index = LibraryIndex.load(path)
        assert index.save_blocker is None
        assert index.library_roots() == []
        [kept] = set_aside(path)
        assert kept.read_bytes() == body
        index.save()
        assert json.loads(path.read_text(encoding="utf-8"))["version"] == SCHEMA_VERSION
        assert kept.read_bytes() == body
        [line] = told
        assert kept.name in line and "Import Existing Folder" in line

    def test_an_index_that_cannot_be_moved_aside_is_not_written_over(self, tmp_path, told, monkeypatch):
        path = tmp_path / "library-index.json"
        path.write_bytes(b"\x00" * 64)

        def refuse(self, target):
            raise PermissionError("The process cannot access the file")

        monkeypatch.setattr(Path, "rename", refuse)
        index = LibraryIndex.load(path)
        assert index.save_blocker is not None
        with pytest.raises(RuntimeError):
            index.save()
        assert path.read_bytes() == b"\x00" * 64
        LibraryIndex.load(path)
        [line] = told
        assert "move it aside" in line

    def test_an_index_another_program_has_open_is_left_alone(self, tmp_path, told, monkeypatch):
        path = tmp_path / "library-index.json"
        good = json.dumps({"version": SCHEMA_VERSION, "libraries": {}})
        path.write_text(good, encoding="utf-8")
        real_read = Path.read_text

        def locked(self, *args, **kwargs):
            if self == path:
                raise PermissionError("The process cannot access the file because it is being used by another process")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", locked)
        index = LibraryIndex.load(path)
        LibraryIndex.load(path)
        assert index.save_blocker is not None
        assert set_aside(path) == []
        assert real_read(path, encoding="utf-8") == good
        [line] = told
        assert "could not open" in line

    def test_two_loads_racing_on_one_damaged_file(self, tmp_path, told, monkeypatch):
        """The library list and the launch check can load at the same moment."""
        path = tmp_path / "library-index.json"
        path.write_bytes(b"")
        real_rename = Path.rename

        def the_other_one_got_there_first(self, target):
            real_rename(self, tmp_path / "library-index.unreadable-elsewhere.json")
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(Path, "rename", the_other_one_got_there_first)
        index = LibraryIndex.load(path)
        assert index.save_blocker is None
        index.save()
        assert path.exists()

    def test_a_newer_index_is_copied_once_however_often_it_is_loaded(self, tmp_path, told):
        """Each load used to take another copy of it until the first save,
        and ten copies pushed the backups taken before a heal out of the
        rotation."""
        from ficary.library.backup import list_backups

        path = tmp_path / "library-index.json"
        earlier = tmp_path / "library-index.backup-20260101-000000.json"
        earlier.write_text(json.dumps({"version": SCHEMA_VERSION, "libraries": {}}), encoding="utf-8")
        path.write_text(json.dumps({"version": SCHEMA_VERSION + 1, "libraries": {}}), encoding="utf-8")
        for _ in range(12):
            LibraryIndex.load(path)
        backups = list_backups(path)
        assert earlier in backups
        assert len(backups) == 2
        assert len(told) == 1 and "newer version" in told[0]

    def test_discarding_a_bad_index_from_the_command_line_sticks(self, tmp_path, told, monkeypatch):
        """``--discard-bad-index`` cleared the block in memory and exited
        without saving, so the next run was blocked again."""
        from ficary import cli

        path = tmp_path / "library-index.json"
        path.write_bytes(b"\x00" * 64)
        monkeypatch.setattr(index_module, "default_index_path", lambda: path)

        def refuse(self, target):
            raise PermissionError("The process cannot access the file")

        monkeypatch.setattr(Path, "rename", refuse)
        with pytest.raises(SystemExit) as exit_info:
            cli._handle_discard_bad_index()
        assert exit_info.value.code == 0
        assert LibraryIndex.load(path).save_blocker is None


wx = pytest.importorskip("wx")


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")
def test_a_damaged_index_found_while_the_window_opens_is_in_the_status_pane(wx_app, told):
    """The library list first loads while the window is still being built,
    before the status pane is listening, so the line went nowhere."""
    from ficary import gui

    path = index_module.default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 512)
    frame = gui.MainFrame()
    try:
        for _ in range(20):
            wx.SafeYield()
        # The pane's timer may already have moved the lines out of the
        # queue and into the pane itself.
        with frame._log_lock:
            text = frame.log_ctrl.GetValue() + "".join(frame._log_queue)
        [kept] = set_aside(path)
        assert f"kept the file as {kept.name}" in text
        assert "Import Existing Folder" in text
    finally:
        frame._clip_timer.Stop()
        frame.Destroy()
        wx.SafeYield()


# ── The settings file ────────────────────────────────────────────

from ficary import portable  # noqa: E402
from ficary import prefs as prefs_module  # noqa: E402

SOUND_SETTINGS = b"library_path=/home/someone/Stories\nformat=epub\n"


@pytest.fixture
def settings_file(monkeypatch):
    """The settings file in this test's data folder, and a run that has
    not checked it yet."""
    monkeypatch.setattr(prefs_module, "_settings_checked", False, raising=False)
    path = portable.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def warnings_from(caplog, name):
    return [record.getMessage() for record in caplog.records if record.name == name and record.levelname == "WARNING"]


class TestSettings:
    def test_a_sound_settings_file_is_kept_as_the_last_good_copy(self, settings_file):
        settings_file.write_bytes(SOUND_SETTINGS)
        prefs_module._check_settings_file()
        good = settings_file.with_name("settings.last-good.ini")
        assert good.read_bytes() == SOUND_SETTINGS
        if os.name == "posix":
            # It holds the same cookies and keys as the settings.
            assert good.stat().st_mode & 0o077 == 0

    @pytest.mark.parametrize(
        "body",
        [b"", b"\x00" * 300, SOUND_SETTINGS[:20] + b"\x00" * 40],
        ids=["empty", "all zeros", "zeros after the start"],
    )
    def test_a_damaged_settings_file_gets_the_last_good_copy_back(self, settings_file, caplog, body):
        """What a power cut soon after a save leaves. wx read no settings
        from it, and the next save wrote the defaults over it."""
        settings_file.with_name("settings.last-good.ini").write_bytes(SOUND_SETTINGS)
        settings_file.write_bytes(body)
        prefs_module._check_settings_file()
        assert settings_file.read_bytes() == SOUND_SETTINGS
        [kept] = list(settings_file.parent.glob("settings.unreadable-*.ini"))
        assert kept.read_bytes() == body
        [line] = warnings_from(caplog, "ficary.prefs")
        assert kept.name in line and "put back" in line

    def test_with_no_good_copy_it_starts_from_the_defaults_and_says_so(self, settings_file, caplog):
        settings_file.write_bytes(b"\x00" * 300)
        prefs_module._check_settings_file()
        assert not settings_file.exists()
        [kept] = list(settings_file.parent.glob("settings.unreadable-*.ini"))
        [line] = warnings_from(caplog, "ficary.prefs")
        assert kept.name in line and "default settings" in line

    def test_a_damaged_last_good_copy_is_not_put_back(self, settings_file, caplog):
        settings_file.with_name("settings.last-good.ini").write_bytes(b"\x00" * 300)
        settings_file.write_bytes(b"")
        prefs_module._check_settings_file()
        assert not settings_file.exists()
        [line] = warnings_from(caplog, "ficary.prefs")
        assert "default settings" in line

    @pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
    def test_a_file_saved_by_hand_as_utf16_is_left_alone(self, settings_file, caplog, encoding):
        """Notepad's "Unicode" is UTF-16: zero bytes everywhere, but sound,
        and wx reads it by its byte-order mark."""
        body = "library_path=/home/someone/Stories\n".encode(encoding)
        if not body.startswith((b"\xff\xfe", b"\xfe\xff")):
            body = b"\xfe\xff" + body
        settings_file.write_bytes(body)
        prefs_module._check_settings_file()
        assert settings_file.read_bytes() == body
        assert list(settings_file.parent.glob("settings.unreadable-*.ini")) == []
        assert warnings_from(caplog, "ficary.prefs") == []

    def test_it_is_checked_once_a_run(self, settings_file):
        """Every window makes its own Prefs."""
        settings_file.write_bytes(SOUND_SETTINGS)
        prefs_module._check_settings_file()
        settings_file.write_bytes(b"library_path=/elsewhere\n")
        prefs_module._check_settings_file()
        assert settings_file.with_name("settings.last-good.ini").read_bytes() == SOUND_SETTINGS

    def test_the_settings_are_checked_before_they_are_read(self, settings_file, monkeypatch):
        events = []
        real_settings_file = portable.settings_file
        monkeypatch.setattr(prefs_module, "_check_settings_file", lambda: events.append("check"))

        def settings_file_read():
            events.append("read")
            return real_settings_file()

        monkeypatch.setattr(portable, "settings_file", settings_file_read)
        prefs_module.Prefs()
        assert events and events[0] == "check"


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds real windows (run under xvfb-run -a)")
def test_damaged_settings_found_while_the_window_opens_are_in_the_status_pane(wx_app, settings_file):
    from ficary import gui

    settings_file.write_bytes(b"\x00" * 300)
    frame = gui.MainFrame()
    try:
        for _ in range(20):
            wx.SafeYield()
        with frame._log_lock:
            text = frame.log_ctrl.GetValue() + "".join(frame._log_queue)
        assert "settings file was damaged" in text
    finally:
        frame._clip_timer.Stop()
        frame.Destroy()
        wx.SafeYield()
