# Desktop UX audit

Status: complete for assigned scope; findings and limitations saved 2026-09-08. Baseline `8f79e57399686357fcfead0e9fce29e70790beb1`. Audit only; no application files changed.

Scope: main desktop download workflows, search, watchlist GUI, Preferences, queue lifecycle, keyboard/accessibility contract, and documentation parity. Reader/audio delegated to audio reviewer; library browser and watch backend delegated to library reviewer. Evidence is relative to this directory.

## Verified defects

<a id="ux-01"></a>

### UX-01 — P1: Linux source/pip preferences cannot persist because their filename is the data directory

Confidence: confirmed with native wxPython 4.2.3 GTK and isolated on-disk reproduction. Root identified the collision; this review verified it.

`portable.py:127`–`141` selects `~/.ficary` as the non-frozen data directory and creates it. `prefs.py:343` instead opens `wx.Config("ficary")`; on this platform `wx.FileConfig.GetLocalFileName("ficary")` returns `/home/matt/.ficary`, a **file** path. `Prefs.set`/`set_bool` call `Flush()` at `prefs.py:358`/`371` but ignore its Boolean result.

Reproduction: create a temporary `.ficary` directory, redirect the native config factory to that path, and call the real `Prefs.set('audit_probe', 'synthetic-value')`. The setter returns normally, the same instance reads the value, `Flush()` returns `False`, and a second preferences instance reads `None`. wx reports `error 21: Is a directory` and leaves `.ficaryXXXXXX` temporary files beside it. No real user preferences were written.

Impact: library location, cookies, downloaded-format defaults and other preferences appear saved during the session and disappear at restart. Other modules that instantiate `Prefs()` independently can also miss settings during that same session. Repeated setters produce errors and temporary config artifacts.

Fix: explicitly use a file beneath the data directory (for example `portable.settings_file()`) on non-frozen Unix, preserve intentional platform migrations, and treat failed `Write`/`Flush` as a surfaced save failure. Test directory/file migration and new-instance reads on Linux; separately validate macOS's native path policy.

Evidence: `evidence/ux_preferences_storage_probe.py`, `.json`, `.stderr`. Native path was inspected read-only; failed saves occurred only under a temporary directory.

<a id="ux-02"></a>

### UX-02 — P1: closing a search window leaves the application permanently busy

Confidence: confirmed with native wx controls and a deterministic blocked fake search worker.

`gui_search.py:818` sets main-frame global busy. `_on_close` sets `_alive=False` (`1458`). Worker completion is skipped when `_alive` is false (`925`, `930`), and `_on_search_finished` also returns before its `finally` when the frame is gone (`949`–`952`). The only normal busy release is `main_frame._set_busy(False)` at `962`.

Reproduction: start a search; close its window while the worker waits; release the worker; reopen search. Evidence records `busy_while_running=true`, `busy_after_worker_finished=true`, and `reopened_search_enabled=false`.

Impact: searches, previews and batch downloads remain blocked until restart; reopening the window does not recover. Closing the main app can also falsely report a search still running after it completed.

Fix: move lifetime/ownership of the search job out of the child window and always settle it even if presentation callbacks are dropped. A minimal fix can release this operation's busy ownership on close/worker completion, guarded so a late worker cannot clear a newer job. Add this exact close-while-worker-running regression.

Evidence: `evidence/ux_native_probe.py`, `evidence/ux_native_probe.json` (`search_close`).

<a id="ux-03"></a>

### UX-03 — P1: authentication fields collapse to zero height in the default Preferences dialog

Confidence: confirmed native GTK geometry. Exact pixel sizes are platform-specific; the missing scrolling/layout constraint is cross-platform code.

`preferences.py:109` sets 640×520; Downloads uses an ordinary `wx.Panel` (`225`–`226`) with all fields in a vertical sizer (`229`–`412`) and no scroll container/minimum fitting policy. Native Downloads page is 622×415, while its minimum content size is 610×642. At default size AO3 cookie is height 0; AO3 User-Agent, ScribbleHub cookie, and SubscribeStar cookie are height 0 at y=421, outside the page.

Reproduction: open Preferences → Downloads at the shipped initial size. Inspect/present the cookie fields; they have zero drawable height. These are required configuration paths for restricted works and subscribed feeds, not optional decoration.

Fix: make long preference pages vertically scrollable with keyboard focus scrolling, or split connection credentials into a separate tab. Retain usable minimum control heights and size dialogs to the current work area/font scale. Test default size plus enlarged fonts and a 1366×768 screen.

