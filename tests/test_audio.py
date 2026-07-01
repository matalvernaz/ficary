"""Tests for the shared audio engine + live-TTS sequencing.

The OpenAL backend needs a real device and isn't exercised here; these tests
drive the backend-agnostic engine logic with a fake backend, and the live-TTS
controller with a fake engine + fake synth.
"""
from ficary.audio.engine import (
    CHANNEL_VOICE,
    AudioEngine,
    ramp_values,
)
from ficary.audio.events import Event, ReaderEvent
from ficary.reader.live_tts import LiveTTSController


class FakeBackend:
    available = True

    def __init__(self):
        self.gains = {}
        self.played = []
        self._next = 1
        self._playing = set()

    def load(self, path, *, looping):
        h = self._next
        self._next += 1
        return h

    def play(self, h):
        self.played.append(h)
        self._playing.add(h)

    def pause(self, h):
        self._playing.discard(h)

    def stop(self, h):
        self._playing.discard(h)

    def set_gain(self, h, gain):
        self.gains[h] = gain

    def set_position(self, h, az, el, dist):
        pass

    def is_playing(self, h):
        return h in self._playing

    def set_reverb(self, size):
        pass

    def shutdown(self):
        pass


def test_ramp_values_ends_exactly():
    vals = ramp_values(0.0, 1.0, 4)
    assert vals == [0.25, 0.5, 0.75, 1.0]
    assert ramp_values(1.0, 0.0, 2) == [0.5, 0.0]


def test_event_bus_fanout_and_isolation():
    engine = AudioEngine(backend=FakeBackend())
    seen_a, seen_b = [], []
    engine.subscribe(lambda e: seen_a.append(e.kind))

    def bad(e):
        raise RuntimeError("boom")

    engine.subscribe(bad)  # must not break others
    engine.subscribe(lambda e: seen_b.append(e.kind))
    engine.emit(Event(ReaderEvent.TTS_STARTED))
    assert seen_a == [ReaderEvent.TTS_STARTED]
    assert seen_b == [ReaderEvent.TTS_STARTED]


def test_play_file_sets_channel_gain_and_plays():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    engine.set_gain(CHANNEL_VOICE, 0.8)
    h = engine.play_file("x.mp3", CHANNEL_VOICE)
    assert h in be.played
    assert be.gains[h] == 0.8


def test_duck_immediate_lowers_channel_gain():
    be = FakeBackend()
    engine = AudioEngine(backend=be)
    h = engine.add_looping_source("amb.ogg", gain=1.0)
    engine.duck("ambient", to=0.25, ms=0)
    assert be.gains[h] == 0.25
    engine.set_gain("ambient", 1.0)
    assert be.gains[h] == 1.0


class FakeEngine:
    """Minimal engine surface LiveTTSController uses; on_done fires at once."""

    def __init__(self):
        self.events = []
        self.played = []

    def play_file(self, path, channel, on_done=None):
        self.played.append(path)
        if on_done:
            on_done()

    def pause(self, channel):
        pass

    def resume(self, channel):
        pass

    def stop(self, channel):
        pass

    def emit(self, event):
        self.events.append(event.kind)


def test_live_tts_walks_all_chunks(tmp_path):
    engine = FakeEngine()
    highlights = []

    def fake_synth(voice, text, out):
        out.write_bytes(b"\x00")

    ctrl = LiveTTSController(
        engine, voice="edge:test", on_highlight=lambda c: highlights.append(c.index),
        synth=fake_synth, tmp_dir=tmp_path,
    )
    ctrl.start("First sentence here.\n\nSecond paragraph now.", chapter_number=3)
    ctrl._worker.join(timeout=5.0)

    assert ReaderEvent.TTS_STARTED in engine.events
    assert ReaderEvent.TTS_STOPPED in engine.events
    assert highlights == [0, 1]  # two paragraphs → two chunks, in order
    assert len(engine.played) == 2
