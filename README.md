# Ficary

Cross-platform fanfiction and original-fiction downloader with a
built-in accessible reader. Exports as EPUB, HTML, plain text, or a
chaptered M4B audiobook.

**Fanfic / original-fiction sites**
FanFiction.net · Archive of Our Own · FicWad · Royal Road ·
MediaMiner · Wattpad · Webnovel

**Erotica sites**
Literotica · Adult-FanFiction.org (AFF) · StoriesOnline · Nifty ·
SexStories · MCStories · Lushstories · Fictionmania · TGStorytime ·
Chyoa (interactive) · Dark Wanderer · GreatFeet · BDSM Library

Every site supports direct-URL download. FFN, AO3, Royal Road, and
Wattpad have dedicated search windows in the GUI; the erotica sites
share a unified "Erotic Story Search" that fans out a query across
all thirteen of them in parallel and collapses results per site.
(FicWad, MediaMiner, and Webnovel are URL-download only — no search.)

Accessible by design — the desktop GUI uses native widgets on every
platform (wxPython wraps Win32 on Windows, Cocoa on macOS, GTK3 on
Linux), so NVDA, JAWS, VoiceOver, and Orca read it the same way they
read any app on those platforms. The CLI is plain text with no
interactive TUI gotchas, usable from any screen-readable terminal.

## Install

### Windows (recommended)

