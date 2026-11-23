"""An audiobook render stops, and says why, when its voices stop producing audio.

With the Edge voice service refusing every request (it answers a client it has stopped accepting with a 403), a render used to try every piece of every chapter three times, reporting progress as if all were well, and fail at the very end with "No chapter audio was generated" and no reason. On a full-length book that took the better part of an hour or more.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from ficary import tts
from ficary.models import Chapter, Story
from ficary.tts_providers import explain_synthesis_error


class WSServerHandshakeError(Exception):
    """Named as aiohttp names it. The code under test goes by the name, so the test needs no aiohttp."""


def refused():
    return WSServerHandshakeError("403, message='Invalid response status', url='wss://speech.example.invalid/synthesize'")


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


# ── The tally on its own ─────────────────────────────────────────


class TestTheTally:
    def test_seven_failures_in_a_row_are_not_enough(self):
        health = tts._VoiceHealth()
        for _ in range(7):
            health.piece_failed(refused())
        assert not health.gave_up
        assert health.failures_in_a_row == 7

    def test_the_eighth_refusal_ends_it_at_once(self):
        health = tts._VoiceHealth()
        for _ in range(8):
            health.piece_failed(refused())
        assert health.gave_up

    def test_other_failures_must_also_have_gone_on_for_a_while(self):
        """A connection that drops for a few seconds must not end a render with hours behind it."""
        clock = Clock()
        health = tts._VoiceHealth(clock=clock)
        for _ in range(30):
            clock.now += 0.5
            health.piece_failed(ConnectionError("network is unreachable"))
        assert not health.gave_up
        clock.now += 6
        health.piece_failed(ConnectionError("network is unreachable"))
        assert health.gave_up

    def test_the_wait_is_measured_from_the_first_failure_of_the_run(self):
        clock = Clock()
        health = tts._VoiceHealth(clock=clock)
        health.piece_failed(ConnectionError("first"))
        clock.now += tts._VOICE_OUTAGE_MIN_SECONDS - 1
        for _ in range(7):
            health.piece_failed(ConnectionError("still down"))
        assert not health.gave_up
        clock.now += 1
        health.piece_failed(ConnectionError("still down"))
        assert health.gave_up

    def test_one_piece_that_works_starts_the_count_again(self):
        clock = Clock()
        health = tts._VoiceHealth(clock=clock)
        for _ in range(7):
            health.piece_failed(refused())
        health.piece_succeeded()
        assert health.failures_in_a_row == 0
        clock.now += 100
        for _ in range(7):
            health.piece_failed(ConnectionError("down"))
        # Seven again, and the hundred seconds before the success do not count towards the wait.
        assert not health.gave_up

    def test_the_last_error_is_kept_for_the_explanation(self):
        health = tts._VoiceHealth()
        health.piece_failed(ConnectionError("first"))
        last = refused()
        health.piece_failed(last)
        assert health.last_error is last

    @pytest.mark.parametrize(
        "error, expected",
        [
            (WSServerHandshakeError("403, message='Invalid response status'"), True),
            (WSServerHandshakeError("401, message='Unauthorized'"), True),
            (WSServerHandshakeError("503, message='Service Unavailable'"), False),
            (ConnectionError("403 forbidden by a proxy"), False),
            (RuntimeError("No audio was received"), False),
        ],
    )
    def test_only_the_service_turning_the_connection_away_counts_as_a_refusal(self, error, expected):
        assert tts._voice_refused(error) is expected


# ── Through the real chapter code ────────────────────────────────


def pieces(count: int):
    """Alternating narration and speech, so nothing merges into anything else."""
    made = []
    for number in range(count):
        if number % 2:
            made.append(tts.Segment(f"This is line number {number} and it is said out loud by somebody.", speaker="Margaret"))
        else:
            made.append(tts.Segment(f"Narration number {number} goes on for long enough not to be merged away."))
    return made


@pytest.fixture
def quick(monkeypatch):
    """No waiting between attempts, and no ffmpeg for the silence between speakers."""
    monkeypatch.setattr(tts, "_TTS_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(tts, "_make_silence_clip", lambda *args, **kwargs: None)


class Synth:
    """Stands in for the call that turns one piece into audio, and counts how often it is asked."""

    def __init__(self, behave):
        self.calls = 0
        self.behave = behave

    async def __call__(self, segment, voice, output_path, speech_rate=0):
        self.calls += 1
        return self.behave(segment, voice, output_path)


def always_refused(segment, voice, output_path):
    raise refused()


class TestAChapter:
    def test_a_refusing_service_is_given_up_on_long_before_every_piece_is_tried(self, quick, monkeypatch, tmp_path):
        synth = Synth(always_refused)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        health = tts._VoiceHealth()
        result = asyncio.run(tts.generate_chapter_audio(pieces(60), {}, tmp_path / "chapter.mp3", chapter_num=1, voice_health=health))
        assert health.gave_up
        assert not result
        # 60 pieces at three attempts each is 180. Eight failed pieces, plus the few already under way when the eighth failed, is far fewer.
        assert synth.calls <= 3 * (tts._VOICE_OUTAGE_MIN_FAILURES + tts._TTS_CONCURRENCY)

    def test_without_a_tally_every_piece_is_still_tried(self, quick, monkeypatch, tmp_path):
        """Voice previews and other callers pass no tally and behave as before."""
        synth = Synth(always_refused)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        asyncio.run(tts.generate_chapter_audio(pieces(12), {}, tmp_path / "chapter.mp3", chapter_num=1))
        assert synth.calls == 12 * 3

    def test_pieces_with_nothing_to_say_are_no_sign_of_trouble(self, quick, monkeypatch, tmp_path):
        """A piece that is punctuation alone comes back empty without an error. Forty of those must not end a render."""
        synth = Synth(lambda segment, voice, output_path: False)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        monkeypatch.setattr(tts, "_VOICE_OUTAGE_MIN_SECONDS", 0)
        health = tts._VoiceHealth()
        asyncio.run(tts.generate_chapter_audio(pieces(40), {}, tmp_path / "chapter.mp3", chapter_num=1, voice_health=health))
        assert not health.gave_up
        assert health.failures_in_a_row == 0

    def test_an_outage_that_is_not_a_refusal_is_given_time(self, quick, monkeypatch, tmp_path):
        def unreachable(segment, voice, output_path):
            raise ConnectionError("network is unreachable")

        synth = Synth(unreachable)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        health = tts._VoiceHealth()
        asyncio.run(tts.generate_chapter_audio(pieces(20), {}, tmp_path / "chapter.mp3", chapter_num=1, voice_health=health))
        # All twenty failed inside a second, which is well short of the wait.
        assert health.failures_in_a_row == 20
        assert not health.gave_up
        assert synth.calls == 20 * 3

    def test_a_piece_that_works_in_between_keeps_the_render_going(self, quick, monkeypatch, tmp_path):
        """Every third piece is refused, one at a time so the order is certain. The ones that work in between show the voices are fine, however many fail in all."""
        monkeypatch.setattr(tts, "_TTS_CONCURRENCY", 1)
        refused_numbers = set(range(0, 60, 3))

        def every_third_is_refused(segment, voice, output_path):
            number = int(segment.text.split("number ")[1].split(" ")[0])
            if number in refused_numbers:
                raise refused()
            output_path.write_bytes(b"audio")
            return True

        synth = Synth(every_third_is_refused)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        # Joining the pieces into one chapter file is ffmpeg's job and not what this test is about.
        monkeypatch.setattr(tts.subprocess, "run", lambda *args, **kwargs: type("Done", (), {"returncode": 0, "stderr": b"", "stdout": b""})())
        health = tts._VoiceHealth()
        asyncio.run(tts.generate_chapter_audio(pieces(60), {}, tmp_path / "chapter.mp3", chapter_num=1, voice_health=health))
        assert not health.gave_up
        # Twenty pieces failed in all, but never two in a row.
        assert health.failures_in_a_row <= 1
        assert synth.calls == 40 + 20 * 3

    def test_the_chapter_is_not_described_as_unsynthesised_when_the_render_has_given_up(self, quick, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(tts, "_generate_segment_audio", Synth(always_refused))
        with caplog.at_level(logging.WARNING, logger="ficary.tts"):
            asyncio.run(tts.generate_chapter_audio(pieces(60), {}, tmp_path / "chapter.mp3", chapter_num=1, voice_health=tts._VoiceHealth()))
        assert "could not be synthesised" not in caplog.text


# ── Through the whole render ─────────────────────────────────────


def story_of(chapters: int) -> Story:
    def chapter_text(number: int) -> str:
        # Each chapter has to read differently: finished chapter audio is kept under a name made from its text, so identical chapters would all be served from the first one's.
        paragraph = (
            f"<p>On day {number} the bakery smelled of burnt sugar when Margaret pushed the door open, and the bell over it rang twice.</p>"
            "<p>\u201cWe are closed for the day, and there is nothing left on the shelves,\u201d said the woman behind the counter.</p>"
            "<p>\u201cThe sign in your window says that you are open until six,\u201d Margaret said.</p>"
        )
        return paragraph * 8

    return Story(
        id=990002,
        title="The Loaf",
        author="Nobody In Particular",
        summary="Two people buy bread.",
        url="https://example.invalid/story/990002",
        chapters=[Chapter(number=number, title=f"Part {number}", html=chapter_text(number)) for number in range(1, chapters + 1)],
    )


@pytest.fixture
def render(quick, monkeypatch, tmp_path):
    """Everything a render needs that is not the voices: no ffmpeg check, and its caches in a scratch folder."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(tts, "_check_ffmpeg", lambda *args, **kwargs: None)
    monkeypatch.setattr(tts, "_chapter_cache_root", lambda story_id: cache / "chapter_audio" / str(story_id))
    monkeypatch.setattr(tts, "_attr_cache_root", lambda: cache / "attribution")

    def run(story, **options):
        return tts.generate_audiobook(story, str(tmp_path / "out"), attribution_backend="builtin", **options)

    run.cache = cache
    run.out = tmp_path / "out"
    return run


