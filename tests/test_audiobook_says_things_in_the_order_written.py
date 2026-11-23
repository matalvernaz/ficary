"""An audiobook speaks a chapter in the order it was written.

Before synthesis a chapter's pieces of speech are tidied by ``_merge_small_segments`` to keep the number of requests down. One of its steps took any short piece of narration, such as "Tom asked" or "she said", and added it to the end of the nearest earlier piece of narration, however many spoken lines stood in between. In a back-and-forth exchange the narrator then read "Tom asked. said Margaret." before either line had been spoken.
"""

from __future__ import annotations

import re

import pytest

from ficary import tts

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


def words(text: str) -> list[str]:
    """The words of a text in order, with punctuation and spacing left out."""
    return re.findall(r"[A-Za-z0-9']+", text)


def spoken(segments) -> list[str]:
    """Every word the audiobook will say, in the order it will say them."""
    return words(" ".join(segment.text for segment in segments if not segment.scene_break))


def as_spoken(html: str):
    text = tts._html_to_audiobook_text(html)
    return text, tts._merge_small_segments(tts._segment_chapter_text(text))


EXCHANGE = (
    "<p>The shop was dark apart from one lamp over the counter, and the door stood a hand's width open.</p>"
    "<p>“Is it open?” Tom asked.</p>"
    "<p>“No,” said Margaret.</p>"
    "<p>“Then why is the light on?”</p>"
    "<p>“I do not know,” she said.</p>"
    "<p>They stood on the step a while longer, and neither of them reached for the handle.</p>"
)


class TestAnExchange:
    def test_who_said_it_is_spoken_after_the_line_as_written(self):
        _text, segments = as_spoken(EXCHANGE)
        said = [segment.text for segment in segments]
        line = next(index for index, text in enumerate(said) if "Is it open" in text)
        tag = next(index for index, text in enumerate(said) if "Tom asked" in text)
        reply = next(index for index, text in enumerate(said) if text.startswith("No"))
        assert line < tag < reply

    def test_nothing_about_the_speakers_is_added_to_the_scene_setting(self):
        _text, segments = as_spoken(EXCHANGE)
        opening = segments[0].text
        assert opening.startswith("The shop was dark")
        assert "Tom asked" not in opening
        assert "said Margaret" not in opening

    def test_the_whole_exchange_is_spoken_in_the_order_written(self):
        text, segments = as_spoken(EXCHANGE)
        assert spoken(segments) == words(text)


SCENE_CHANGE = (
    "<p>She locked the door behind her and did not look back at the house again.</p>"
    "<hr/>"
    "<p>Two days later.</p>"
    "<p>\u201cAre you awake?\u201d Tom asked from the doorway.</p>"
    "<p>\u201cI am now,\u201d she said.</p>"
)


class TestTheStartOfAScene:
    """With scene dividers played as pauses, short narration at the start of a scene was not moved but lost: nothing earlier in the scene was there to take it."""

    def as_spoken_with_pauses(self):
        text = tts._html_to_audiobook_text(SCENE_CHANGE, hr_as_stars=True)
        return text, tts._merge_small_segments(tts._segment_chapter_text(text))

    def test_the_line_that_opens_the_scene_is_spoken(self):
        _text, segments = self.as_spoken_with_pauses()
        pause = next(index for index, segment in enumerate(segments) if segment.scene_break)
        assert segments[pause + 1].text == "Two days later."
        assert segments[pause + 1].speaker is None

    def test_nothing_in_the_scene_is_left_out(self):
        text, segments = self.as_spoken_with_pauses()
        assert spoken(segments) == words(text)
        said = " ".join(segment.text for segment in segments)
        assert "Tom asked from the doorway" in said
        assert "she said" in said

    def test_the_pause_between_the_scenes_is_still_there(self):
        _text, segments = self.as_spoken_with_pauses()
        assert [segment.scene_break for segment in segments].count(True) == 1


PASSAGES = {
    "a tag between two lines from the same person": (
        "<p>“Hello,” she said. “How are you keeping these days?”</p>"
    ),
    "tags after every line with no narration between": (
        "<p>“One loaf,” said Tom.</p><p>“Two,” said Ann.</p><p>“Three?”</p><p>“Four,” he said.</p>"
    ),
    "a short action between two lines": (
        "<p>“Give it here,” said Tom.</p><p>She did not.</p><p>“Please,” he said.</p><p>“No,” said Ann, and she walked out into the rain without looking back at him.</p>"
    ),
    "a tag before its line": (
        "<p>Tom said, “Is it open?”</p><p>Ann said, “No.”</p><p>“Oh,” said Tom.</p>"
    ),
    "narration, a scene divider, and more dialogue": (
        "<p>The rain had stopped an hour before, and the stones of the street were still dark with it.</p>"
        "<hr/>"
        "<p>“Did you get it?” he asked.</p><p>“Yes,” she said.</p>"
    ),
}