Evidence: `evidence/ux_native_probe.json` (`preferences_downloads`).

<a id="ux-04"></a>

### UX-04 — P2: default search windows clip filters horizontally

Confidence: confirmed native GTK geometry.

`gui_search.py:315` opens 820×640. Choice filters use a fixed eight-column grid (`394`–`407`) rather than adapting to available width. Native FFN minimum layout is 982×687: Genre is x=729,w=123 and Time x=729,w=169, both beyond the 820px panel. AO3 Status is x=788,w=114. No scrolling or minimum-width constraint exposes the clipped portions.

Impact: controls/selected values are partly hidden in the default workflow; larger fonts or narrow displays amplify it. This is distinct from screen-reader naming: a semantic label does not make the visual layout usable for low-vision users.

Fix: fewer columns or wrapping filter rows with a collapsible advanced-filter section; keep keyboard order matching visual order. Verify all sites at default window size and with larger system font settings.

Evidence: `evidence/ux_native_probe.json` (`search_geometry`).

<a id="ux-05"></a>

### UX-05 — P2: library/watch jobs discard saved Cloudflare and newer site credentials

Confidence: confirmed using synthetic preferences through the real `DownloadJob.from_prefs` and `_build_scraper`, with constructors patched to avoid network/cache access.

`jobs.py:47` defaults `cf_solve=False`; `from_prefs` (`81`–`131`) never reads `KEY_CF_SOLVE`. The schema (`49`–`52`) omits `scribblehub_cookie` and `subscribestar_cookie`, although `_build_scraper` reads those fields at `cli.py:805` and `815`. Manual GUI snapshots include all three (`gui.py:2923`–`2935`). GUI library refresh seeds jobs via `library/refresh.py:387`; watch auto-download does so via `cli.py:5280`.

Reproduction: synthetic prefs enable CF solver and set both cookie strings. Resulting job has `cf_solve=false`, no cookie attributes; both scraper constructors receive only `max_retries=5,use_cache=true`. Environment fallbacks may hide this when independently configured; GUI-only setup exposes it.

Impact: a manual download can succeed while automatic refresh fails at the same site's Cloudflare/login gate. SubscribeStar authentication is effectively mandatory for its feed.

Fix: add the missing schema fields and seed all three settings from prefs; preserve CLI/environment precedence. Verify manual, library-refresh and watch paths with the same synthetic credential settings.

Evidence: `evidence/ux_jobs_probe.py`, `.json`. Library reviewer independently reproduced the same omission; consolidate rather than count twice.

<a id="ux-06"></a>

### UX-06 — P2: fetching Audiobookshelf libraries freezes the desktop event loop

Confidence: confirmed native event-loop probe plus production timeout inspection.

The button handler synchronously calls `list_libraries(self.prefs)` at `preferences.py:528`; production performs a HTTP request with a 30-second timeout (`audiobookshelf.py:25`, `126`). Native probe records `main_thread=true` inside the request, and a 20ms GUI timer cannot fire until the fake request returns.

Impact: an unreachable or slow server stalls repaint, keyboard input, screen-reader navigation and cancel/close actions for the request duration.

Fix: snapshot the draft URL/token on the UI thread, fetch on a worker, disable the request button while active, and deliver results only while the dialog still exists. Keep a visible/announced request status and a bounded cancellation path.

Evidence: `evidence/ux_abs_preferences_probe.py`, `.json` (`timeline`). No network request was sent.

<a id="ux-07"></a>

### UX-07 — P2: Cancel does not discard Audiobookshelf connection changes

Confidence: confirmed native dialog probe with in-memory settings.

`preferences.py:525`–`526` writes the edited URL and token to persistent prefs before Fetch libraries. Cancel never rolls those writes back. All other normal Preferences edits are staged until `_save` at `848`.

Reproduction: start with `https://old.invalid`, change to `https://new.invalid` and a different synthetic token, Fetch libraries, then close without OK. The preferences store retains the new URL/token. Fetch failures also occur after the same early writes.

Impact: exploring or mistyping an alternative server can change the active upload destination/credentials even after Cancel. A later render can use the unexpected configuration.

Fix: fetch with a temporary configuration object built from the draft controls; persist only on OK. Keep library/folder selections coupled to the same draft server, and clear stale ids when its library list changes.

Evidence: `evidence/ux_abs_preferences_probe.json` (`settings_after_cancel_without_ok`).

<a id="ux-08"></a>

### UX-08 — P1: Update File leaves a renamed book stale and writes a second book