class TestTheWholeRender:
    def test_it_stops_in_the_first_chapter_and_says_why(self, render, monkeypatch):
        synth = Synth(always_refused)
        monkeypatch.setattr(tts, "_generate_segment_audio", synth)
        progress = []
        with pytest.raises(tts.AudiobookVoiceUnavailable) as stopped:
            render(story_of(5), progress_callback=lambda current, total, title: progress.append(current))
        message = str(stopped.value)
        assert "chapter 1 of 5" in message
        assert "refused the request (403)" in message
        assert "already made" not in message
        # Nothing was reported as done, and chapters two to five were never started.
        assert progress == []
        assert synth.calls <= 3 * (tts._VOICE_OUTAGE_MIN_FAILURES + tts._TTS_CONCURRENCY)

    def test_it_is_a_runtime_error_so_every_front_end_already_shows_it(self):
        assert issubclass(tts.AudiobookVoiceUnavailable, RuntimeError)
        assert not issubclass(tts.AudiobookVoiceUnavailable, tts.AudiobookCancelled)

    def test_nothing_half_made_is_left_in_the_cache_or_the_output_folder(self, render, monkeypatch):
        monkeypatch.setattr(tts, "_generate_segment_audio", Synth(always_refused))
        with pytest.raises(tts.AudiobookVoiceUnavailable):
            render(story_of(3))
        assert [path.name for path in render.cache.rglob("*.mp3")] == []
        assert [path.name for path in render.out.rglob("*.m4b")] == []
        assert [path.name for path in render.out.iterdir() if path.name.startswith("ffn-audiobook-")] == []

    def test_a_connection_that_stays_down_stops_it_too(self, render, monkeypatch):
        def unreachable(segment, voice, output_path):
            raise ConnectionError("network is unreachable")

        monkeypatch.setattr(tts, "_generate_segment_audio", Synth(unreachable))
        monkeypatch.setattr(tts, "_VOICE_OUTAGE_MIN_SECONDS", 0)
        with pytest.raises(tts.AudiobookVoiceUnavailable) as stopped:
            render(story_of(3))
        message = str(stopped.value)
        assert "could not be reached" in message
        assert "then make the audiobook again" in message
        assert "Play" not in message

    def test_chapters_already_made_are_mentioned_as_kept(self, render, monkeypatch):
        """The service goes away after the first chapter. That chapter is in the cache, and the message says the next try carries on from it."""
        monkeypatch.setattr(tts, "_generate_segment_audio", Synth(always_refused))
        real_chapter = tts.generate_chapter_audio

        async def first_chapter_works(segments, voice_mapper, output_path, chapter_num=0, **options):
            if chapter_num == 1:
                output_path.write_bytes(b"chapter one audio")
                return tts.ChapterAudioResult(True, 3, 3)
            return await real_chapter(segments, voice_mapper, output_path, chapter_num=chapter_num, **options)

        monkeypatch.setattr(tts, "generate_chapter_audio", first_chapter_works)
        with pytest.raises(tts.AudiobookVoiceUnavailable) as stopped:
            render(story_of(4))
        message = str(stopped.value)
        assert "chapter 2 of 4" in message
        assert "The 1 chapter already made is kept" in message
        assert len(list(render.cache.rglob("*.mp3"))) == 1


class TestTheExplanation:
    def test_the_reader_still_hears_about_play(self):
        assert explain_synthesis_error(ConnectionError("down")).endswith("then press Play again.")

    def test_a_render_gets_its_own_advice(self):
        text = explain_synthesis_error(ConnectionError("down"), retry_hint="then make the audiobook again")
        assert text == "The voice service could not be reached. Check the internet connection, then make the audiobook again."