Download the latest `ficary-portable.zip` from the
[Releases page](https://github.com/matalvernaz/ficary/releases). It's a
self-contained folder with its own Python, ffmpeg, and ffprobe bundled —
no dependencies to install. The app auto-updates from GitHub when a new
release is published.

### macOS (Apple Silicon)

Download `ficary-macos-arm64.tar.gz` from the Releases page, extract, and
run `./ficary/ficary`. The binary is unsigned, so the first launch needs
right-click → Open (or **System Settings → Privacy & Security → Open
Anyway**) to clear Gatekeeper.

### Linux (x86_64)

Download `ficary-linux-x86_64.tar.gz` from the Releases page, extract,
and run `./ficary/ficary`. Built against GTK3 — any modern desktop Linux
(Ubuntu 22.04+, Fedora 38+, Debian 12+) has the runtime libraries
already installed.

### pip (any platform, dev)

```bash
pip install "ficary[all] @ git+https://github.com/matalvernaz/ficary"
```

Extras are split so you only pull what you need:

| Extra       | Adds                          |
|-------------|-------------------------------|
| `epub`      | EPUB export (`ebooklib`)      |
| `audio`     | Audiobook synthesis via edge-tts — requires ffmpeg on PATH (Piper TTS is installed on demand from the GUI / `--install-piper`; the LLM attribution backend uses the stdlib and needs no extra) |
| `gui`       | wxPython desktop GUI          |
| `clipboard` | Clipboard-watch mode          |
| `playback`  | In-app reader audio — app-voice TTS + soundscapes (PyOpenAL) |
| `cf-solve`  | Playwright-backed Cloudflare-challenge fallback (also needs `playwright install chromium`) |
| `all`       | All of the above *except* `cf-solve` (opt-in due to ~400MB browser binary) |

The desktop binaries (Windows / macOS / Linux) ship with every extra
except `cf-solve` already included. Install `cf-solve` from **Edit →
Optional Features...** if you need it.

## Using it

### GUI

`ficary` with no arguments launches the GUI — that's what
double-clicking the desktop binary does. From a pip install:

```bash
ficary                    # no args → GUI
python -m ficary.gui      # explicit GUI launch
```

The main window is a download form. Search windows open from the
**Search** menu:

- **FFN** (Ctrl+1) — full filter set: genre, rating, language, word
  count, status, world, up to four characters, pairing, exclusions
- **AO3** (Ctrl+2) — with series collapse when 2+ parts appear
- **Royal Road** (Ctrl+3) — query-based search plus list browse for
  Rising Stars / Best Rated / Complete / Weekly Popular
- **Wattpad** (Ctrl+4)
- **Erotic Story Search** (Ctrl+5) — unified fan-out across all
  thirteen erotica sites (Literotica, AFF, StoriesOnline, Nifty,
  SexStories, MCStories, Lushstories, Fictionmania, TGStorytime,
  Chyoa, Dark Wanderer, GreatFeet, BDSM Library) with a per-site
  scope dropdown for when you already know where you want to search

The **Library** menu has scan / reorganize / update / abandoned
management; **Ctrl+U** checks the whole library for story updates
from anywhere (**Ctrl+Shift+U** checks for a new ficary release).
**Reader → Open reader** (Ctrl+R) opens a downloaded story in the
built-in reader — see [In-app reader](#in-app-reader). **Watchlist**
lets you follow stories, authors, or searches and get a Pushover /
Discord / email ping — or an automatic download — when a tracked
story updates. **Edit → Optional Features...** installs the extras
(EPUB, audio, clipboard, playback, cf-solve) at runtime on any
build — the frozen desktop binaries pip-install into a portable
`deps/` folder so "delete the folder" actually uninstalls.
Multi-select pickers and result lists mirror their check /
selection state into the row label so every screen reader speaks
it reliably.

### CLI — common tasks

```bash
# Single story (URL or ID). URLs for any of the supported sites
# work — the scraper is auto-selected from the URL.
ficary https://www.fanfiction.net/s/12345
ficary 12345
ficary https://www.literotica.com/s/example-story
ficary https://storiesonline.net/s/12345

# Batch from a text file (one URL per line, mixed sites allowed)
ficary -b urls.txt

# Pick format
ficary -f html  https://archiveofourown.org/works/1234
ficary -f audio https://www.royalroad.com/fiction/26727   # needs ffmpeg

# FFN via FicHub's shared cache — one request for the whole fic
# instead of ~6s/chapter. First-time downloads only; falls back to
# a direct scrape on any miss.
ficary --fichub https://www.fanfiction.net/s/12345

# Logged-in downloads: AO3 restricted works / private bookmarks /
# marked-for-later, Webnovel chapters your account has unlocked.
# Both flags take a browser "Cookie:" header string and fall back
# to $FICARY_AO3_COOKIE / $FICARY_WEBNOVEL_COOKIE.
ficary --ao3-cookie "$COOKIE" https://archiveofourown.org/works/1234
ficary --webnovel-cookie "$COOKIE" https://www.webnovel.com/book/...

# All of an author's stories — erotica author pages work too
ficary -a https://www.fanfiction.net/u/1234/Name
ficary -a https://archiveofourown.org/users/Name/works
ficary -a https://storiesonline.net/a/AuthorName

# List-page tools: print every fic URL a list page contains (author
# profile, AO3 series/tag/search, FFN community, Wattpad reading
# list) as TSV, or download them all
ficary --extract https://archiveofourown.org/tags/Time%20Travel/works
ficary --bulk https://archiveofourown.org/tags/Time%20Travel/works --max-results 50

# AO3 series merged into a single file
ficary --merge-series https://archiveofourown.org/series/1234

# Search
ficary -s "time travel" --site ffn  --sort favorites
ficary -s "dungeon"      --site royalroad --rr-tags progression,magic
ficary --rr-list "rising stars"   # list browse — no query needed
ficary -s "werewolf"     --site literotica
# (fan-out search across every erotica site is GUI-only — open the
# Erotic Story Search window from the GUI search menu)

# Update an existing export with new chapters
ficary -u "Path/To/Story.epub"

# Update a whole library folder (unchanged fics cost one HTTP
# probe; completed stories are skipped by default —
# --no-skip-complete probes them too)
ficary -U ~/Fanfic --recursive

# Partial downloads
ficary --chapters 1-5,10,50- https://...      # flexible ranges

# Send an EPUB to Kindle after download
ficary --send-to-kindle you@kindle.com https://...

# Upload a finished audiobook render to an Audiobookshelf server
# (configure ABS_URL / ABS_TOKEN in the environment, or
# Preferences → Audiobookshelf in the GUI)
ficary --abs-list-libraries            # find library / folder ids
ficary -f audio --send-to-abs https://...

# Watchlist: follow a story, author, or saved search and get a
# Pushover / Discord / email ping when it changes. Add
# --watchlist-auto-download to fetch updates automatically —
# tracked stories update in place in your library.
ficary --watchlist-add https://www.fanfiction.net/u/1234/Name
ficary --watchlist-add-search ao3 "time travel" --watchlist-label "TT watch"
ficary --watchlist-add https://... --watchlist-auto-download
ficary --watchlist-run                 # poll once — cron friendly
```

`ficary --help` has the full list.

## In-app reader

Ficary can open a downloaded story and read it, not just fetch it —
**Reader → Open reader** (Ctrl+R). Chapters come straight from the
scraper's chapter cache when present, or are re-parsed from the
exported EPUB/HTML file.

- **Screen-reader-native reading.** The chapter opens in an
  accessible read-only view your own screen reader reads with
  say-all. Chapter list, next / previous / jump, adjustable font,
  and light / dark / high-contrast themes.
- **Reading position and bookmarks** are saved per story and
  restored when you reopen it.
- **App-voice reading (optional).** Switch reading mode to App voice
  and ficary reads aloud with edge/Piper voices, following along
  with a highlight, auto-advancing into the next chapter at a
  natural chapter end.
- **Soundscapes (optional).** Assign an ambient audio bed to a story
  (build beds in Reader → Soundscape editor). The bed fades in while
  reading, ducks under the narration, and fades out when you leave;
  positional placement and reverb included.
- **Sleep timer.** Stop reading after 5–120 minutes; a shortcut
  reports the time left.

App-voice and soundscapes need the `playback` extra — bundled in the
desktop binaries; elsewhere `pip install "ficary[playback]"` or
**Edit → Optional Features...**. Without it, screen-reader reading
and audiobook export are unaffected.

## Library management

Once you've scanned a directory of downloaded stories, ficary tracks
them in a library index and layers several tools on top.

```bash
# One-time scan of a directory — identifies every story, records
# metadata, bootstraps the library index.
ficary --scan-library ~/Fanfic

# During scan: auto-mark WIPs (status != Complete) whose file
# hasn't been touched in DAYS days as abandoned, so subsequent
# --update-library runs skip them. Reads the
# library_abandoned_after_days user pref by default; pass
# --abandoned-after-days N to override, or 0 to disable.
ficary --scan-library ~/Fanfic --abandoned-after-days 730

# Review the abandoned list (scope with --library-dir)
ficary --list-abandoned

# Revive one URL (the author posted again!) or all at once
ficary --revive-abandoned https://www.fanfiction.net/s/12345
ficary --revive-abandoned          # no URL = revive every marked story

# Search by metadata (title / author / fandom / URL substring)
ficary --library-find "time travel"

# Full-text search across every indexed chapter body. Uses SQLite
# FTS5 syntax: prefix wildcards (dragon*), NEAR(a b), and boolean
# operators (AND / OR / NOT) all work. Bootstrap is a one-time
# --populate-search DIR; subsequent --update-library runs keep the
# index warm. Stories downloaded via direct URL (not the library
# update path) land in the text index on the next --populate-search.
ficary --populate-search ~/Fanfic
ficary --library-search "orphanage scene"

# Detect suspected cross-site mirror pairs (same story on FFN and
# AO3, Literotica and StoriesOnline, etc.). Needs >=2 corroborating
# signals (normalised title match, author match, first-chapter word
# overlap) to flag a pair, so common titles don't produce false
# positives. Read-only; never deletes.
ficary --find-mirrors ~/Fanfic

# Hygiene: library doctor, watchlist doctor, cache doctor, or all
# three at once. --heal applies only the SAFE fixes; anything that
# deletes data is a separate opt-in (--heal-drop-missing,
# --heal-prune-stale, --heal-drop-watches, or --heal-all for
# everything). Destructive heals snapshot the index/watchlist first
# and record a manifest; cache prunes quarantine into .trash/ for
# 14 days instead of deleting.
ficary --doctor
ficary --doctor --heal
ficary --doctor --heal-all

# Undo the most recent destructive heal in one command: restores
# the snapshots named in the newest heal manifest and moves
# quarantined cache entries back.
ficary --doctor-restore-last

# Per-chapter silent-edit detection. Hash-based, so an author's
# in-place typo fix shows up even though the chapter count didn't
# change.
ficary --populate-hashes ~/Fanfic    # one-time bootstrap
ficary --scan-edits ~/Fanfic         # drift report
```

### Auto-sort and the Original Works folder

When you configure a library path in preferences, new downloads are
sorted into fandom subfolders automatically. The auto-sorter
recognises each site's category format: FFN's `Books > Harry Potter`
breadcrumbs get their leading meta-category stripped, AO3's
`Harry Potter / Naruto` crossover joins get split so multi-fandom
routes to the misc bucket, and plain single-fandom strings pass
through untouched. Royal Road is treated as an original-fiction
source — RR downloads land in `Original Works/` rather than `Misc/`,
so your library surfaces original novels as a dedicated subtree
alongside the fandom folders.

Upgrading from a 1.x install with an existing library?
2.0.0 changes the auto-sort layout for FFN and Royal Road downloads —
run `ficary --reorganize ~/Fanfic --apply` to migrate your existing
files to match the new layout. The dry-run (without `--apply`) prints
the proposed moves first.

## What it handles automatically

- **Rate limiting**: adaptive (AIMD) inter-chapter delay — starts fast,
  backs off on 429/503, decays back down on clean responses. FFN holds
  a steady 6s/chapter because its bulk-fetch captcha bans faster
  crawls; `--fichub` sidesteps that for first-time FFN downloads by
  re-ingesting FicHub's cached EPUB in a single request. `--delay-min`
  / `--delay-max` override with a fixed range if you want the old
  behaviour.
- **Parallel chapter fetches** on Royal Road, FicWad, and MediaMiner
  (default 3 workers, same AIMD feedback halves concurrency on
  rate-limit responses). FFN stays sequential.
- **Cloudflare impersonation** via `curl_cffi` (Chrome, Edge, Safari).
  Stubborn 403s can opt into `--cf-solve`, which launches a headless
  Chromium via Playwright, lets the challenge resolve, and injects
  the solved cookies into the scraper session. Solved cookies are
  cached under `~/.cache/ficary/cf-cookies/` (chmod 0600) for 24
  hours so later runs reuse them without re-launching the browser.
- **Per-chapter caching** in `~/.cache/ficary`, so interrupted downloads
  resume cheaply and update-mode only fetches what actually changed.
- **Cover image cache** at 7-day TTL so re-exporting a long series
  doesn't re-download the same cover per part.
- **Wayback fallback** (`--use-wayback`): when the live site 404s, try
  the most recent archive.org snapshot before giving up.
- **Series handling**: AO3 series collapse in search results when 2+
  parts appear; Literotica chapters (`Ch. NN` / `Pt. NN` / `- N` / `PN`)
  collapse per author + URL stem, and downloading the collapsed row
  resolves the canonical `/series/se/<id>` to pull chapters that
  didn't match the search.
- **Royal Road stubbed fictions**: the misleading bare `Stub` status is
  replaced with `Complete (Stubbed)` / `In-Progress (Stubbed)` /
  `Stubbed` depending on what RR exposes.

## Library-update performance knobs

Large libraries (thousands of fics) benefit from three gates on
`--update-library`:

- **`--recheck-interval SECONDS`** — skip stories whose index
  `last_probed` timestamp is within SECONDS of now. A value like
  `3600` makes a second `--update-library` minutes after the first
  near-instant.
- **`--skip-stale-complete DAYS`** — skip stories that are both
  marked Complete and whose file mtime is at least DAYS old. Gentler
  than `--skip-complete`: a fic completed yesterday still gets
  probed (the author may add an epilogue), but one untouched for a
  year stops costing an HTTP probe each run.
- **Abandoned WIPs get skipped automatically.** Any story carrying
  an `abandoned_at` timestamp in the index is dropped from the
  probe queue — the mark is set by `--scan-library` when
  `library_abandoned_after_days` is configured (or
  `--abandoned-after-days N` is passed explicitly) and the story's
  file has been untouched that long without being Complete. The
  mark is sticky until revived with `--revive-abandoned URL` (or
  all at once via the same flag with no argument). The Library
  dialog in the GUI exposes both the threshold setting and a
  "Manage abandoned..." review list so screen-reader users can
  walk the list and revive without touching the CLI.

`--recheck-interval` and `--skip-stale-complete` are overridden by
`--force-recheck`. Abandoned entries stay skipped — once you've
declared a WIP dead, a forced recheck doesn't automatically bring
it back; use `--revive-abandoned` to undo the mark.

## Audiobook notes

`-f audio` synthesises each chapter through one or more pluggable
TTS providers and concatenates into a chaptered M4B with embedded
cover art. Needs `ffmpeg` and `ffprobe` on PATH for the pip install;
they're bundled in the Windows / macOS / Linux binaries. A finished
render can upload straight into an Audiobookshelf server
(`--send-to-abs` on the CLI, Preferences → Audiobookshelf in the
GUI).

### TTS providers

Two providers ship in-tree, and the audiobook generator pulls voices
from the union of every enabled one. Pick which contribute via
`--tts-providers <names>` on the CLI or the "TTS providers..."
button in the GUI's audio toolbar.

- **edge** — [edge-tts](https://github.com/rany2/edge-tts), Microsoft's
  Edge Neural Voices. Cloud TTS, no API key, broad coverage of
  English locales (US/UK/Australian/Canadian/Indian/Irish/NZ) plus
  every major language. The historical default; every pre-2.2.0
  voice map continues to resolve.
- **piper** — local [Piper TTS](https://github.com/rhasspy/piper)
  via ONNX inference. Runs offline once installed. Ships a curated
  voice manifest that downloads on first use, covering English
  regional accents (UK / Scottish / Irish / Welsh / Australian /
  Indian) plus French / Spanish / German / Italian / Russian /
  Japanese / Polish / Portuguese / Dutch / Swedish. Install the
  binary with `--install-piper` or the GUI's "Install Piper binary"
  button; voice ONNX files lazy-download on first use.

Voice ids are namespaced as `provider:short_name`
(`edge:en-US-AvaNeural`, `piper:en_GB-alan-medium`). Per-story
voice maps live at `<output_dir>/.ficary-voices-<id>.json` and are
user-editable.

### Speaker attribution

Four backends, picked via `--attribution`:

- **builtin** — regex-based dialogue parser. No dependencies.
  Default.
- **fastcoref** — neural coreference refinement (~90 MB).
  `pip install fastcoref` or click Install in the GUI.
- **booknlp** — full [BookNLP](https://github.com/booknlp/booknlp)
  quote + coref attribution (~150 MB small / ~1 GB big).
- **llm** — sends each chapter to a Large Language Model. Use a
  local Ollama instance (no key, offline) or a remote provider
  (OpenAI / Anthropic / OpenAI-compatible like Groq / OpenRouter /
  vLLM). LLaMa-3 evaluations on the Project Dialogism Novel Corpus
  put well-prompted LLMs above BookNLP-big on quotation accuracy.

CLI flags for the LLM backend: `--llm-provider`, `--llm-model`,
`--llm-api-key`, `--llm-endpoint` — falls back through env vars
(`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY`)
then the GUI prefs. The GUI exposes the same settings via "LLM
settings..." in the audio toolbar.

### LLM-driven enrichment

When the LLM attribution backend is enabled, the audiobook generator
runs five additional analysis passes per story to produce a richer
audiobook than the heuristic pipeline alone:

1. **Per-quote emotion** classification (bundled into the same
   per-chapter call as attribution): `whisper`, `shout`, `excited`,
   `cheerful`, `sad`, `angry`. Maps to edge-tts prosody adjustments.
2. **Character profiles** (`gender` / `age` / `accent` / `tone`)
   saved to `.ficary-profile-<id>.json`. Feeds VoiceMapper as a richer
   prior than gender alone.
3. **Per-character accent map** seeded into `.ficary-accents-<id>.json`
   from the profiles. The VoiceMapper builds each character's voice
   pool with a three-tier preference (exact locale > language >
   any), so Hagrid lands on a UK voice instead of round-robining to
   en-US.
4. **Pronunciation map** (`.ficary-pronunciations-<id>.json`):
   pre-filled with phonetic respellings of made-up names, fandom
   terms, foreign loanwords, hard-to-pronounce place names.
5. **Narrator voice suggestion**: the LLM reads the story's tone and
   recommends `gender` + `accent`, which the generator translates
   into a real catalog voice. Caller-supplied `narrator_voice`
   overrides.

Every map file is user-editable; edits survive re-renders. Every
LLM enrichment is purely additive — transport failures fall through
silently, and existing user-edited JSON is never clobbered.

Separate from attribution, `--llm-strip-notes` (paired with
`--strip-notes`) sends each paragraph the regex pre-pass kept
through the configured LLM for a second-pass author's-note
decision — catching disguised outros, beta thanks, and shout-outs
that don't trip the keyword gate. It works with any attribution
backend and every output format (not just audio), costs one LLM
call per chapter, and caches results per story so re-exports don't
re-spend.

The per-chapter attribution result is cached at
`<portable_root>/cache/attribution/v1/<backend>/<size>/<sha>.json`
keyed by chapter text, so re-running after a partial failure or
adding one new chapter doesn't repeat the LLM cost on the rest.

## Accessibility

ficary is built and tested with screen-reader users as a first-class
audience. Concretely:

- **Windows**: GUI tested with NVDA. Multi-select pickers, search
  result rows, and watchlist entries mirror their check/selection
  state into the visible label text so MSAA-fragile controls still
  read correctly.
- **In-app reader**: the reading view is a native read-only text
  control, so say-all and per-line/word/character navigation work
  exactly as they do in any standard edit field — no custom widget
  in the way.
- **macOS**: wxPython wraps native Cocoa widgets; VoiceOver reads
  the GUI using the same AXUIElement tree it reads in Safari or
  Mail.
- **Linux**: wxPython wraps GTK3 widgets; Orca reads the GUI via
  at-spi2 (the system accessibility bus). Any distro that installs
  GNOME or KDE has at-spi2 active by default.
- **CLI on every platform**: plain text, one line per decision, no
  animated progress bars or cursor manipulation. Works in any
  terminal a screen reader can read (Windows Terminal, macOS
  Terminal + VoiceOver, any Linux terminal with Orca or BRLTTY).

## Development

```bash
git clone https://github.com/matalvernaz/ficary
cd ficary
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest
```

Tests run offline against static HTML fixtures and don't hit the network.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
