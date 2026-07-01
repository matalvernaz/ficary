"""Tests for the soundscape subsystem (model, library, session, assignment)."""
from pathlib import Path

from ficary.audio.events import Event, ReaderEvent
from ficary.reader.state import ReaderStateDB
from ficary.soundscape import library
from ficary.soundscape.model import Sound, Soundscape
from ficary.soundscape.session import SoundscapeSession


class TestModel:
    def test_round_trip(self):
        sc = Soundscape("Rainy Library",
                        [Sound("rain.ogg", 0.6, positional=False),
                         Sound("fire.ogg", 0.4, positional=True, azimuth=210, distance=1.5)],
                        reverb_room_size=0.35, master_volume=0.8)
        back = Soundscape.from_dict(sc.to_dict())
        assert back.name == "Rainy Library"
        assert len(back.sounds) == 2
        assert back.sounds[1].positional and back.sounds[1].azimuth == 210
        assert back.reverb_room_size == 0.35

    def test_clamps_and_skips_bad_sounds(self):
        sc = Soundscape.from_dict({
            "name": "X", "master_volume": 5, "reverb_room_size": -1,
            "sounds": [{"volume": 0.5}, {"source": "ok.ogg", "volume": 9}],
        })
        assert sc.master_volume == 1.0 and sc.reverb_room_size == 0.0
        assert len(sc.sounds) == 1  # the source-less entry is dropped
        assert sc.sounds[0].volume == 1.0


class TestLibrary:
    def test_save_load_list_delete(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ficary.portable.soundscapes_dir", lambda: tmp_path)
        slug = library.save(Soundscape("Storm At Sea", [Sound("waves.ogg")]))
        assert slug == "storm-at-sea"
        assert library.list_slugs() == ["storm-at-sea"]
        loaded = library.load(slug)
        assert loaded and loaded.name == "Storm At Sea"
        library.delete(slug)
        assert library.list_slugs() == []

    def test_resolve_user_sound(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ficary.portable.sounds_dir", lambda: tmp_path)
        (tmp_path / "rain.ogg").write_bytes(b"\x00")
        assert library.resolve_source("rain.ogg") == tmp_path / "rain.ogg"
        assert library.resolve_source("missing.ogg") is None


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.subscribed = None

    def subscribe(self, cb):
        self.subscribed = cb

    def unsubscribe(self, cb):
        self.subscribed = None

    def add_looping_source(self, path, **kw):
        self.calls.append(("add", path))

    def set_reverb_room_size(self, size):
        self.calls.append(("reverb", size))

    def fade_in(self, ch, ms, to=1.0):
        self.calls.append(("fade_in", ch, to))

    def fade_out(self, ch, ms, then_stop=True):
        self.calls.append(("fade_out", ch))

    def duck(self, ch, to, ms):
        self.calls.append(("duck", ch, to))

    def restore(self, ch, ms):
        self.calls.append(("restore", ch))

    def stop(self, ch):
        self.calls.append(("stop", ch))

    def _kinds(self):
        return [c[0] for c in self.calls]


class TestSession:
    def _sc(self):
        return Soundscape("Rain", [Sound("rain.ogg", 0.5)], master_volume=0.8)

    def test_open_builds_and_fades_in(self, monkeypatch):
        monkeypatch.setattr("ficary.soundscape.library.resolve_source", lambda s: Path("/x"))
        eng = FakeEngine()
        sess = SoundscapeSession(eng, self._sc())
        assert eng.subscribed is not None  # session subscribed to the bus
        sess._on_event(Event(ReaderEvent.READER_OPENED))
        assert "add" in eng._kinds() and "fade_in" in eng._kinds()

    def test_ducks_under_tts_then_restores(self, monkeypatch):
        monkeypatch.setattr("ficary.soundscape.library.resolve_source", lambda s: Path("/x"))
        eng = FakeEngine()
        sess = SoundscapeSession(eng, self._sc())
        sess._on_event(Event(ReaderEvent.READER_OPENED))
        sess._on_event(Event(ReaderEvent.TTS_STARTED))
        sess._on_event(Event(ReaderEvent.TTS_STOPPED))
        assert "duck" in eng._kinds() and "restore" in eng._kinds()

    def test_close_fades_out(self, monkeypatch):
        monkeypatch.setattr("ficary.soundscape.library.resolve_source", lambda s: Path("/x"))
        eng = FakeEngine()
        sess = SoundscapeSession(eng, self._sc())
        sess._on_event(Event(ReaderEvent.READER_OPENED))
        sess._on_event(Event(ReaderEvent.READER_CLOSED))
        assert "fade_out" in eng._kinds()

    def test_missing_sounds_no_start(self, monkeypatch):
        monkeypatch.setattr("ficary.soundscape.library.resolve_source", lambda s: None)
        eng = FakeEngine()
        sess = SoundscapeSession(eng, self._sc())
        sess._on_event(Event(ReaderEvent.READER_OPENED))
        assert "fade_in" not in eng._kinds()

    def test_no_soundscape_is_inert(self):
        eng = FakeEngine()
        sess = SoundscapeSession(eng, None)
        sess._on_event(Event(ReaderEvent.READER_OPENED))
        assert eng.calls == []


class TestAssignment:
    def test_set_get_clear(self, tmp_path):
        db = ReaderStateDB(tmp_path / "r.db")
        assert db.get_soundscape("k") is None
        db.set_soundscape("k", "storm-at-sea")
        assert db.get_soundscape("k") == "storm-at-sea"
        db.set_soundscape("k", None)
        assert db.get_soundscape("k") is None
        db.close()
