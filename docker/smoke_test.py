"""Runs BookNLP and fastcoref once each inside the Docker image, so an image whose speech
attribution no longer works is never published.

The Dockerfile's import check can't catch a backend that imports fine and then fails on its
first story, and the audiobook code hides that kind of failure on purpose by falling back to
the built-in attribution. Both backends have done exactly that: BookNLP once setuptools
dropped pkg_resources, and fastcoref under transformers 5.

Run it against an image (it downloads about 160 MB of BookNLP and 360 MB of fastcoref models):
    docker run --rm -i ghcr.io/matalvernaz/ficary python - < docker/smoke_test.py
"""
import sys

from ficary import attribution, tts

# A few lines of dialogue with named speakers, enough for every backend to do real work.
PASSAGE = (
    '"Where are you going?" Anna asked.\n\n'
    '"To the station," said Ben. "The last train leaves at nine."\n\n'
    'Anna frowned. "Then I am coming with you."\n\n'
    '"Suit yourself," Ben said.\n'
)

BACKENDS = (
    ("booknlp", "small"),
    ("fastcoref", None),
)

segments = tts.parse_segments(PASSAGE)
failed = []
for backend, size in BACKENDS:
    # refine_speakers never raises; a failure shows up as has_failed() and a logged warning.
    attribution.refine_speakers(list(segments), PASSAGE, backend=backend, model_size=size)
    if attribution.has_failed(backend, size):
        failed.append(backend)

if failed:
    print("These attribution backends fell back to the built-in one: " + ", ".join(failed), file=sys.stderr)
    print("The warning logged above says why; the run-time patches are in ficary/attribution.py.", file=sys.stderr)
    sys.exit(1)
print("Every attribution backend in the smoke test ran: " + ", ".join(name for name, _ in BACKENDS))