Confidence: confirmed real GUI orchestration and TXT exporters in a temporary directory, with only upstream fetching and indexing mocked.

`gui.py:2586` pins the existing file's format and **parent folder**, but retains the current filename template. `_run_download` uses `update_path` to read chapters then calls `_export_story(story, params)` (`3506`), which re-derives the filename from upstream title/author and the template (`3271`). It never passes the original target pathname to the exporter.

Reproduction: create `My renamed book.txt` with one chapter, offer a two-chapter upstream version, invoke actual `_begin_update_for_path`. Original hash remains unchanged and it still has one chapter; a new `Synthetic title - Synthetic author.txt` has two. The GUI reports `Done! Saved to: ...Synthetic title - Synthetic author.txt`.

Impact: renamed books, foreign exports and titles changed upstream do not update where the reader expects. Repeated updates can re-fetch the same chapter and grow duplicate entries; auto-sorting can also choose a different folder because this path still calls `_resolve_output_dir`.

Fix: preserve the exact destination for an update independently of export naming/autosort preferences, with atomic replacement only after successful export. Test renamed files and upstream title changes across TXT/HTML/EPUB. CLI's separate update-path findings belong to the root report.

Evidence: `evidence/ux_gui_update_probe.py`, `.json`.

<a id="ux-09"></a>

### UX-09 — P2: the first-run manual directs users to controls that moved

Confidence: confirmed code/documentation comparison.

`README.md:108`–`113` says the main window is a download form and instructs users to paste into its URL box. The actual main surface is the library (`gui.py:779`); the download form lives in a hidden child window opened by File → Add Story / Ctrl+D (`465`–`490`, `2318`, `3805`). The getting-started menu tour omits Add Story. It also describes Ctrl+Shift+L as loading a **text file** (`README.md:121`–`123`), while its current dialog expects a **web list URL** (`gui_dialogs.py:2163`, `2253`). The Preferences hint says the library folder is configured in Preferences (`README.md:115`), but that dialog has no Library tab.

Impact: a user following the shipped first-download steps cannot find the named controls, and a user with `urls.txt` is directed to a different operation. This matters especially for keyboard/screen-reader users relying on exact instructions.

Fix: rewrite Getting started around the library-first home, Ctrl+D and the first-save folder picker; distinguish URL-list extraction from CLI `-b urls.txt`. Update help strings too: Download's help still says Ctrl+D downloads and Watch Clipboard's help says Ctrl+W toggles it (`gui.py:865`, `886`), although those keys open Add Story and Watchlist.

Evidence: current README and menu/button bindings; no browser/UI speech claim.

## Cross-review candidates and ownership

- Audio reviewer/root notified: `_snapshot_download_params` creates `llm_render_config` only with author's-note stripping enabled (`gui.py:2897`–`2912`), while `_export_story` uses it for LLM audiobook attribution (`3221`–`3250`). Also one `_render_cancel` slot is shared by concurrent per-site audio exports (`3236`–`3255`). Audio reviewer owns validation/reporting.
- Library reviewer owns `make_watch_downloader` default output `.` and dedupe joins against GUI `Future(None)`; avoid duplicate findings.
- DownloadJob credential parity is confirmed as UX-05; library reviewer has matching evidence. Root owns CLI entry-point, packaging and full-suite results.

## Coverage and limits

Read current main GUI/download/close/snapshot paths, search worker lifecycle and controls, watchlist mutation/poll lifecycle, Preferences controls/save paths, optional-features and URL-list dialogs, queue implementation, and README promises. Native GTK probes use in-memory settings or temporary config files and fake network operations. No accounts, live libraries, or real downloads modified.

No NVDA, JAWS, VoiceOver, or Orca speech validation. GTK geometry/control-state probes do not establish Windows/macOS screen-reader behavior. The consolidated report records baseline test execution. Product opportunities and unvalidated accessibility risks are kept separate from verified bugs.

## Product opportunities and capability parity

These are design/capability gaps, not additional reproduced failures. Prioritize the P1/P2 defects above before adding broad new features.

