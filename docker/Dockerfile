# Container image of ficary's web front end (docker/app.py): a page that downloads a story as
# EPUB, HTML or TXT, or turns it into an audiobook in the background. Published as
# ghcr.io/matalvernaz/ficary by .github/workflows/build-docker.yml for every release tag.
#
# Build from the repository root:   docker build -f docker/Dockerfile .
# Run with a volume on /data:       docker run -p 8000:8000 -v ficary-data:/data ghcr.io/matalvernaz/ficary
#
# /data is the home folder of the user the server runs as, so the download cache and the speech
# attribution models (BookNLP's big model is about 1 GB) survive the container being replaced.
# A folder mounted there must be writable by uid 1000.

FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg joins the synthesised chapters into the audiobook. curl is for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# torch comes first and only from PyTorch's CPU index. The PyPI build pulls in about 3.5 GB of
# NVIDIA CUDA libraries, on ARM as well, and nothing in this image can use a GPU.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# The speech attribution backends and the web server. torch is already satisfied, so it stays
# the CPU build.
# BookNLP goes in without its declared dependencies. It lists tensorflow, about 1.6 GB with
# everything tensorflow brings, but never imports it. What it does import is torch,
# transformers, spaCy, tqdm and pkg_resources. setuptools is lifted to a current release, which
# has no pkg_resources: PyTorch's index ships an old one that still does, and with it the image
# would never take the path a new desktop install takes, where ficary lends BookNLP the one
# function it needs.
# Neither backend has had a release that works with current libraries as-is, so ficary
# patches both at run time (ficary/attribution.py), and docker/smoke_test.py runs both on
# every build and fails it if either one falls back to the built-in attribution.
RUN pip install \
        fastapi \
        "uvicorn[standard]" \
        python-multipart \
        transformers \
        spacy \
        tqdm \
        "setuptools>=82" \
        fastcoref \
    && pip install --no-deps booknlp \
    && python -m spacy download en_core_web_sm

# ficary itself, from this checkout, with the EPUB and audiobook extras.
COPY pyproject.toml /src/
COPY ficary /src/ficary
RUN pip install "/src[epub,audio]" \
    && rm -rf /src

# Refuse to publish an image that picked up a GPU build after all.
RUN if pip list --format=freeze | grep -i -E '^(nvidia-|triton==)'; then \
        echo "GPU packages are installed; torch has to come from the CPU index." >&2; \
        exit 1; \
    fi

WORKDIR /app
COPY docker/app.py /app/app.py

# Import the server and everything the attribution backends load, once, so a dependency that
# broke since the last release fails this build instead of the first audiobook. Importing the
# app registers its form routes, which needs python-multipart. BookNLP's own modules only
# import once ficary has supplied pkg_resources, so the smoke test covers those.
RUN python -c "import app, ficary.tts, ficary.attribution, fastcoref, booknlp, spacy; spacy.load('en_core_web_sm')"

# Run as an ordinary user whose home is /data.
RUN useradd --uid 1000 --user-group --home-dir /data --no-create-home --shell /usr/sbin/nologin ficary \
    && mkdir -p /data \
    && chown ficary:ficary /data
USER ficary
ENV HOME=/data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