class TestOtherShapes:
    @pytest.mark.parametrize("name", sorted(PASSAGES))
    def test_the_words_come_out_in_the_order_they_went_in(self, name):
        text, segments = as_spoken(PASSAGES[name])
        assert spoken(segments) == words(text)

    def test_a_short_piece_of_narration_between_two_speakers_stays_where_it_is(self):
        segments = tts._merge_small_segments([
            tts.Segment("Hello there, how are you today?", speaker="Ann"),
            tts.Segment("she said."),
            tts.Segment("Fine.", speaker="Tom"),
        ])
        assert [(segment.speaker, segment.text) for segment in segments] == [
            ("Ann", "Hello there, how are you today?"),
            (None, "she said."),
            ("Tom", "Fine."),
        ]

    def test_a_short_piece_is_not_moved_back_past_an_earlier_line(self):
        segments = tts._merge_small_segments([
            tts.Segment("The room was empty and the fire had gone out long before they came in."),
            tts.Segment("Is anyone here?", speaker="Tom"),
            tts.Segment("Tom called."),
        ])
        assert [segment.text for segment in segments] == [
            "The room was empty and the fire had gone out long before they came in.",
            "Is anyone here?",
            "Tom called.",
        ]


class TestWhatTheTidyingStillDoes:
    """The rest of the tidying is unchanged: fewer requests where that costs nothing."""

    def test_narration_that_is_side_by_side_is_still_joined(self):
        segments = tts._merge_small_segments([
            tts.Segment("He stopped."),
            tts.Segment("She did not."),
            tts.Segment("Wait!", speaker="Tom"),
        ])
        assert [(segment.speaker, segment.text) for segment in segments] == [
            (None, "He stopped. She did not."),
            ("Tom", "Wait!"),
        ]

    def test_a_full_stop_or_comma_is_joined_without_a_space_before_it(self):
        """The narration after a quote often arrives as its words and then its punctuation as a separate piece."""
        segments = tts._merge_small_segments([
            tts.Segment("Is it open?", speaker="Tom"),
            tts.Segment("Tom asked"),
            tts.Segment(", leaning on the wall beside the window"),
            tts.Segment("."),
            tts.Segment("No.", speaker="Ann"),
        ])
        assert [segment.text for segment in segments] == [
            "Is it open?",
            "Tom asked, leaning on the wall beside the window.",
            "No.",
        ]

    def test_words_are_still_joined_with_a_space(self):
        segments = tts._merge_small_segments([tts.Segment("He stopped."), tts.Segment("She did not.")])
        assert [segment.text for segment in segments] == ["He stopped. She did not."]

    def test_a_piece_that_is_only_punctuation_is_still_left_out(self):
        segments = tts._merge_small_segments([
            tts.Segment("Are you there?", speaker="Tom"),
            tts.Segment("."),
            tts.Segment("...", speaker="Ann"),
            tts.Segment("Then I will wait.", speaker="Tom"),
        ])
        assert [segment.text for segment in segments] == ["Are you there?", "Then I will wait."]

    def test_scene_dividers_are_kept_and_nothing_is_joined_across_one(self):
        segments = tts._merge_small_segments([
            tts.Segment("The first scene ends here."),
            tts.Segment("", scene_break=True),
            tts.Segment("The second begins."),
        ])
        assert [(segment.scene_break, segment.text) for segment in segments] == [
            (False, "The first scene ends here."),
            (True, ""),
            (False, "The second begins."),
        ]

    def test_an_oversized_piece_is_still_split_and_keeps_its_order(self):
        sentence = "The road went on and on over the hill and down the other side. "
        long_text = sentence * 80
        assert len(long_text) > tts._MAX_SEGMENT_CHARS
        segments = tts._merge_small_segments([tts.Segment(long_text)])
        assert len(segments) > 1
        assert all(len(segment.text) <= tts._MAX_SEGMENT_CHARS for segment in segments)
        assert spoken(segments) == words(long_text)

    def test_joined_narration_never_goes_over_the_size_one_request_can_take(self):
        piece = "A sentence of narration that is long enough to count. " * 10
        segments = tts._merge_small_segments([tts.Segment(piece) for _ in range(10)])
        assert all(len(segment.text) <= tts._MAX_SEGMENT_CHARS for segment in segments)
        assert spoken(segments) == words(piece * 10)


# One piece of speech: who says it (nobody means the narrator), and text that is short, long, or only punctuation.
a_piece = st.builds(
    lambda speaker, text: (speaker, text),
    st.sampled_from([None, None, "Ann", "Tom"]),
    st.sampled_from([
        "she said.", "Tom asked", "he said,", "No.", "Yes!", "Is it open?", ".", "...", "—",
        "The street was empty when they came out, and the rain had stopped an hour before.",
        "I have been standing out here in the cold for the better part of an hour, you know.",
    ]),
)


class TestAnyChapter:
    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.one_of(a_piece, st.just("scene break")), max_size=25))
    def test_tidying_never_changes_the_order_of_the_words(self, pieces):
        segments = [
            tts.Segment("", scene_break=True) if piece == "scene break" else tts.Segment(piece[1], speaker=piece[0])
            for piece in pieces
        ]
        written = words(" ".join(segment.text for segment in segments))
        assert spoken(tts._merge_small_segments(segments)) == written

    @settings(max_examples=300, deadline=None)
    @given(st.lists(a_piece, max_size=25))
    def test_nobody_is_given_words_they_did_not_say(self, pieces):
        """Tidying may join narration to narration. It must never move words from one voice to another."""
        segments = [tts.Segment(text, speaker=speaker) for speaker, text in pieces]
        before: dict = {}
        for segment in segments:
            before.setdefault(segment.speaker, []).extend(words(segment.text))
        after: dict = {}
        for segment in tts._merge_small_segments(segments):
            after.setdefault(segment.speaker, []).extend(words(segment.text))
        assert {speaker: said for speaker, said in after.items() if said} == {speaker: said for speaker, said in before.items() if said}
