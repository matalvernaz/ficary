# Ficary

Ficary downloads stories from fanfiction and fiction sites and turns
them into books you can keep — EPUB, HTML, plain text, or a chaptered
M4B audiobook. It can also read stories aloud itself, keep a whole
library of downloads up to date, and watch your favourite stories and
authors for new chapters.

It is built for screen-reader users first. The desktop app uses each
platform's native controls, so NVDA, JAWS, VoiceOver, and Orca read it
the same way they read any other app, and everything works from the
keyboard.

**Contents:**
[Sites](#what-you-can-download-from) ·
[Installing](#installing) ·
[Getting started](#getting-started) ·
[Searching](#searching-for-stories) ·
[Reading in the app](#reading-in-the-app) ·
[Your library](#your-library) ·
[Audiobooks](#audiobooks) ·
[Watching for new chapters](#watching-for-new-chapters) ·
[Command line](#using-the-command-line) ·
[Good to know](#good-to-know) ·
[Accessibility](#accessibility) ·
[For developers](#for-developers)

## What you can download from

**Fanfiction and original fiction:**
FanFiction.net · Archive of Our Own · FicWad · Royal Road ·
MediaMiner · Wattpad · Webnovel

**Erotica archives and story forums:**
Literotica · Adult-FanFiction.org · StoriesOnline · Nifty ·
SexStories · MCStories · Lushstories · Fictionmania · TGStorytime ·
Chyoa (interactive) · Dark Wanderer · GreatFeet · ReadOnlyMind ·
Giantess World · The Mousepad · Chastity Mansion · TicklingForum ·
BDSM Library

Paste a story link from any of these and Ficary works out the rest —
you never have to tell it which site a link belongs to. Most sites
can also be searched from inside the app; see
[Searching for stories](#searching-for-stories). (FicWad, MediaMiner,
and Webnovel are download-by-link only — no search.)

One caveat: BDSM Library's own story database has been broken at the
source since July 2026 — the site serves blank pages for every story.
Ficary reports this clearly instead of saving an empty file, and will
simply start working again if the site recovers.

## Installing

### Windows (recommended)

Download `ficary-portable.zip` from the
[Releases page](https://github.com/matalvernaz/ficary/releases) and
unzip it anywhere you like. That's the whole install — the folder
contains everything Ficary needs, including its own Python and audio
tools. Run `ficary.exe` to start. When a new version comes out, the
app offers to update itself.

### macOS (Apple Silicon)

Download `ficary-macos-arm64.tar.gz` from the Releases page, extract
it, and run `./ficary/ficary`. The app isn't signed with an Apple
developer certificate, so the first launch needs right-click → Open
(or **System Settings → Privacy & Security → Open Anyway**). After
that it opens normally.

### Linux (x86_64)

Download `ficary-linux-x86_64.tar.gz` from the Releases page, extract
it, and run `./ficary/ficary`. Any recent desktop distribution
(Ubuntu 22.04+, Fedora 38+, Debian 12+) already has the system
libraries it needs.

### For Python users (pip)

```bash
pip install "ficary[all] @ git+https://github.com/matalvernaz/ficary"
```

`[all]` installs every feature except the optional Cloudflare solver.
If you'd rather install only what you need, the pieces are:

- `epub` — EPUB export
- `audio` — audiobook creation (also needs `ffmpeg` on your PATH)
- `gui` — the desktop app
- `clipboard` — clipboard-watch mode
- `playback` — in-app read-aloud voices and soundscapes
- `cf-solve` — a browser-based fallback for the most stubborn
  Cloudflare-protected sites. It's a ~400 MB download (it includes a
  headless Chromium via `playwright install chromium`), which is why
  `all` leaves it out.

### Optional features

The desktop downloads come with every feature already included except
the Cloudflare solver. If you ever need a missing piece, open
**Edit → Optional Features...** in the app and install it from
there — no command line required. Features installed this way live
inside Ficary's own folder, so deleting the folder still deletes
everything.

## Getting started

Start Ficary — double-click the app, or run `ficary` with no
arguments. The main window is a download form:

1. Paste a story link into the URL box.
2. Pick an output format — EPUB, HTML, plain text, or audiobook.
3. Press **Download**.

The finished file lands in your output folder. If you've set a
library folder in Preferences, downloads are sorted into fandom
subfolders automatically (see [Your library](#your-library)).

A quick tour of the menus:

- **File** — update a single downloaded file with new chapters
  (Ctrl+Shift+F), re-download one from scratch (Ctrl+Shift+R), or
  download a whole list of links from a text file (Ctrl+Shift+L).
- **Edit** — Preferences (Ctrl+,) and Optional Features.
- **Search** — find stories without leaving the app; see the next
  section.
- **Library** — the Library window (Ctrl+L), a browsable list of
  everything you've downloaded (Ctrl+B), and a one-keystroke "check
  everything for new chapters" (Ctrl+U).
- **Reader** — read a downloaded story inside the app (Ctrl+R), and
  build ambient soundscapes for it.
- **Watchlist** — follow stories, authors, or searches and get told
  when something new appears (Ctrl+W).
- **Help** — this manual (F1) and Check for App Updates
  (Ctrl+Shift+U), which shows everything that changed since your
  version.

## Searching for stories

Each search window opens from the **Search** menu:

- **FFN** (Ctrl+1) — the full FanFiction.net filter set: genre,
  rating, language, word count, status, world, up to four characters,
  pairing, and exclusions.
- **AO3** (Ctrl+2) — Archive of Our Own search; when a result is part
  of a series with two or more parts, the parts collapse into one row
  so you can grab the whole series at once.
- **Royal Road** (Ctrl+3) — search by query, or browse the site's own
  lists: Rising Stars, Best Rated, Complete, Weekly Popular.
- **Wattpad** (Ctrl+4).
- **Erotic Story Search** (Ctrl+5) — one search across twenty sites
  at once; see below.

Results appear in a list you can arrow through; press Enter or the
Download button on a row to fetch it.

### The Erotic Story Search

One window searches every erotica site in parallel — the eighteen
dedicated archives and forums, plus AO3 (explicit works only) and
Wattpad's mature section — and brings the results together in a
single list. You can:

- **Search by words, tags, or both.** The Tags box browses by
  category on the sites that support it (for example
  `bdsm, hypnosis`).
- **Scope to one site** with the site dropdown when you already know
  where you want to look.
- **Browse with no query at all.** Pick a site, press Search, and you
  get that site's natural listing — its newest or most popular
  stories. Works for every site except Adult-FanFiction.org, which
  has no site-wide listing (fill in a fandom there).
- **Sort by newest.** The "Sort by" dropdown switches from
  grouped-by-site order to newest-first, using the "Updated" date
  shown on each row.
- **Load More** keeps walking each site's results as far as they go.

Rows show the story's site, title, author, summary, word count, and
last-update date wherever the site publishes them.

## Reading in the app

Ficary doesn't just fetch stories — it can read them. Open a
downloaded story with **Reader → Open reader** (Ctrl+R).

- **Read with your own screen reader.** The chapter text sits in a
  plain read-only view, so say-all and line/word/character navigation
  work exactly as they do in any standard text field. Chapter list,
  next / previous / jump, adjustable font, and light / dark /
  high-contrast themes.
- **Your place is saved.** Reading position and bookmarks are stored
  per story and restored when you come back.
- **Or let the app read aloud.** Switch the reading mode to App voice
  and Ficary reads with Edge or Piper voices, highlighting the text
  as it goes and flowing into the next chapter automatically.
- **Soundscapes.** Give a story an ambient audio bed — rain, tavern,
  engine hum — built in the Soundscape editor (Reader menu). The bed
  fades in while you read, ducks under the narration, and fades out
  when you leave.
- **Sleep timer.** Stop reading after 5 to 120 minutes; a shortcut
  tells you how long is left.

App-voice reading and soundscapes need the `playback` feature —
already included in the desktop downloads, or one click away in
**Edit → Optional Features...**. Without it, reading with your own
screen reader and audiobook export work exactly the same.

## Your library

Point Ficary at a folder of downloaded stories and it becomes a
library: every story is indexed with its title, author, fandom, and
source, and a set of tools builds on top of that. Set your library
folder in Preferences or the Library window (Ctrl+L), then scan it
once (the window's Scan button, or `ficary --scan-library ~/Fanfic`).

### Browsing

**Library → Browse Library** (Ctrl+B) lists everything the scan
found, with a live search box. Arrow through the list and the details
pane reads out title, author, fandom, format, file path, and source
link. From there you can open a story in the reader (Enter), check it
for updates, re-export it to another format, copy its path, or delete
it.

### Automatic sorting

With a library folder set, new downloads file themselves into fandom
subfolders. Ficary understands each site's own category labels —
FanFiction.net's `Books > Harry Potter` style, AO3's crossover
listings (multi-fandom stories go to the misc folder), and plain
fandom names. Royal Road counts as original fiction, so its downloads
land in an `Original Works/` folder rather than being mixed in with
fandoms.

### A separate adult library

Adult-site downloads can go to a completely separate folder — a
different drive, an unsynced or encrypted location, wherever. Set
"Adult library folder" in the Library window. The library browser
keeps adult titles hidden until you tick "Show adult", and Scan
Library covers both folders in one pass.

### Keeping stories up to date

- **Check everything at once:** Ctrl+U in the app, or
  `ficary -U ~/Fanfic --recursive` on the command line. Stories that
  haven't changed cost one quick check each; finished stories are
  skipped by default.
- **Update one file:** File → Update File (Ctrl+Shift+F), or
  `ficary -u "Path/To/Story.epub"`.
- **Give up on dead WIPs.** Ficary can mark unfinished stories that
  haven't changed in a long time (two years, say) as abandoned, so
  update runs stop wasting time on them. If the author comes back,
  revive the story and it's checked again. The Library window has a
  "Manage abandoned..." list for reviewing and reviving; the same
  works from the command line (`--list-abandoned`,
  `--revive-abandoned`).
- **Large libraries** have extra speed levers — see
  [Using the command line](#using-the-command-line).

### Finding things

- **Search your library's details** — title, author, fandom, or
  link: `ficary --library-find "time travel"`.
- **Search the full text of every chapter you've downloaded:** run
  `ficary --populate-search ~/Fanfic` once to build the text index,
  then `ficary --library-search "orphanage scene"`. Wildcards
  (`dragon*`) and AND / OR / NOT all work. Library update runs keep
  the index fresh from then on.
- **Find duplicate copies of the same story from different sites**
  (the same fic on FanFiction.net and AO3, say):
  `ficary --find-mirrors ~/Fanfic`. It only flags a pair when at
  least two signals agree — matching title, matching author,
  overlapping first chapter — so common titles don't cause false
  alarms. It reports; it never deletes anything.
- **Spot silent edits.** Authors sometimes fix typos or rewrite
  scenes without adding a chapter. `ficary --populate-hashes ~/Fanfic`
  once, then `ficary --scan-edits ~/Fanfic` any time for a report of
  chapters whose text changed under you.

### Housekeeping

`ficary --doctor` checks the library index, the watchlist, and the
download cache for problems and explains what it finds. Add `--heal`
to apply the safe fixes. Anything that would delete data is a
separate, explicit opt-in (`--heal-drop-missing`,
`--heal-prune-stale`, `--heal-drop-watches`, or `--heal-all`), and
every destructive heal saves a snapshot first —
`ficary --doctor-restore-last` undoes the most recent one in a single
command.

Upgrading from a 1.x install? Version 2.0 changed where
FanFiction.net and Royal Road downloads are filed.
`ficary --reorganize ~/Fanfic` prints the moves it would make;
add `--apply` to do them.

## Audiobooks

Pick the audiobook format (or `-f audio` on the command line) and
Ficary reads each chapter with text-to-speech voices and assembles a
proper M4B audiobook — chapter markers, embedded cover art, resumable
in any audiobook player. A finished audiobook can upload straight to
an Audiobookshelf server (Preferences → Audiobookshelf, or
`--send-to-abs`).

The desktop downloads include everything audiobooks need. A pip
install needs `ffmpeg` and `ffprobe` on the PATH.

### Voices

Two voice providers are built in, and you can use either or both:

- **edge** — Microsoft's Edge neural voices. Online, free, no
  account or key, with broad coverage of English accents (US, UK,
  Australian, Canadian, Indian, Irish, NZ) plus every major language.
  This is the default.
- **piper** — fully offline voices that run on your own machine.
  Install the Piper engine with one click in the audio toolbar (or
  `--install-piper`); each voice downloads the first time it speaks.
  Covers UK, Scottish, Irish, Welsh, Australian, and Indian English
  plus French, Spanish, German, Italian, Russian, Japanese, Polish,
  Portuguese, Dutch, and Swedish.

Voice names carry their provider as a prefix —
`edge:en-US-AvaNeural`, `piper:en_GB-alan-medium`. Each story
remembers its voice casting in a small file next to the audiobook
(`.ficary-voices-<id>.json`), which you can edit by hand; your
choices survive re-renders.

### Giving characters their own voices

Ficary can work out who's speaking each line of dialogue and give
each character a distinct voice. Four ways to do it, from lightest to
smartest — pick with `--attribution` or in the GUI's audio toolbar:

- **builtin** — pattern-based dialogue detection. No extra
  downloads. The default.
- **fastcoref** — a small neural model (~90 MB) that resolves more
  "he said / she said" correctly.
- **booknlp** — a heavyweight literary NLP model (~150 MB small,
  ~1 GB big).
- **llm** — sends each chapter to a large language model, which
  reads the scene and decides who's talking. Point it at a local
  Ollama (free, offline) or a remote provider — OpenAI, Anthropic, or
  anything OpenAI-compatible such as Groq, OpenRouter, or vLLM. In
  published evaluations, well-prompted LLMs beat BookNLP's best model
  at this. Set the provider, model, and key under "LLM settings..."
  in the audio toolbar (or `--llm-provider`, `--llm-model`,
  `--llm-api-key`, `--llm-endpoint`).

With the LLM backend on, Ficary goes further than naming speakers.
It reads the story and builds, per character: an emotion cue for each
quote (whispers whisper, shouts shout), a profile (gender, age,
accent, tone) used to cast a fitting voice, an accent map so a
Scottish character gets a Scottish voice instead of pot luck, and a
pronunciation guide for made-up names and fandom terms. It also
suggests a narrator voice to match the story's tone. Each of these
lives in a small editable file next to the audiobook, your hand edits
are never overwritten, and results are cached — re-rendering a story
or adding one new chapter doesn't re-spend LLM calls on the rest.

### Skipping author's notes

`--strip-notes` removes recognisable author's notes from any export
format. Add `--llm-strip-notes` to also catch the disguised ones —
outros, beta thanks, review shout-outs — using the same LLM settings
as above, one call per chapter, cached per story.

## Watching for new chapters

The watchlist follows things so you don't have to check:

- **a story** — tell me when it updates;
- **an author** — tell me when they post anything new;
- **a saved search** — tell me when a new story matches.

When something changes you get a notification — Pushover, Discord, or
email, set up in Preferences — or Ficary can just download the update
straight into your library (`--watchlist-auto-download`, or the
matching checkbox in the app).

Manage watches in the app with **Watchlist → Manage watchlist...**
(Ctrl+W), or from the command line:

```bash
ficary --watchlist-add https://www.fanfiction.net/u/1234/Name
ficary --watchlist-add-search ao3 "time travel" --watchlist-label "TT watch"
ficary --watchlist-add https://... --watchlist-auto-download
ficary --watchlist-run          # check everything once — cron friendly
```

## Using the command line

Everything the app does works from a terminal too. The most common
jobs:

```bash
# Download one story — paste any supported site's link, or a bare
# FanFiction.net story id.
ficary https://www.fanfiction.net/s/12345
ficary 12345
ficary https://www.literotica.com/s/example-story

# A whole list: one link per line, sites can be mixed freely.
ficary -b urls.txt

# Choose the output format.
ficary -f html  https://archiveofourown.org/works/1234
ficary -f audio https://www.royalroad.com/fiction/26727

# HTML with the classic flat title page instead of the modern one.
ficary --html-style classic https://...

# FanFiction.net, but fast: fetch the whole story in one request from
# FicHub's shared cache instead of ~6 seconds per chapter. First-time
# downloads only; automatically falls back to a normal download.
ficary --fichub https://www.fanfiction.net/s/12345

# Stories that need you logged in — AO3 restricted works, Webnovel
# chapters your account owns. Paste your browser's "Cookie:" header;
# the FICARY_AO3_COOKIE / FICARY_WEBNOVEL_COOKIE environment
# variables work too.
ficary --ao3-cookie "$COOKIE" https://archiveofourown.org/works/1234
ficary --webnovel-cookie "$COOKIE" https://www.webnovel.com/book/...

# Everything an author has written.
ficary -a https://www.fanfiction.net/u/1234/Name
ficary -a https://archiveofourown.org/users/Name/works
ficary -a https://storiesonline.net/a/AuthorName

# List pages — an author profile, an AO3 tag or series, an FFN
# community, a Wattpad reading list. Print every story link the page
# contains, or download them all.
ficary --extract https://archiveofourown.org/tags/Time%20Travel/works
ficary --bulk https://archiveofourown.org/tags/Time%20Travel/works --max-results 50

# An AO3 series merged into a single book.
ficary --merge-series https://archiveofourown.org/series/1234

# Search.
ficary -s "time travel" --site ffn --sort favorites
ficary -s "dungeon" --site royalroad --rr-tags progression,magic
ficary --rr-list "rising stars"          # browse a Royal Road list
ficary -s "werewolf" --site literotica

# The erotica search, same as the app's: all twenty sites at once,
# or scoped to one. Tags browse by category; no query needed.
ficary -s "cruise ship" --site erotica
ficary --site erotica --tags bdsm,hypnosis --sort date
ficary --site erotica --erotica-site readonlymind --sort date

# Update an existing file with new chapters.
ficary -u "Path/To/Story.epub"

# Update a whole library folder.
ficary -U ~/Fanfic --recursive

# Only some chapters.
ficary --chapters 1-5,10,50- https://...

# Email an EPUB to your Kindle when it's done.
ficary --send-to-kindle you@kindle.com https://...

# Send a finished audiobook to Audiobookshelf.
ficary --abs-list-libraries      # find your library and folder ids
ficary -f audio --send-to-abs https://...
```

`ficary --help` lists everything.

### Speed levers for big libraries

Three flags keep `-U` / `--update-library` fast when your library has
thousands of stories:

- `--recheck-interval 3600` — don't re-check a story checked within
  the last hour (any number of seconds works). Makes a second update
  run minutes after the first nearly instant.
- `--skip-stale-complete 365` — skip stories that are finished *and*
  untouched for that many days. Gentler than skipping all finished
  stories: a fic completed yesterday still gets checked in case of an
  epilogue.
- Abandoned stories are skipped automatically until you revive them
  (`--revive-abandoned URL`, or with no URL to revive everything).

`--force-recheck` overrides the first two when you want a full pass.
Abandoned stays abandoned until revived — a forced pass doesn't
un-abandon anything.

## Good to know

Things Ficary handles for you, so you know what's normal:

- **It paces itself.** Downloads start fast and back off automatically
  when a site pushes back, then speed up again. FanFiction.net is the
  exception — it bans fast crawlers, so Ficary holds a steady
  6 seconds per chapter there. For a long FFN fic, `--fichub` skips
  the wait entirely on first download.
- **Some sites download several chapters at once** (Royal Road,
  FicWad, MediaMiner) when they tolerate it.
- **Interrupted downloads resume.** Every fetched chapter is kept in
  a local cache, so a dropped connection or closed laptop doesn't
  start you over, and update checks only fetch what actually changed.
- **Cloudflare-protected sites usually just work** — Ficary presents
  itself like a normal browser. For the rare site that still blocks
  it, the optional `cf-solve` feature opens an invisible real browser
  once, passes the site's check, and remembers the result for a day.
- **Deleted story?** Add `--use-wayback` and Ficary tries the
  Internet Archive's most recent snapshot before giving up.
- **Series are understood.** AO3 series show as one row in search and
  can merge into one book. A multi-part Literotica story downloads as
  one book with real chapters — and Literotica's "Page 2 / Page 3"
  splits inside a single part are joined back together rather than
  pretending to be chapters.
- **Royal Road "stubs" are labelled honestly.** When an author has
  removed most chapters for a paid edition, the status says so
  (`Complete (Stubbed)` and similar) instead of a bare `Stub`.

## Accessibility

Ficary is built and tested with screen-reader users as a first-class
audience:

- **Windows** — tested with NVDA. Lists that can be multi-selected
  (search results, pickers, watchlist entries) repeat their checked
  state in the row's text, so the state is always spoken reliably.
- **The reader** is a native read-only text view — say-all and
  line/word/character navigation behave exactly as in any standard
  text field, with no custom widget in the way.
- **macOS** — native Cocoa controls; VoiceOver reads Ficary with the
  same commands as Safari or Mail.
- **Linux** — native GTK controls; Orca reads it through the standard
  accessibility bus, active by default on GNOME and KDE.
- **The command line** prints plain text, one line per event — no
  animated progress bars, no cursor tricks. Works in any terminal a
  screen reader can read.

## For developers

```bash
git clone https://github.com/matalvernaz/ficary
cd ficary
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest
```

Tests run offline against saved page fixtures — no network needed.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). In the app: **Help → Check for App
Updates** shows every change since the version you're running.
