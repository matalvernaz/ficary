"""Web front end for ficary, the page the Docker image serves (see docker/Dockerfile).

- Every format is a job: POST /jobs queues one, GET /jobs/{id} polls, GET /jobs/{id}/result downloads.
  Text files (epub, html, txt) and audiobooks have a queue each, so a quick EPUB never waits behind an
  hour-long render. Audiobook jobs run one at a time unless FICARY_WEB_MAX_AUDIO_JOBS says otherwise;
  text jobs run one at a time, which keeps the page from asking a story site for two stories at once.
  A finished job is kept for FICARY_WEB_KEEP_AUDIOBOOKS_HOURS (a day by default) and then removed with
  its file. The bookkeeping for that is in web_jobs.py.
- POST /download still makes a text file while the request waits and returns it, for scripts that use
  it. The page no longer does: a long story could outlast Cloudflare's 100 seconds for one request.
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
import threading
import time
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
import web_voices
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

# How many voice samples may be made at once. A sample is one short sentence, but every one is a
# trip to a voice service, and the button is easy to press repeatedly.
MAX_SAMPLES_AT_ONCE = 2
_SAMPLE_SLOTS = threading.BoundedSemaphore(MAX_SAMPLES_AT_ONCE)

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
            narrator_voice=job.narrator_voice or None,
            character_voices=job.character_voices,
            mix_character_engines=job.mix_engines,
            enabled_tts_providers=job.tts_providers,
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


def _run_text_job(job: Job) -> None:
    web_jobs.run_text_job(
        job,
        registry=TEXT_JOBS,
        download_story=lambda url, progress: detect_scraper(url)().download(url, progress_callback=progress),
        export=lambda format, story, folder: EXPORTERS[format](story, folder),
        make_folder=lambda job: Path(tempfile.mkdtemp(prefix=f"ficary-{job.id}-")),
    )


# Text files: one at a time, so the page never asks a story site for two stories at once.
TEXT_JOBS = web_jobs.JobRegistry(
    _run_text_job,
    max_running=1,
    keep_finished_seconds=KEEP_AUDIOBOOKS_HOURS * web_jobs.SECONDS_PER_HOUR,
    name="text",
)
TEXT_JOBS.start_sweeping()


def _find_job(job_id: str) -> tuple[web_jobs.JobRegistry, Job] | None:
    """The job with this id and the registry that has it, whichever kind it is."""
    for registry in (JOBS, TEXT_JOBS):
        job = registry.get(job_id)
        if job is not None:
            return registry, job
    return None


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
 /* A list as wide as its longest choice ran past its column, under the next field or out of the
    box; each field fills its own column instead. */
 .row select, .row input[type=number] { width: 100%; box-sizing: border-box; }
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
<!-- The script below sends this to /jobs and follows the job. Without the script, the browser
     posts it to /download and waits for the file, as the page used to. -->
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
<!-- What the eye sees of a download: rebuilt on every poll, and deliberately not a live region. -->
<div id="text-status"></div>

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
   <label>Speed
    <input type="number" name="speed" value="1" min="0.5" max="12" step="any" aria-describedby="speed-hint">
   </label>
  </div>
  <p id="speed-hint">Times normal: 1 is normal speed, 2 is twice as fast, up to 12. <span id="speed-cap"></span></p>
  <div class="row">
   <label><input type="checkbox" name="strip_notes"> strip author notes</label>
   <label><input type="checkbox" name="hr_as_stars" checked> scene dividers as silence</label>
  </div>
  <!-- Filled in by the script from /voices: the engines this server has, then the voices of the
       chosen engine. Without the script the audiobook uses ficary's default voices. -->
  <div class="row">
   <label>Voice engine
    <select id="voice-engine" disabled>
     <option>Loading voices…</option>
    </select>
   </label>
   <label>Narrator voice
    <select name="narrator_voice" id="narrator-voice" disabled>
     <option value="">Ficary's default</option>
    </select>
   </label>
  </div>
  <div class="row">
   <label>Character voices
    <select name="character_voices" id="character-voices">
     <option value="engine" selected>From the narrator's engine</option>
     <option value="any">From every engine, mixed</option>
     <option value="narrator">None: the narrator reads everything</option>
    </select>
   </label>
  </div>
  <p id="one-voice-note" hidden>With one voice, speaker attribution is skipped: it only decides which voice says each line.</p>
  <button type="button" id="listen" disabled>Listen to the narrator voice</button>
  <audio id="sample-audio" controls hidden></audio>
  <!-- What went wrong with a sample, for the eye; the screen reader hears it through #announce. -->
  <p id="sample-status"></p>
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

// ── Voices ──────────────────────────────────────────────────────
// The engine list and the voice list for the chosen engine, from /voices. The narrator voice and
// the character choice are remembered in this browser, so a favourite voice stays chosen.
const engineSelect = $("#voice-engine");
const voiceSelect = $("#narrator-voice");
const castSelect = $("#character-voices");
const listenButton = $("#listen");
const speedInput = document.querySelector('#audio-form input[name="speed"]');
const speedCap = $("#speed-cap");
const SPEED_KEY = "ficary-speed";
const sampleAudio = $("#sample-audio");
const NARRATOR_KEY = "ficary-narrator-voice";
const CAST_KEY = "ficary-character-voices";
let allVoices = [];
let defaultVoiceId = null;
// "loading" until /voices answers, then "ready", or "failed" when there is no list to choose from.
// Until it is ready the narrator list is switched off and sends nothing, so a saved narrator
// would silently become the default.
let voicesState = "loading";

function remembered(key) {
  try { return localStorage.getItem(key); } catch (error) { return null; }
}
function remember(key, value) {
  try { localStorage.setItem(key, value); } catch (error) { /* Storage is off; nothing is remembered. */ }
}

// "en-GB" as "British English", in the browser's own language. A tag the browser does not know
// is shown as it is.
let languageNames = null;
try { languageNames = new Intl.DisplayNames([navigator.language || "en"], { type: "language" }); } catch (error) { /* Older browser. */ }
function languageOf(locale) {
  if (!locale) return "";
  try { return (languageNames && languageNames.of(locale)) || locale; } catch (error) { return locale; }
}

function voiceLabel(v) {
  const parts = [v.name];
  const language = languageOf(v.locale);
  if (language) parts.push(language);
  if (v.gender === "Male" || v.gender === "Female") parts.push(v.gender.toLowerCase());
  return parts.join(", ");
}

// The browser's own language first, then the rest by language and name.
const ownLanguage = (navigator.language || "en").split("-")[0].toLowerCase();
function voiceOrder(a, b) {
  const aOwn = (a.locale || "").toLowerCase().split("-")[0] === ownLanguage ? 0 : 1;
  const bOwn = (b.locale || "").toLowerCase().split("-")[0] === ownLanguage ? 0 : 1;
  if (aOwn !== bOwn) return aOwn - bOwn;
  return languageOf(a.locale).localeCompare(languageOf(b.locale)) || a.name.localeCompare(b.name);
}

function fillVoices(group, chosen) {
  voiceSelect.replaceChildren();
  const voices = allVoices.filter((v) => v.group === group).sort(voiceOrder);
  for (const v of voices) {
    const option = new Option(voiceLabel(v), v.id);
    voiceSelect.add(option);
  }
  if (chosen && voices.some((v) => v.id === chosen)) {
    voiceSelect.value = chosen;
    return;
  }
  // A newly chosen engine starts on its main voice rather than the first name in the alphabet,
  // which for Eloquence is Bobby, a child: the page's default voice when it is this engine's,
  // otherwise the first grown-up voice in the browser's language, in the engine's own order
  // (Reed, DECtalk's Paul, Microsoft Sam).
  const ownAdults = allVoices.filter((v) => v.group === group
    && (v.gender === "Male" || v.gender === "Female")
    && (v.locale || "").toLowerCase().split("-")[0] === ownLanguage);
  const main = voices.find((v) => v.id === defaultVoiceId) || ownAdults[0];
  if (main) voiceSelect.value = main.id;
}

async function loadVoices() {
  // The character choice does not depend on the voice list, so it is put back first: a list
  // that fails to load must not turn a saved one-voice choice back into a cast.
  const savedCast = remembered(CAST_KEY);
  if (savedCast === "engine" || savedCast === "any" || savedCast === "narrator") castSelect.value = savedCast;
  showCastChoice();
  const savedSpeed = remembered(SPEED_KEY);
  if (savedSpeed && !isNaN(parseFloat(savedSpeed))) speedInput.value = savedSpeed;
  let body;
  try {
    const reply = await fetch("/voices");
    if (!reply.ok) throw new Error(await problemIn(reply));
    body = await reply.json();
  } catch (error) {
    voicesState = "failed";
    voicesUnavailable("The voice list could not be loaded, so the audiobook will use Ficary's default voices.");
    return;
  }
  allVoices = body.voices || [];
  defaultVoiceId = body.default || null;
  if (!allVoices.length) {
    voicesState = "failed";
    voicesUnavailable("This server lists no voices, so the audiobook will use Ficary's default voices.");
    return;
  }
  voicesState = "ready";
  const counts = new Map();
  for (const v of allVoices) counts.set(v.group, (counts.get(v.group) || 0) + 1);
  const defaultGroup = (allVoices.find((v) => v.id === body.default) || allVoices[0]).group;
  const groups = [...counts.keys()].sort((a, b) => (a === defaultGroup ? -1 : b === defaultGroup ? 1 : a.localeCompare(b)));
  engineSelect.replaceChildren();
  for (const group of groups) {
    const n = counts.get(group);
    engineSelect.add(new Option(group + " (" + n + (n === 1 ? " voice)" : " voices)"), group));
  }
  const saved = remembered(NARRATOR_KEY);
  const savedVoice = allVoices.find((v) => v.id === saved);
  const start = savedVoice || allVoices.find((v) => v.id === body.default) || allVoices[0];
  engineSelect.value = start.group;
  fillVoices(start.group, start.id);
  showSpeedCap();
  engineSelect.disabled = false;
  voiceSelect.disabled = false;
  listenButton.disabled = false;
  if (saved && !savedVoice) {
    // The narrator saved here is missing, usually because the speech server it lives on is not
    // answering. Quietly starting on the default would send the story to a different voice
    // service than the one chosen, so the list asks for a choice and the form will not go
    // until one is made.
    voiceSelect.add(new Option("Choose a narrator", ""), 0);
    voiceSelect.value = "";
    voiceSelect.required = true;
    sampleProblem("The narrator saved in this browser isn't available right now; its speech server may be down. Choose a narrator before starting an audiobook.");
  }
}

// Says why there is no voice choice, on the page and to a screen reader; text in a disabled list
// is not reliably read out.
function voicesUnavailable(text) {
  engineSelect.replaceChildren(new Option("No voices to choose from"));
  sampleProblem(text);
}

engineSelect.addEventListener("change", () => {
  fillVoices(engineSelect.value, null);
  remember(NARRATOR_KEY, voiceSelect.value);
  dropSample();
  showSpeedCap();
});
voiceSelect.addEventListener("change", () => {
  remember(NARRATOR_KEY, voiceSelect.value);
  // A choice made answers the "choose a narrator" request, so that sentence goes.
  if (voiceSelect.value) sampleStatus.textContent = "";
  dropSample();
  showSpeedCap();
});
// The sample is made at the speed in the form, so a sample made before the speed changed is out of
// date; it goes, and Listen makes a new one. It is not replayed by itself, which would talk over a
// screen reader saying the number being typed.
speedInput.addEventListener("input", () => {
  dropSample();
  showSpeedCap();
});
speedInput.addEventListener("change", () => {
  remember(SPEED_KEY, speedInput.value);
  // Said once the number is settled, not at every keystroke.
  if (speedCap.textContent) announce(speedCap.textContent);
});

// Says, beside the speed, when the chosen voice can't go as fast as asked (Edge stops at twice
// normal), rather than quietly making it slower.
function showSpeedCap() {
  const voice = allVoices.find((v) => v.id === voiceSelect.value);
  const speed = parseFloat(speedInput.value);
  if (voice && voice.max_speed && speed > voice.max_speed) {
    speedCap.textContent = voice.group + " voices go up to " + voice.max_speed + " times normal, so this one would be made at " + voice.max_speed + ".";
  } else {
    speedCap.textContent = "";
  }
}
// With one voice the attribution choices do nothing, so they are switched off and say why.
const attributionSelect = document.querySelector('#audio-form select[name="attribution"]');
const modelSizeSelect = document.querySelector('#audio-form select[name="model_size"]');
const oneVoiceNote = $("#one-voice-note");
function showCastChoice() {
  const oneVoice = castSelect.value === "narrator";
  attributionSelect.disabled = oneVoice;
  modelSizeSelect.disabled = oneVoice;
  oneVoiceNote.hidden = !oneVoice;
}
castSelect.addEventListener("change", () => {
  remember(CAST_KEY, castSelect.value);
  showCastChoice();
});

let sampleUrl = null;
const sampleStatus = $("#sample-status");
// Takes away a sample that no longer matches the chosen voice or speech rate.
function dropSample() {
  if (!sampleUrl) return;
  sampleAudio.pause();
  sampleAudio.removeAttribute("src");
  sampleAudio.load();
  sampleAudio.hidden = true;
  URL.revokeObjectURL(sampleUrl);
  sampleUrl = null;
}
// Kept apart from the job's status area, which a followed job rewrites every two seconds.
function sampleProblem(text) {
  sampleStatus.textContent = text;
  announce(text);
}
listenButton.addEventListener("click", async () => {
  const option = voiceSelect.selectedOptions[0];
  if (!option || !option.value) return;
  const name = option.textContent.split(",")[0];
  sampleStatus.textContent = "";
  announce("Getting a sample of " + name + ".");
  listenButton.disabled = true;
  try {
    const fd = new FormData();
    fd.set("voice", option.value);
    fd.set("speed", speedInput.value || "1");
    const reply = await fetch("/voices/sample", { method: "POST", body: fd });
    if (!reply.ok) {
      sampleProblem("The sample could not be played: " + await problemIn(reply));
      return;
    }
    if (sampleUrl) URL.revokeObjectURL(sampleUrl);
    sampleUrl = URL.createObjectURL(await reply.blob());
    sampleAudio.src = sampleUrl;
    sampleAudio.hidden = false;
    await sampleAudio.play();
  } catch (error) {
    sampleProblem("The sample could not be played: " + error);
  } finally {
    listenButton.disabled = false;
  }
});

loadVoices();

$("#audio-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  // A narrator chosen in this browser is only sent once the voice list has put it in the list.
  // Someone who never chose one gets the default, as before.
  if (voicesState !== "ready" && remembered(NARRATOR_KEY)) {
    sampleProblem(voicesState === "loading"
      ? "The voice list is still loading, so your narrator isn't set yet. Try again in a moment."
      : "The voice list couldn't be loaded, so the narrator you chose can't be used. Reload the page to try again.");
    return;
  }
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

// ── Text files ─────────────────────────────────────────────────────
// An EPUB, HTML or TXT file is a job too. It used to be made while the page's request waited, and
// a long story could outlast Cloudflare's 100 seconds for one request. It is followed in its own
// status area, and its sentences go through the same announcer as the audiobook's.
const textStatusEl = $("#text-status");
let followedTextJob = null;
const FORMAT_NAMES = { epub: "EPUB", html: "HTML", txt: "TXT" };

// Remembered like an audiobook, so a reload or a sign-in in the middle picks it back up, and
// forgotten once the file has been handed over, so a reload never downloads it a second time.
const TEXT_JOB_KEY = "ficary-text-job";
function rememberTextJob(id) {
  try { localStorage.setItem(TEXT_JOB_KEY, id); } catch (error) { /* Storage is switched off. */ }
}
function forgetTextJob() {
  try { localStorage.removeItem(TEXT_JOB_KEY); } catch (error) { /* As above. */ }
}
function rememberedTextJob() {
  try { return localStorage.getItem(TEXT_JOB_KEY); } catch (error) { return null; }
}

// The stage a download is at, as a sentence. What gets announced when it changes.
function textSentence(j) {
  const name = FORMAT_NAMES[j.format] || "story";
  if (j.status === "queued") {
    if (!j.ahead) return "Download queued. It will start shortly.";
    return j.ahead === 1
      ? "Waiting for 1 other download to finish first."
      : "Waiting for " + j.ahead + " other downloads to finish first.";
  }
  if (j.status === "downloading") return "Downloading the story.";
  if (j.status === "done") return "The " + name + " file is ready and should be downloading now. If it is not, use the Download link on the page.";
  if (j.status === "error") return "The story could not be downloaded. " + (j.error || "");
  return "";
}

function showTextStatus(text) {
  textStatusEl.textContent = text;
  announce(text);
}

function addTextTo(parent, tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

$("#text-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  textStatusEl.textContent = "Starting…";
  lastAnnounced = "";
  let id = null;
  try {
    const r = await fetch("/jobs", { method: "POST", body: fd });
    if (!r.ok) {
      showTextStatus("The download was not started: " + await problemIn(r));
      return;
    }
    id = (await r.json()).id;
  } catch (error) {
    showTextStatus("The download was not started: the server could not be reached. Reload the page and try again. You may be asked to sign in.");
    return;
  }
  followedTextJob = id;
  rememberTextJob(id);
  followTextJob(id);
});

async function followTextJob(id) {
  let unanswered = 0;
  while (true) {
    if (id !== followedTextJob) return;
    const j = await askAboutJob(id);
    if (id !== followedTextJob) return;
    if (j === "gone") {
      forgetTextJob();
      showTextStatus("The server no longer has this download. Download the story again.");
      return;
    }
    if (j === null) {
      unanswered += 1;
      if (unanswered === UNANSWERED_CHECKS_BEFORE_SAYING_SO) {
        showTextStatus("Lost contact with the server. Still trying. If this does not clear up, reload the page. You may be asked to sign in again.");
      }
      await pause(CHECK_EVERY_MS);
      continue;
    }
    unanswered = 0;
    textStatusEl.replaceChildren();
    announce(textSentence(j));
    addTextTo(textStatusEl, "p", textSentence(j));
    if (j.total) {
      const bar = document.createElement("progress");
      bar.value = j.current;
      bar.max = j.total;
      textStatusEl.appendChild(bar);
      addTextTo(textStatusEl, "p", `${j.current} / ${j.total} chapters`);
    }
    if (j.status === "done") {
      forgetTextJob();
      const result = "/jobs/" + encodeURIComponent(id) + "/result";
      const line = document.createElement("p");
      const link = document.createElement("a");
      link.href = result;
      link.textContent = "Download " + (j.detail || "the file");
      line.appendChild(link);
      textStatusEl.appendChild(line);
      // The server sends it as an attachment, so the browser saves it and the page stays.
      window.location.assign(result);
      return;
    }
    if (j.status === "error") {
      forgetTextJob();
      return;
    }
    await pause(CHECK_EVERY_MS);
  }
}

// A download asked for before this page was loaded: pick it back up.
const earlierTextJob = rememberedTextJob();
if (earlierTextJob) {
  followedTextJob = earlierTextJob;
  followTextJob(earlierTextJob);
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


@app.get("/voices")
def list_voices() -> dict:
    """The voices the audiobook form offers, with the family each belongs to."""
    return web_voices.voices_for_page(web_voices.available_voices())


@app.post("/voices/sample")
def voice_sample(voice: str = Form(...), speed: float | None = Form(None), speech_rate: int = Form(0)):
    """A sentence in the chosen voice at the form's speed, as MP3, so a voice can be heard as the
    book will sound before it is made. ``speed`` is times normal; ``speech_rate``, a percent change,
    is what it took before and is still taken when no speed is given."""
    from ficary import tts_providers

    info = tts_providers.voice_by_id(voice)
    if info is None:
        raise HTTPException(404, "That voice is not available on this server.")
    percent = web_voices.percent_from_speed(speed) if speed is not None else speech_rate
    rate = web_voices.sample_rate(percent, info.id)
    text = web_voices.sample_text(info)
    started = time.monotonic()
    if not _SAMPLE_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Other samples are being made. Try again in a moment.")
    # Uncompressed where the voice's provider has it: MP3 smeared the clicks and hiss of p and s
    # sounds in formant voices like Eloquence. Edge only ever sends MP3.
    lossless = getattr(tts_providers.get_provider(info.provider), "lossless", False)
    folder = None
    try:
        # Made inside the try, so a full or unwritable disk cannot keep the slot taken for good.
        folder = Path(tempfile.mkdtemp(prefix="ficary-sample-"))
        path = folder / ("sample.wav" if lossless else "sample.mp3")
        tts_providers.synthesize(info.id, text, path, rate=rate)
    except Exception as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        logger.warning("sample of %s at %s failed: %s", voice, rate or "normal speed", exc)
        raise HTTPException(502, tts_providers.explain_synthesis_error(exc, retry_hint="then press Listen again")) from exc
    finally:
        _SAMPLE_SLOTS.release()
    # Which voice and speed were asked for and what the speed became, so a "too slow" can be looked up.
    logger.info(
        "sample: %s, speed asked %s, made at %s, in %.1f s",
        voice, f"{speed:g}x" if speed is not None else f"{speech_rate:+d}%", rate or "normal speed",
        time.monotonic() - started,
    )
    cleanup = BackgroundTask(shutil.rmtree, folder, ignore_errors=True)
    media_type = "audio/wav" if lossless else "audio/mpeg"
    return FileResponse(path, media_type=media_type, filename=path.name, background=cleanup)


@app.post("/download")
def download(url: str = Form(...), format: str = Form("epub")):
    """Make a text file while the request waits, and return it. Kept for scripts; the page uses
    /jobs, since a long story could outlast Cloudflare's 100 seconds for one request."""
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
    # Times normal, from the page; speech_rate (a percent change) is for scripts and older pages.
    speed: float | None = Form(None),
    strip_notes: str = Form("false"),
    hr_as_stars: str = Form("true"),
    narrator_voice: str = Form(""),
    character_voices: str = Form(web_voices.CHARACTER_VOICES_FROM_ENGINE),
):
    if format in TEXT_FORMATS:
        try:
            check_format_deps(format)
        except ImportError as exc:
            raise HTTPException(500, str(exc)) from exc
        text_job = Job(id=uuid.uuid4().hex[:12], url=url, format=format)
        TEXT_JOBS.add(text_job)
        return JSONResponse({"id": text_job.id})
    if format != AUDIO_FORMAT:
        raise HTTPException(400, f"format must be one of {TEXT_FORMATS + (AUDIO_FORMAT,)}")
    if attribution not in ATTRIBUTION_BACKENDS:
        raise HTTPException(400, f"attribution must be one of {ATTRIBUTION_BACKENDS}")
    if model_size not in ALLOWED_BOOKNLP_SIZES + ("",):
        raise HTTPException(400, f"model_size must be one of {ALLOWED_BOOKNLP_SIZES} on this server")

    if character_voices not in web_voices.CHARACTER_VOICE_CHOICES:
        raise HTTPException(400, f"character_voices must be one of {web_voices.CHARACTER_VOICE_CHOICES}")
    if character_voices == web_voices.CHARACTER_VOICES_NARRATOR:
        # Attribution only decides who says each line, and with one voice that changes nothing,
        # so BookNLP's minutes and gigabyte are skipped.
        attribution, model_size = "builtin", ""
    narrator_voice = narrator_voice.strip()
    if narrator_voice:
        from ficary import tts_providers

        if tts_providers.voice_by_id(narrator_voice) is None:
            raise HTTPException(400, "That narrator voice is not available on this server.")

    try:
        check_format_deps(AUDIO_FORMAT)
    except ImportError as exc:
        raise HTTPException(500, str(exc)) from exc

    if speed is not None:
        speech_rate = web_voices.percent_from_speed(speed)
    cast, mix_engines, only_providers = web_voices.character_cast(narrator_voice, character_voices, web_voices.available_voices())
    job = Job(
        id=uuid.uuid4().hex[:12],
        url=url,
        format=format,
        attribution=attribution,
        model_size=model_size,
        speech_rate=speech_rate,
        strip_notes=strip_notes.lower() in ("1", "true", "yes", "on"),
        hr_as_stars=hr_as_stars.lower() in ("1", "true", "yes", "on"),
        narrator_voice=narrator_voice,
        character_voices=cast,
        mix_engines=mix_engines,
        tts_providers=only_providers,
    )
    JOBS.add(job)
    logger.info(
        "audiobook job %s: %s; narrator %s; characters from %s; speed %+d%%; attribution %s",
        job.id, url, narrator_voice or "the default", character_voices, speech_rate, attribution,
    )
    return JSONResponse({"id": job.id})


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    found = _find_job(job_id)
    if not found:
        raise HTTPException(404, "unknown job")
    registry, job = found
    return {
        "id": job.id,
        "format": job.format,
        "status": job.status,
        # How many jobs of its kind have to finish or start before this one. Zero unless it is waiting.
        "ahead": registry.jobs_ahead(job.id),
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
    found = _find_job(job_id)
    if not found:
        raise HTTPException(404, "unknown job")
    _registry, job = found
    if job.status != "done" or not job.path:
        raise HTTPException(409, f"job status is {job.status}")
    p = Path(job.path)
    if job.format in TEXT_FORMATS:
        if not p.is_file():
            raise HTTPException(410, "this file is no longer on the server; download the story again")
        return FileResponse(p, media_type=web_jobs.TEXT_MEDIA_TYPES[job.format], filename=p.name)
    if not p.is_file():
        raise HTTPException(410, "this audiobook is no longer on the server; make it again")
    return FileResponse(p, media_type="audio/mp4", filename=p.name)