| Area | Current implementation | Bounded improvement |
| --- | --- | --- |
| First-run destination and navigation | Home is the library list; Ctrl+D opens Add Story; Ctrl+L opens a management window; Ctrl+B refreshes/focuses the home list (`gui.py:779`, `3805`, `4080`, `4189`). A destination is requested only at first download (`2275`). | Empty-library actions: Add story, Choose library folder, Import existing folder. Show current destination in home status. Label the menu action `Manage library…` and document Ctrl+B as focus-library, so the user does not have to infer three different meanings of “Library.” |
| Queue control | Per-site queues expose counts and `cancel_site`, but `cancel_site` has no GUI caller. The main UI primarily reports appended logs; cancellation is available for audio only (`download_queue.py:307`, `gui.py:903`, `1196`). | An accessible Jobs list with story, destination, format, queued/running/failed state and one action to cancel pending jobs/retry failures. Start with the existing queue API; persist/resume the queue only as a separate, explicitly scoped feature. |
| Search responsiveness | A global `_downloading` check gates search/load-more (`gui_search.py:736`, `856`); unrelated active downloads can make an otherwise enabled Search click silently return. Progress and per-site failures are written to the main window log, outside the search window (`560`, `1146`). | Local search status, visible result/selection counts and Cancel search. Scope search ownership per window/job and provide an explicit busy reason. Show partial site failures next to results, with retry for failed sites. |
| Search selection and filtering | Native checkboxes are supported, while Download Selected acts on checked rows or the highlighted row (`gui_search.py:1233`). The FFN fandom picker allows multiple choices even though the adapter uses the first (`128`–`141`). | Explain checked-vs-highlighted action in the dialog, show `N checked`, and add select-all/none. Make FFN fandom choice single-select until multiple fandom queries are implemented. Keep secondary filters collapsed but discoverable. |
| Watchlist setup and editing | Add dialogs create story/author/search watches; only pause/resume/remove are available afterward (`gui_watchlist.py:347`, `394`, `449`). `Watch.filters` exists and polling passes it to search (`watchlist.py:198`, `802`), but Add Search has only site/query/label/channels. | Edit existing watches without deleting their poll history. Add “Watch this search” to search results and carry the active filters. Allow adjusting channels, label and auto-download on an existing watch. |
| Notification visibility | Channel choices start all checked, including channels without configured credentials (`gui_watchlist.py:643`). Watchlist exposes counts and last error; there is no local new-items history. | Show configured/unconfigured status per channel, provide a Test notification action, and store a simple local recent-alert list with open-story/open-file actions. This also gives users a useful outcome if they use no third-party notification service. |
| Email/Kindle desktop parity | CLI can send exports to Kindle. Notifications GUI has recipient only; SMTP host/port/user/password are environment/settings configuration, and the help says CLI `--send-to-kindle` configures them (`preferences.py:92`, `cli.py:4408`, `mailer.py:54`). | A connection settings section for SMTP, Test connection/send, and a Send to Kindle action on an existing export. Keep passwords masked and separate saving connection settings from testing them. Audio reviewer owns SMTP behavior defects. |
| Batch input and export scope | Desktop Add from URL list extracts a web list. CLI accepts local `-b urls.txt` and chapter ranges; main desktop download params have no chapter-range field (`gui_dialogs.py:2104`, `cli.py:932`). | Add plain-text file/pasted multiline URL import to the existing batch picker. Offer optional chapter selection under advanced download options for users who want a sample or a partial recovery. Preserve the simple default one-URL form. |
| Diagnostics and recovery | Logs exist and are optional on disk (`gui.py:948`–`1088`); repair/backup utilities span the CLI/library code. | A Help → Diagnostics surface that shows version, configured data paths, writable-state checks, optional-feature availability and a copyable redacted report. Link to existing recovery operations once their data-integrity findings are fixed. Do not copy raw cookies/API keys into reports. |

## Static operational concerns requiring targeted follow-up

These call paths are verified; the adverse external outcome is not reproduced against live sites.

- **Per-site serialization is bypassed by batch paths.** `DownloadQueues` promises one worker per site, but `_run_picked_batch` constructs scrapers and calls `scraper.download` directly (`gui.py:3711`, `3727`); single URL jobs remain allowed concurrently. Author/search batch + manual download can overlap requests to the same site, so the queue alone cannot enforce the stated politeness policy. Route each batch item through the existing per-site job flow; do not rewrite the whole application to solve it. Check preview/series paths under the same invariant.
- **Some download triggers bypass destination selection.** Manual Download and search Download call `_require_save_target`; clipboard `_on_clip_timer` (`gui.py:2790`–`2835`) and Show Parts' download (`gui_search.py:1440`–`1448`) enqueue directly. `_resolve_output_dir` returns an empty string unchanged (`gui.py:2965`–`2967`), and exporters resolve that to the current directory (`exporters.py:513`). A fresh session without a configured library can therefore write outside the destination-selection flow. Follow up with isolated first-run probes for each trigger and share one destination validation boundary.
- **Saved search data restoration assumes nested dictionary types.** `_load_state` validates only the outer JSON object then calls `.items()` on `filters`, `text` and `checks` (`gui_search.py:588` onward). A malformed but valid JSON nested value can prevent opening a search window. Treat invalid per-section values as empty/default and expose a reset action; this is a settings-corruption recovery concern, not a normal-data repro.
- **Queue deduplication joins by story identity, not requested output.** This candidate is now reproduced and counted as [CORE-10](CORE.md); the related successful `Future(None)` outcome mismatch is L10 in [LIBRARY.md](LIBRARY.md). Distinguish fetch dedupe from export intent, and give joined jobs a shared result contract.

