"""Web front end for ficary, the page the Docker image serves (see docker/Dockerfile).

- Text formats (epub, html, txt) are synchronous: POST /download returns the file.
- Audio is asynchronous: POST /jobs queues a job, GET /jobs/{id} polls, GET /jobs/{id}/result downloads.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

import ficary
from ficary.exporters import EXPORTERS, check_format_deps
from ficary.sites import detect_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ficary-web")

app = FastAPI(title="ficary", version=ficary.__version__)

TEXT_FORMATS = ("epub", "html", "txt")
AUDIO_FORMAT = "audio"
ATTRIBUTION_BACKENDS = ("builtin", "fastcoref", "booknlp")

# BookNLP model sizes the page offers, from FICARY_WEB_BOOKNLP_SIZES (comma-separated, default
# both). The big model needs several times the memory and time of the small one, more than a
# few gigabytes on a long chapter, so a small server can offer only "small".
BOOKNLP_SIZE_LABELS = {
    "small": "small (~150 MB, the desktop app's default)",
    "big": "big (~1 GB, more accurate, much slower)",
}
_requested_sizes = os.environ.get("FICARY_WEB_BOOKNLP_SIZES", "small,big").split(",")
ALLOWED_BOOKNLP_SIZES = tuple(size for size in BOOKNLP_SIZE_LABELS if size in {part.strip() for part in _requested_sizes}) or ("small",)


# ── Job registry ──────────────────────────────────────────────────

@dataclass
class Job:
    id: str
    url: str
    format: str
    attribution: str
    model_size: str
    speech_rate: int
    strip_notes: bool
    hr_as_stars: bool
    status: str = "queued"  # queued | downloading | synthesizing | done | error
    stage: str = ""
    current: int = 0
    total: int = 0
    detail: str = ""
    error: str | None = None
    path: str | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _update(job: Job, **kw: Any) -> None:
    with _jobs_lock:
        for k, v in kw.items():
            setattr(job, k, v)
        job.updated = time.time()


def _run_audio_job(job: Job) -> None:
    from ficary.tts import generate_audiobook

    try:
        _update(job, status="downloading", stage="fetching story")
        scraper = detect_scraper(job.url)()

        def dl_progress(current: int, total: int, title: str, cached: bool) -> None:
            _update(job, current=current, total=total, detail=f"{title}{' (cached)' if cached else ''}")

        story = scraper.download(job.url, progress_callback=dl_progress)

        _update(job, status="synthesizing", stage="tts + attribution", current=0, total=len(story.chapters), detail="")

        def tts_progress(current: int, total: int, title: str) -> None:
            _update(job, current=current, total=total, detail=title)

        out_dir = Path(tempfile.mkdtemp(prefix=f"ficary-{job.id}-"))
        path = generate_audiobook(
            story,
            str(out_dir),
            progress_callback=tts_progress,
            speech_rate=job.speech_rate,
            attribution_backend=job.attribution,
            attribution_model_size=job.model_size or None,
            strip_notes=job.strip_notes,
            hr_as_stars=job.hr_as_stars,
        )
        _update(job, status="done", path=str(path), stage="complete", detail=Path(path).name)
    except Exception as exc:
        logger.exception("audio job %s failed", job.id)
        _update(job, status="error", error=str(exc))


# ── HTTP endpoints ────────────────────────────────────────────────

LANDING = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ficary</title>
<style>
 body { font-family: system-ui, sans-serif; max-width: 44rem; margin: 3rem auto; padding: 0 1rem; color: #222; }
 input, select, button { font-size: 1rem; padding: .5rem; }
 input[type=url] { width: 100%; box-sizing: border-box; }
 label { display: block; margin-top: 1rem; }
 fieldset { margin-top: 1.5rem; }
 .row { display: flex; gap: .75rem; flex-wrap: wrap; }
 .row > label { flex: 1; min-width: 10rem; margin-top: 0; }
 button { margin-top: 1.5rem; padding: .6rem 1.2rem; cursor: pointer; }
 #status { margin-top: 1.5rem; }
 progress { width: 100%; }
</style>
</head>
<body>
<h1>ficary</h1>
<p>Downloads stories from AO3, FanFiction.net, Royal Road, Wattpad, ScribbleHub, Webnovel, Literotica, and 20+ other sites.</p>

<form id="text-form" method="post" action="/download" autocomplete="off">
 <label>Story URL
  <input type="url" name="url" required autofocus autocomplete="off" spellcheck="false" placeholder="https://archiveofourown.org/works/...">
 </label>
 <div class="row">
  <label>Format
   <select name="format">
    <option value="epub">EPUB</option>
    <option value="html">HTML</option>
    <option value="txt">TXT</option>
   </select>
  </label>
 </div>
 <button type="submit">Download</button>
</form>

<fieldset>
 <legend><strong>Audiobook (async)</strong></legend>
 <form id="audio-form" autocomplete="off">
  <label>Story URL
   <input type="url" name="url" required autocomplete="off" spellcheck="false" placeholder="https://archiveofourown.org/works/...">
  </label>
  <div class="row">
   <label>Attribution
    <select name="attribution">
     <option value="booknlp" selected>BookNLP (best)</option>
     <option value="fastcoref">fastcoref</option>
     <option value="builtin">builtin (fast)</option>
    </select>
   </label>
   <label>Model size
    <select name="model_size">
__MODEL_SIZE_OPTIONS__
    </select>
   </label>
   <label>Speech rate %
    <input type="number" name="speech_rate" value="0" min="-50" max="100">
   </label>
  </div>
  <div class="row">
   <label><input type="checkbox" name="strip_notes"> strip author notes</label>
   <label><input type="checkbox" name="hr_as_stars" checked> scene dividers as silence</label>
  </div>
  <button type="submit">Start audiobook job</button>
 </form>
 <div id="status"></div>
</fieldset>

<script>
const $ = (s) => document.querySelector(s);
const statusEl = $("#status");

$("#audio-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  // checkboxes need explicit bool
  fd.set("strip_notes", e.target.strip_notes.checked ? "true" : "false");
  fd.set("hr_as_stars", e.target.hr_as_stars.checked ? "true" : "false");
  statusEl.textContent = "Queuing…";
  const r = await fetch("/jobs", { method: "POST", body: fd });
  if (!r.ok) { statusEl.textContent = "Error: " + await r.text(); return; }
  const { id } = await r.json();
  poll(id);
});

// Adds an element holding plain text to the status area. Chapter titles and error messages
// come from the story site, so they are always set as text and never parsed as HTML.
function addText(tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  statusEl.appendChild(element);
  return element;
}

async function poll(id) {
  while (true) {
    const r = await fetch("/jobs/" + id);
    if (!r.ok) { statusEl.textContent = "Lost job " + id; return; }
    const j = await r.json();
    const pct = j.total ? Math.round(100 * j.current / j.total) : 0;
    statusEl.replaceChildren();

    // What the job is doing, then how far through it is.
    const summary = addText("p", "");
    const statusWord = document.createElement("strong");
    statusWord.textContent = j.status;
    summary.append(statusWord, " — " + (j.stage || "") + (j.detail ? " · " + j.detail : ""));
    const bar = document.createElement("progress");
    bar.value = j.current;
    bar.max = j.total || 1;
    statusEl.appendChild(bar);
    addText("p", `${j.current} / ${j.total || "?"} (${pct}%)`);

    if (j.status === "done") {
      const line = document.createElement("p");
      const link = document.createElement("a");
      link.href = "/jobs/" + encodeURIComponent(id) + "/result";
      link.textContent = "Download audiobook";
      line.appendChild(link);
      statusEl.appendChild(line);
      return;
    }
    if (j.status === "error") {
      addText("pre", j.error || "unknown error");
      return;
    }
    await new Promise(r => setTimeout(r, 2000));
  }
}
</script>
</body>
</html>
"""


