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

# The web page's BookNLP size setting: FICARY_WEB_BOOKNLP_SIZES=small must take the big model off
# the menu (and so off the jobs endpoint), and without it both sizes are offered. The page is
# app.py in the image's working folder.
import importlib  # noqa: E402
import os  # noqa: E402

sys.path.insert(0, "/app")
os.environ["FICARY_WEB_BOOKNLP_SIZES"] = "small"
import app as web_page  # noqa: E402

if web_page.ALLOWED_BOOKNLP_SIZES != ("small",) or 'value="big"' in web_page.LANDING:
	problems.append("FICARY_WEB_BOOKNLP_SIZES=small still offers BookNLP's big model on the web page")
del os.environ["FICARY_WEB_BOOKNLP_SIZES"]
importlib.reload(web_page)
if web_page.ALLOWED_BOOKNLP_SIZES != ("small", "big") or 'value="big"' not in web_page.LANDING:
	problems.append("without FICARY_WEB_BOOKNLP_SIZES the web page should offer both BookNLP sizes")

# The web page's audiobook job settings: one job at a time and a day's keeping unless the
# environment says otherwise. A setting that is not a number above zero falls back to the default
# instead of stopping the server from starting.
if web_page.MAX_AUDIO_JOBS != 1 or web_page.KEEP_AUDIOBOOKS_HOURS != 24.0:
	problems.append(f"the web page's audiobook job defaults are {web_page.MAX_AUDIO_JOBS} at once kept {web_page.KEEP_AUDIOBOOKS_HOURS} hours, expected 1 and 24")
os.environ["FICARY_WEB_MAX_AUDIO_JOBS"] = "2"
os.environ["FICARY_WEB_KEEP_AUDIOBOOKS_HOURS"] = "72"
importlib.reload(web_page)
if web_page.MAX_AUDIO_JOBS != 2 or web_page.KEEP_AUDIOBOOKS_HOURS != 72.0:
	problems.append("FICARY_WEB_MAX_AUDIO_JOBS and FICARY_WEB_KEEP_AUDIOBOOKS_HOURS are not being read")
os.environ["FICARY_WEB_MAX_AUDIO_JOBS"] = "lots"
importlib.reload(web_page)
if web_page.MAX_AUDIO_JOBS != 1:
	problems.append("a FICARY_WEB_MAX_AUDIO_JOBS that is not a number should fall back to one job at a time")
del os.environ["FICARY_WEB_MAX_AUDIO_JOBS"]
del os.environ["FICARY_WEB_KEEP_AUDIOBOOKS_HOURS"]
importlib.reload(web_page)

# The server's working folder. The image names where it goes (FICARY_WEB_WORK_DIR); the server
# makes it when it starts and sends every temporary file there. This runs as the user the server
# runs as, so it also catches a folder that user cannot write to, which is what an image build
# leaves behind if it ever makes the folder as root.
import tempfile  # noqa: E402

work_folder = web_page.use_work_folder()
if work_folder is None:
	problems.append("the server could not set up its working folder; is FICARY_WEB_WORK_DIR set in the image and writable by its user?")
elif tempfile.gettempdir() != str(work_folder):
	problems.append(f"temporary files go to {tempfile.gettempdir()}, not to the server's working folder {work_folder}")
else:
	try:
		probe_folder = tempfile.mkdtemp(prefix="ficary-smoke-")
		os.rmdir(probe_folder)
	except OSError as error:
		problems.append(f"the server cannot write in its working folder {work_folder}: {error}")
if "__KEEP_HOURS__" in web_page.LANDING or "24 hours" not in web_page.LANDING:
	problems.append("the web page does not say how long a finished audiobook is kept")

if problems:
	print("This image failed its checks:", file=sys.stderr)
	for problem in problems:
		print("  " + problem, file=sys.stderr)
	sys.exit(1)
print("BookNLP and fastcoref both ran and set the speakers they should.")