## Accessibility validation still required

The code deliberately uses native controls, names many inputs, handles Space on search checkboxes, and refocuses the library when Add Story closes. Preserve these concrete patterns. Their existence alone is not proof of complete assistive-technology support.

| Test surface | Evidence from code/probes | Required manual validation |
| --- | --- | --- |
| Names and descriptions | Many fields use `SetName`; `set_help` separately sets help text and tooltip. Some preference checkboxes use long explanatory paragraphs as their accessible names. | NVDA/JAWS: each tab stop announces a short distinct name, role, value and state; descriptions are available on request and do not obscure navigation. VoiceOver/Orca: equivalent semantic content through their native accessibility APIs. |
| Dynamic status | `_announce_label` sets label/name and attempts a native name-change event (`gui.py:102`). Watchlist, Optional Features and URL extraction instead use `SetLabel` with static names (`gui_watchlist.py:311`, `525`, `597`; `gui_dialogs.py:1041`, `1068`, `2283`). | Listen while focus stays in the originating control. Confirm start/error/completion announcements are delivered exactly once, without reading the entire log repeatedly. Windows MSAA name changes are not a substitute for testing each reader; GTK probes do not validate this. |
| Focus after async work | Search completion explicitly focuses results. Add Story is hidden/refocused; author pickers are modal; errors may appear after the originating frame closes. | Start slow work, switch to another window, then let it finish: completion should not steal focus unexpectedly. Close/reopen, Escape and modal error paths should leave focus on a visible control. Test with queued concurrent downloads. |
| Low vision/layout | UX-03/04 confirm clipping/zero-height controls at native default size. Other complex dialogs also use fixed-size panels. | Large OS text, 150–200% display scaling, small work area, maximized and resized windows; no clipped controls or invisible tab stops. High-contrast palettes must be evaluated beyond the reader's chapter view. |
| Keyboard-only operations | Menu shortcuts and arrow/Space paths exist; three different search multi-pickers share the visible `Pick…` label. | Complete first download, search/filter selection, watch creation/editing once added, update, export, cancellation and recovery without a mouse. Verify context and action for repeated button labels; check mnemonic collisions in full dialogs. |
| Windows/macOS/Linux parity | Probes ran wxPython 4.2.3 GTK3/wxWidgets 3.2.7 under Xvfb. | Windows NVDA and JAWS, macOS VoiceOver and Linux Orca smoke paths with recorded platform versions. Neither CI widget mocks nor this native GTK geometry probe replace those sessions. |

## Verification artifacts and audit boundaries

| Artifact | Verification |
| --- | --- |
| `evidence/ux_native_probe.py` / `.json` | Native search close/busy lifecycle; Preferences Downloads geometry; FFN/AO3/Royal Road/Wattpad default search geometry. Exit 0. |
| `evidence/ux_preferences_storage_probe.py` / `.json` / `.stderr` | Native Linux config-path inspection plus actual `Prefs` writes against an isolated directory collision. Expected native errors captured. Exit 0. |
| `evidence/ux_jobs_probe.py` / `.json` | Real jobs settings mapping and scraper constructor kwargs, synthetic secrets, no requests. Exit 0. |
| `evidence/ux_abs_preferences_probe.py` / `.json` | Native event-loop ordering and cancel-without-save semantics using a fake request. Exit 0. |
| `evidence/ux_gui_update_probe.py` / `.json` | Real update orchestration/export against temporary renamed book and fake upstream. Original hash unchanged; duplicate has new chapter. Exit 0. |

No application changes, installs, notifications, live authenticated requests, or real-library mutations performed. Repository HEAD remains `8f79e57399686357fcfead0e9fce29e70790beb1`; only audit artifacts are untracked. Root reports test-suite/static-check results and the combined priority order. This audit does not assert that every supported website, bundled platform build, screen reader or paid model/provider has been exercised.
