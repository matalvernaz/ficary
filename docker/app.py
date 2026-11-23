"""Web front end for ficary, the page the Docker image serves (see docker/Dockerfile).

- Text formats (epub, html, txt) are synchronous: POST /download returns the file.
- Audio is asynchronous: POST /jobs queues a job, GET /jobs/{id} polls, GET /jobs/{id}/result downloads.
  Audiobook jobs run one at a time unless FICARY_WEB_MAX_AUDIO_JOBS says otherwise, and a finished
  one is kept for FICARY_WEB_KEEP_AUDIOBOOKS_HOURS (a day by default) and then removed with its file.
  The bookkeeping for that is in web_jobs.py.
- When FICARY_WEB_WORK_DIR names a folder (the image sets it to /tmp), everything the server writes
  while working goes in a folder of its own inside it, and that folder is emptied each time the
  server starts. One server process only: the jobs are held in memory, and a second process
  starting would empty the folder under the first.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

import ficary
from ficary.exporters import EXPORTERS, check_format_deps
from ficary.sites import detect_scraper

import web_jobs
from web_jobs import Job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ficary-web")

# The folder the server's working folder is made in. Not set outside the image, where the server
# leaves the system's temporary folder as it is; the Dockerfile sets it to /tmp.
WORK_FOLDER_PARENT = os.environ.get("FICARY_WEB_WORK_DIR", "").strip()


def use_work_folder() -> Path | None:
    """Send every temporary file this process makes to the server's working folder, emptied first. Returns the folder, or None when the setting is absent or the folder cannot be used.

    Story exports, audiobook jobs and a render's scratch files all ask Python for a temporary folder, so moving that one setting moves them all.
    """
    if not WORK_FOLDER_PARENT:
        return None
    folder = web_jobs.prepare_work_folder(WORK_FOLDER_PARENT)
    if folder is not None:
        tempfile.tempdir = str(folder)
    return folder


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Done when the server starts and not when this file is imported: the image build imports it
    # as root, and a folder made then would belong to root and refuse the user the server runs as.
    use_work_folder()
    yield


app = FastAPI(title="ficary", version=ficary.__version__, lifespan=_lifespan)

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

# How many audiobooks are made at once. One, because a single render with BookNLP can use well over
# a gigabyte and a small server has room for one. A server with memory to spare can raise it.
MAX_AUDIO_JOBS = int(web_jobs.positive_number(os.environ.get("FICARY_WEB_MAX_AUDIO_JOBS"), web_jobs.DEFAULT_MAX_RUNNING_JOBS))

# How long a finished audiobook stays downloadable before it and its job are removed.
KEEP_AUDIOBOOKS_HOURS = web_jobs.positive_number(os.environ.get("FICARY_WEB_KEEP_AUDIOBOOKS_HOURS"), web_jobs.DEFAULT_KEEP_FINISHED_HOURS)


def _update(job: Job, **kw: Any) -> None:
    JOBS.update(job, **kw)


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
        # Recorded so the folder can be removed with the job, or at once if the render fails.
        _update(job, folder=str(out_dir))
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


JOBS = web_jobs.JobRegistry(
    _run_audio_job,
    max_running=MAX_AUDIO_JOBS,
    keep_finished_seconds=KEEP_AUDIOBOOKS_HOURS * web_jobs.SECONDS_PER_HOUR,
)
JOBS.start_sweeping()


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
 /* On the page for screen readers, off it for the eye. */
 .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
</head>
<body>
<h1>ficary</h1>
<p>Downloads stories from AO3, FanFiction.net, Royal Road, Wattpad, ScribbleHub, Webnovel, Literotica, and 20+ other sites.</p>

<h2>Download a story</h2>
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

<h2>Make an audiobook</h2>
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
 <!-- What a screen reader is told. It is in the page from the start and only its text ever
      changes, which is what makes a change get announced. It changes when the job moves to a
      new stage, not on every poll, so a long render is not narrated chapter by chapter. -->
 <p id="announce" role="status" class="visually-hidden"></p>
 <!-- What the eye sees: rebuilt on every poll, and deliberately not a live region. -->
 <div id="status"></div>
</fieldset>

<script>
const $ = (s) => document.querySelector(s);
const statusEl = $("#status");
const announceEl = $("#announce");
let lastAnnounced = "";
// The job this page is following. Starting another audiobook makes that one the job followed;
// the loop for the earlier one stops, so two loops never take turns rewriting the status.
let followedJob = null;

// Says something to a screen reader, once. The same sentence again is not repeated, so polling
// every two seconds does not turn into speech every two seconds.
function announce(text) {
  if (text === lastAnnounced) return;
  lastAnnounced = text;
  announceEl.textContent = text;
}

// The stage a job is at, as a sentence. This is what gets announced when it changes.
function stageSentence(j) {
  if (j.status === "queued") {
    if (!j.ahead) return "Audiobook job queued. It will start shortly.";
    return j.ahead === 1
      ? "Waiting for 1 other audiobook to finish first."
      : "Waiting for " + j.ahead + " other audiobooks to finish first.";
  }
  if (j.status === "downloading") return "Downloading the story.";
  if (j.status === "synthesizing") return "Making the audiobook. A long story can take an hour or more.";
  if (j.status === "done") return "The audiobook is ready. The Download audiobook link is on the page. The server keeps it for __KEEP_HOURS__ after it was made.";
  if (j.status === "error") return "The audiobook could not be made. " + (j.error || "");
  return "";
}

// The audiobook this browser asked for last is remembered, so that reloading the page, or having
// to sign in again, picks it back up. Without this a reload lost sight of a render that can take
// an hour, and nothing on the page could find it again.
const FOLLOWED_JOB_KEY = "ficary-audiobook-job";
function rememberJob(id) {
  try { localStorage.setItem(FOLLOWED_JOB_KEY, id); } catch (error) { /* Storage is switched off. The page works; it just cannot pick a job back up. */ }
}
function forgetJob() {
  try { localStorage.removeItem(FOLLOWED_JOB_KEY); } catch (error) { /* As above. */ }
}
function rememberedJob() {
  try { return localStorage.getItem(FOLLOWED_JOB_KEY); } catch (error) { return null; }
}

// Shows a message in the status area and says it to a screen reader.
function showAndAnnounce(text) {
  statusEl.textContent = text;
  announce(text);
}

// What the server said was wrong, out of an error reply. Its errors are JSON with a "detail".
async function problemIn(reply) {
  const body = await reply.text();
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch (error) { /* Not JSON. Say it as it came. */ }
  return body || ("the server answered " + reply.status);
}

$("#audio-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  // checkboxes need explicit bool
  fd.set("strip_notes", e.target.strip_notes.checked ? "true" : "false");
  fd.set("hr_as_stars", e.target.hr_as_stars.checked ? "true" : "false");
  statusEl.textContent = "Queuing…";
  lastAnnounced = "";
  let id = null;
  try {
    const r = await fetch("/jobs", { method: "POST", body: fd });
    if (!r.ok) {
      showAndAnnounce("The audiobook was not started: " + await problemIn(r));
      return;
    }
    id = (await r.json()).id;
  } catch (error) {
    // No connection, or a sign-in page where the answer should have been.
    showAndAnnounce("The audiobook was not started: the server could not be reached. Reload the page and try again. You may be asked to sign in.");
    return;
  }
  followedJob = id;
  rememberJob(id);
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

const CHECK_EVERY_MS = 2000;
// How many checks in a row can go unanswered before the page says so. A render that takes an hour
// will see the odd dropped connection, and that is not worth a word.
const UNANSWERED_CHECKS_BEFORE_SAYING_SO = 5;
const pause = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// Asks the server how a job is doing. Gives back the job; the word "gone" when the server says it
// does not know the job; or null when the question went unanswered this time (no connection, a
// server error, a sign-in page where the answer should be).
async function askAboutJob(id) {
  try {
    const r = await fetch("/jobs/" + encodeURIComponent(id));
    if (r.status === 404) return "gone";
    if (!r.ok) return null;
    return await r.json();
  } catch (error) {
    return null;
  }
}

async function poll(id) {
  let unanswered = 0;
  while (true) {
    if (id !== followedJob) return;
    const j = await askAboutJob(id);
    if (id !== followedJob) return;
    if (j === "gone") {
      // Only this means the job is lost. Anything else that goes wrong gets another try.
      forgetJob();
      showAndAnnounce("The server no longer has this audiobook. A finished audiobook is cleared out __KEEP_HOURS__ after it was made, and restarting the server loses one that was not finished. Start it again if you still want it.");
      return;
    }
    if (j === null) {
      unanswered += 1;
      if (unanswered === UNANSWERED_CHECKS_BEFORE_SAYING_SO) {
        showAndAnnounce("Lost contact with the server. Still trying. The audiobook carries on being made meanwhile. If this does not clear up, reload the page. You may be asked to sign in again.");
      }
      await pause(CHECK_EVERY_MS);
      continue;
    }
    unanswered = 0;
    const pct = j.total ? Math.round(100 * j.current / j.total) : 0;
    statusEl.replaceChildren();
    announce(stageSentence(j));
    if (j.status === "queued" && j.ahead) {
      addText("p", stageSentence(j));
    }

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
      addText("p", "The server keeps it for __KEEP_HOURS__ after it was made.");
      return;
    }
    if (j.status === "error") {
      // Said once. A reload should not bring the same failure back up.
      forgetJob();
      addText("pre", j.error || "unknown error");
      return;
    }
    await pause(CHECK_EVERY_MS);
  }
}

// An audiobook asked for before this page was loaded: pick it back up.
const earlierJob = rememberedJob();
if (earlierJob) {
  followedJob = earlierJob;
  poll(earlierJob);
}
</script>
</body>
</html>
"""


LANDING = LANDING.replace("__KEEP_HOURS__", web_jobs.hours_in_words(KEEP_AUDIOBOOKS_HOURS))
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
    JOBS.add(job)
    return JSONResponse({"id": job.id})


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {
        "id": job.id,
        "status": job.status,
        # How many audiobooks have to finish or start before this one. Zero unless it is waiting.
        "ahead": JOBS.jobs_ahead(job.id),
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
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.status != "done" or not job.path:
        raise HTTPException(409, f"job status is {job.status}")
    p = Path(job.path)
    if not p.is_file():
        raise HTTPException(410, "this audiobook is no longer on the server; make it again")
    return FileResponse(p, media_type="audio/mp4", filename=p.name)
