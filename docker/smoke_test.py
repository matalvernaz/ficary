"""Runs BookNLP and fastcoref inside the Docker image on a short original scene and checks the
speakers they set, so an image whose speech attribution doesn't really work is never published.

"Didn't fall back" isn't enough: until 2.20.9 BookNLP ran without falling back and changed
nothing, because its output was read from the wrong columns. So this checks real results: the
lines BookNLP should correct, that neither backend gives narration a voice, and that every name
either one sets belongs to the scene. The scene is tests/fixtures/booknlp/passage.txt, which the
unit tests replay BookNLP's recorded output for.

Run it against an image (it downloads about 160 MB of BookNLP and 360 MB of fastcoref models):
    docker run --rm -i ghcr.io/matalvernaz/ficary python - < docker/smoke_test.py
"""
import sys

from ficary import attribution, tts

SCENE = (
    "The bakery on Halloway Street smelled of burnt sugar when Margaret Ashby-Lowe pushed the door open. "
    "Her brother Tom followed her in, shaking rain from his coat. "
    "A card in the window still read \"OPEN\" in faded red letters.\n\n"
    "“We’re closed,” said the woman behind the counter, without looking up from her dough.\n\n"
    "“The sign says you’re open,” Margaret said.\n\n"
    "The woman sighed and set down her rolling pin. “Then the sign is wrong. Come back tomorrow.”\n\n"
    "“We only need one loaf,” Tom said. “It’s for our mother’s birthday.”\n\n"
    "The woman looked at him for a long moment. “One loaf. And you didn’t get it from me.”\n\n"
    "Margaret laughed. “Thank you.”\n\n"
    "Tom held the door for his sister on the way out. “You could have smiled at her,” he said.\n\n"
    "Outside, Tom waited until their cousin Sam was watching, then threw him the loaf. “Catch,” he said.\n\n"
    "Sam caught it against his chest. “Next time, warn me first.”\n"
)

# Every name a backend may set: the scene's characters as the text names them.
SCENE_NAMES = {"Margaret Ashby-Lowe", "Margaret", "Tom", "Sam"}

# Lines the built-in parser gets wrong and BookNLP's output gets right.
BOOKNLP_EXPECTED = {
	"It’s for our mother’s birthday.": "Tom",
	"Thank you.": "Margaret Ashby-Lowe",
	"Catch,": "Tom",
}

BACKENDS = (
	("booknlp", "small"),
	("fastcoref", None),
)

problems = []
for backend, size in BACKENDS:
	before = tts.parse_segments(SCENE)
	# refine_speakers never raises; a failure shows up as has_failed() and a logged warning.
	after = attribution.refine_speakers(tts.parse_segments(SCENE), SCENE, backend=backend, model_size=size)
	if attribution.has_failed(backend, size):
		problems.append(f"{backend} fell back to the built-in attribution; the warning logged above says why")
		continue

	for old, new in zip(before, after):
		if not attribution._is_dialogue_segment(old) and new.speaker is not None:
			problems.append(f"{backend} gave narration a voice ({new.speaker}): {new.text[:60]!r}")
		if new.speaker is not None and new.speaker not in SCENE_NAMES:
			problems.append(f"{backend} set a speaker that isn't in the scene: {new.speaker!r}")

	if backend == "booknlp":
		speaker_of = {segment.text: segment.speaker for segment in after}
		for line, speaker in BOOKNLP_EXPECTED.items():
			if speaker_of.get(line) != speaker:
				problems.append(f"booknlp gave {line!r} to {speaker_of.get(line)!r}, expected {speaker!r}")

	print(f"{backend}: " + "; ".join(f"{segment.text[:25]!r} -> {segment.speaker}" for segment in after if attribution._is_dialogue_segment(segment)))

if problems:
	print("Speech attribution in this image is broken:", file=sys.stderr)
	for problem in problems:
		print("  " + problem, file=sys.stderr)
	sys.exit(1)
print("BookNLP and fastcoref both ran and set the speakers they should.")