LANDING = LANDING.replace(
    "__MODEL_SIZE_OPTIONS__",
    "\n".join(
        f'     <option value="{size}"{" selected" if index == 0 else ""}>{BOOKNLP_SIZE_LABELS[size]}</option>'
        for index, size in enumerate(ALLOWED_BOOKNLP_SIZES)
    ),
)


@app.get("/", response_class=HTMLResponse)
def landing() -> str:
    return LANDING


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/download")
def download(url: str = Form(...), format: str = Form("epub")):
    if format not in TEXT_FORMATS:
        raise HTTPException(400, f"format must be one of {TEXT_FORMATS} (use /jobs for audio)")
    try:
        check_format_deps(format)
    except ImportError as exc:
        raise HTTPException(500, str(exc)) from exc

    scraper = detect_scraper(url)()
    try:
        story = scraper.download(url)
    except Exception as exc:
        logger.exception("download failed for %s", url)
        raise HTTPException(502, f"download failed: {exc}") from exc

    out_dir = Path(tempfile.mkdtemp(prefix="ficary-"))
    try:
        path = EXPORTERS[format](story, str(out_dir))
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        logger.exception("export failed for %s", url)
        raise HTTPException(500, f"export failed: {exc}") from exc

    media = {
        "epub": "application/epub+zip",
        "html": "text/html; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
    }[format]
    # The file only exists to be sent once, so its folder goes as soon as the response is done.
    # Without this every download stayed in the container's /tmp until the container was replaced.
    cleanup = BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True)
    return FileResponse(path, media_type=media, filename=Path(path).name, background=cleanup)


@app.post("/jobs")
def start_job(
    url: str = Form(...),
    format: str = Form(AUDIO_FORMAT),
    attribution: str = Form("booknlp"),
    # Same default as the desktop app, small, unless this server doesn't offer it.
    model_size: str = Form(ALLOWED_BOOKNLP_SIZES[0]),
    speech_rate: int = Form(0),
    strip_notes: str = Form("false"),
    hr_as_stars: str = Form("true"),
):
    if format != AUDIO_FORMAT:
        raise HTTPException(400, "jobs endpoint is for audio format only")
    if attribution not in ATTRIBUTION_BACKENDS:
        raise HTTPException(400, f"attribution must be one of {ATTRIBUTION_BACKENDS}")
    if model_size not in ALLOWED_BOOKNLP_SIZES + ("",):
        raise HTTPException(400, f"model_size must be one of {ALLOWED_BOOKNLP_SIZES} on this server")

    try:
        check_format_deps(AUDIO_FORMAT)
    except ImportError as exc:
        raise HTTPException(500, str(exc)) from exc

    job = Job(
        id=uuid.uuid4().hex[:12],
        url=url,
        format=format,
        attribution=attribution,
        model_size=model_size,
        speech_rate=speech_rate,
        strip_notes=strip_notes.lower() in ("1", "true", "yes", "on"),
        hr_as_stars=hr_as_stars.lower() in ("1", "true", "yes", "on"),
    )
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=_run_audio_job, args=(job,), daemon=True, name=f"job-{job.id}").start()
    return JSONResponse({"id": job.id})


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "current": job.current,
        "total": job.total,
        "detail": job.detail,
        "error": job.error,
        "created": job.created,
        "updated": job.updated,
    }


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.status != "done" or not job.path:
        raise HTTPException(409, f"job status is {job.status}")
    p = Path(job.path)
    return FileResponse(p, media_type="audio/mp4", filename=p.name)
