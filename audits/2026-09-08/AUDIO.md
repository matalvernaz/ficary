# Reader, audio, TTS, and integration audit

Snapshot: `8f79e57399686357fcfead0e9fce29e70790beb1`. Audit only; no application changes.

## Checkpoint

2026-09-08: COMPLETE for this audit pass. Fourteen confirmed findings plus UX/hardening observations; evidence and final coverage/limits below. `/home/matt/AGENTS.md` was absent; `/home/matt/CLAUDE.md` was read. All probes used temporary files/mocks and no paid requests or external sending.

## Findings

See confirmed findings AUD-A01–AUD-A14 below.

## Coverage and limits

Assigned: `ficary/reader`, `ficary/audio`, `ficary/soundscape`, `ficary/tts.py`, `ficary/tts_providers`, attribution/profile/accent logic, installer helpers, Audiobookshelf, mailer. Root owns baseline test run. Native wx controls were exercised under Xvfb; screen-reader announcements and actual speaker playback require separate manual validation.

## Verified checkpoint 1

Read `/home/matt/CLAUDE.md` (homelab conventions; no applicable deployment actions). Native wx probes run under Xvfb with synthetic stories, temporary portable root, mock engine/controller. No production preferences, network, email, or synthesis calls used.

<a id="aud-a01"></a>

### AUD-A01 — P1, confirmed: SMTP TLS never verifies the mail server certificate

- Code: `ficary/mailer.py:99-108`, `154-163` (`SMTP_SSL` and `starttls` omit `context`).
- Evidence: `evidence/audio-boundaries-probe.py` / `.txt`. Current interpreter constructs both default contexts with `check_hostname=False`, `verify_mode=CERT_NONE`. `smtplib.SMTP.starttls` uses `ssl._create_stdlib_context()` when no context is passed.
- Impact: an attacker able to intercept the SMTP connection can impersonate the configured relay and obtain SMTP credentials plus emailed content despite the connection using TLS.
- Targeted fix: construct `ssl.create_default_context()` and pass it to both `SMTP_SSL(..., context=...)` and `smtp.starttls(context=...)`; test certificate/hostname rejection with local fixtures or context inspection.

<a id="aud-a02"></a>

### AUD-A02 — P1, confirmed: Piper binary installation fails on nested Unix archives and then reports a directory as installed

- Code: `ficary/tts_providers/piper.py:148-154`, `280-299`.
- Repro: inject a tarball with `piper/piper` (executable) and `piper/libexample.so`, using a temporary install root. Flattening calls `shutil.move('<root>/piper/piper', '<root>/piper')` while the destination is the containing directory. It raises `Destination path '.../piper/piper' already exists`.
- Evidence: `evidence/audio-boundaries-probe.py` / `.txt`: first install `False`; resolved executable `is_dir() == True`; second install `True` without repairing the installation.
- Impact: the supported nested package shape cannot install; subsequent TTS invokes a directory and fails. This can also trigger the cloud fallback in AUD-A03.
- Targeted fix: unpack in a separate staging directory and move the completed layout into the managed root; require `candidate.is_file()` in executable discovery. Validate a representative nested Unix tarball and Windows zip.

<a id="aud-a03"></a>

### AUD-A03 — P1, confirmed: local-only synthesis can silently send story text to Edge

- Code: `ficary/tts.py:2287-2296`, `2305-2308`. `enabled_tts_providers` only constrains voice-pool selection (`3490-3497`); it is absent from segment fallback.
- Evidence: `evidence/audio-boundaries-probe.py` / `.txt`: a failed Piper voice/narrator is followed by `en-US-AriaNeural` and reported successful. Test uses fake synthesis returning failure for Piper, success for the Edge ID; no network.
- Impact: selecting only an offline provider does not constrain the actual synthesis transport. Provider outages/missing models can send the text to Microsoft without another explicit choice and can produce mixed-provider audio.
- Targeted fix: carry the allowed provider set into fallback; use only permitted voices or return a clear failure. Treat cloud fallback as an explicit setting with a visible indication when used.

<a id="aud-a04"></a>

### AUD-A04 — P2, confirmed: app voice ignores restored/bookmarked location and does not persist listening progress

- Code: `ficary/reader/gui.py:177-192`, `327-348`, `380-391`, `459-474`. Playback always passes the full chapter text; highlighting changes style/scroll position but not caret or a dedicated listening offset. Position saving only occurs after loading and on normal close, with no periodic autosave.
- Evidence: `evidence/audio-reader-probe.py` / `.txt` uses real wx controls and a synthetic two-paragraph chapter. Caret at `52` (second paragraph) still sends the complete text beginning `First sentence...` to the TTS controller. Saved caret remains the user's earlier caret regardless of highlights.
- Impact: reopening, jumping to bookmarks, or stopping and restarting speech repeats the chapter from the beginning. App crashes lose screen-reader caret changes too.
- Targeted fix: define one persisted content position, start TTS at its containing chunk, update the listening position from highlight/chunk events, and debounce/autosave. Preserve the screen-reader caret separately if native navigation should not move during app-voice playback.

<a id="aud-a05"></a>

### AUD-A05 — P2, confirmed: delayed voice resolution can restart speech after Stop or a mode switch

- Code: `ficary/reader/gui.py:312-329`, `373-378`; deferred highlight/completion callbacks also lack a controller-generation identity (`342-343`, `350-365`, `380-384`).
- Evidence: `evidence/audio-reader-probe.py` / `.txt`: invoke Stop, switch to Screen reader, then deliver the queued `_begin_playback` callback. A new controller starts while mode is Screen reader. It only checks `_alive`.
- Impact: a slow first voice-catalog request can cause playback after the user cancelled; stale completion/highlight callbacks can apply to a replacement session as they only test `_live is not None`.
- Targeted fix: a reader playback-request generation invalidated by Stop, mode change, chapter change, and close; every deferred resolve/highlight/completion callback must carry and verify it before acting.

Checkpoint 1 completed; subsequent verified results are recorded below.

## Verified checkpoint 2 — synthesis, cancellation, and cache correctness

<a id="aud-a06"></a>

### AUD-A06 — P1, confirmed: incomplete audiobooks are reported successful and their missing speech is permanently cached

- Code: `ficary/tts.py:2417-2443`, `2474-2509`, `3582-3583`, `3616-3624`.
- Repro: one chapter has two speech segments. Fake synthesis succeeds for the first and fails all three attempts for the second. The real chapter assembly returns success with only the first segment; `_generate_audiobook_inner` promotes that incomplete body into the normal content cache. A second render accepts it as complete.
- Evidence: `evidence/audio-cache-race-probe.py` / `.txt`: first output contains only `Successful sentence...`; synthesis calls remain `4` after the second render; `Second render still omits LOST sentence: True`.
- Impact: a transient provider failure permanently removes prose from the book until the user manually clears a hidden cache. Whole failed chapters are also skipped if any other chapter succeeds, with no structured incomplete-result status.
- Targeted fix: return attempted/succeeded/failed segment counts with chapter results. Never cache incomplete bodies as complete; fail export or require an explicit incomplete-export choice, provide a missing-segment report, and retry only failures. Keep fallback voice use visible as degraded output even when speech text is preserved.

<a id="aud-a07"></a>

### AUD-A07 — P2, confirmed: cancellation is ignored during final assembly; preprocessing has no cancellation checkpoints

- Code: `ficary/tts.py:3255-3387`, `3575-3578`, `3604-3610`, `3626-3711`.
- Evidence: `evidence/audio-cache-race-probe.py` / `.txt` sets the cancellation event during the last chapter's heading synthesis. The function still synthesizes the intro, calls the M4B builder, and returns an output file. Existing checks only surround chapter-body work.
- Impact: Cancel can still produce and auto-upload a supposedly cancelled render; a long LLM preprocessing pass can continue making requests for every chapter before observing cancellation.
- Targeted fix: check cancellation before each preprocessing/request/heading/cover/intro/mux stage and before successful return. Thread cancellation into request loops where supported. Keep the GUI's explanation of cancellation granularity accurate.

<a id="aud-a08"></a>

### AUD-A08 — P2, confirmed: an obsolete soundscape build stops the newly selected soundscape

- Code: `ficary/soundscape/session.py:47-55`, `98-109`, `125-140`.
- Repro: block the old soundscape's first decode, select a different soundscape, let the new build finish, then release the old decode. The stale worker calls `engine.stop(CHANNEL_AMBIENT)`, which stops every source, including those the new generation just created.
- Evidence: `evidence/audio-cache-race-probe.py` / `.txt`: new handles change from `[2]` to `[]`; `Session still marked started: True`.
- Impact: selecting an ambience while loading can leave silence with an internally active session; replacement builds can also race a close/reopen.
- Targeted fix: give each build ownership of its handles and stop only obsolete handles, or invalidate/stop the old build before replacement and prevent obsolete workers from issuing channel-wide mutations. Cancellation of a pending build must also work before `_started` becomes true.

<a id="aud-a09"></a>

### AUD-A09 — P2, confirmed: failed LLM attribution is cached as a successful result and never retried

- Code: `ficary/attribution.py:358-367`, `400-414`, `438-445`; caller `ficary/tts.py:3343-3347`, `3375-3385`.
- Root cause: LLM failures are recorded under `(llm, llm_cache_token(provider, model))`, but `has_failed` checks `(llm, normalize_size(...))`, which is always `(llm, None)`. The cache guard therefore reports false after a configured LLM failure.
- Evidence: `evidence/audio-attribution-probe.py` / `.txt`: recorded key is `('llm', 'ollama-audit-model')`, `has_failed('llm', None)` returns `False`, a fallback cache entry is written, and a second render after resetting process failure state never invokes the provider again.
- Impact: an initial outage/authentication error can lock a chapter into builtin attribution across app restarts. The shared `_failed_runs` set also lasts until process exit, so correcting credentials for the same provider/model cannot retry within the session.
- Targeted fix: make failure status an explicit per-call result, or centralize one config-aware failure key used by both dispatcher and caller. Scope suppression to a render and add a retry/reset action. Include character priors, relevant endpoint/model identity, and algorithm version in cache invalidation where they affect attribution.

<a id="aud-a10"></a>

### AUD-A10 — P2, confirmed: choosing LLM speaker attribution does nothing unless unrelated note stripping is enabled

- Code: `ficary/gui.py:2897-2912`, `3220-3226`, `3244-3246`; resulting fallback `ficary/attribution.py:428-445`.
- Evidence: `evidence/audio-gui-probe.py` / `.txt`: snapshot with audio attribution `llm`, valid configured provider/model, and note stripping disabled produces `audio_backend='llm'`, `llm_render_config=None`.
- Impact: the selected attribution backend silently falls back instead of running. The inverse setting combination is also inconsistent: LLM note stripping with builtin attribution loses the config at `_export_story`; the TTS API only enables LLM note stripping when attribution backend is `llm` (`tts.py:3298-3302`).
- Targeted fix: snapshot config whenever either feature requires it and represent attribution config and note-stripping config independently through the audio API. Test all four combinations.

<a id="aud-a11"></a>

### AUD-A11 — P2, confirmed: concurrent audiobook renders overwrite and clear each other's Cancel control

- Code: `ficary/gui.py:922-930`, `3236-3255`.
- Repro: start render A, then B on the same frame. B replaces `_render_cancel`. When A completes, its unconditional `finally` clears B's event and disables the shared button even though B remains active.
- Evidence: `evidence/audio-gui-probe.py` / `.txt`: `Second render overwrites cancel target: True`; after A finishes, pointer `None`; Cancel never sets B's event.
- Impact: per-site concurrent downloads that both export audio cannot reliably be cancelled. A cancelled/finished earlier operation also changes the active operation's controls.
- Targeted fix: associate cancellation with queue/job identity; expose per-job Cancel or a clearly defined Cancel-all action. At minimum, cleanup must verify it still owns the stored event before clearing it.

<a id="aud-a12"></a>

### AUD-A12 — P2, confirmed: the live-TTS generation token does not prevent playback after Stop

- Code: `ficary/reader/live_tts.py:151-173`, especially highlight/event emission followed by unconditional `play_file`; Stop invalidation at `111-129`.
- Repro: pause the worker inside its highlight callback, call Stop (which returns after the bounded join), then let the obsolete worker continue. No current-generation check protects playback between callback return and `play_file`.
- Evidence: `evidence/audio-live-stop-probe.py` / `.txt`: `Play calls before releasing stale worker: 0`; `Play calls after Stop already returned: 1`.
- Impact: speech can begin after Stop, and an obsolete controller can speak over a replacement. This is a distinct lower-layer race from the reader's queued voice-resolution callbacks in AUD-A05.
- Targeted fix: make playback registration conditional on an owned generation under a synchronization boundary shared with Stop; checking only before callbacks leaves another check/use race. Keep callbacks out of locked sections and propagate request identity into the engine.

<a id="aud-a13"></a>

### AUD-A13 — P2, confirmed: naturally completed chapters leak live-TTS temporary audio when advancing

- Code: `ficary/reader/gui.py:197-203`, `339-348`, `366-371`; cleanup only in `ficary/reader/live_tts.py:122-131`.
- Repro: finish one chapter, load/play the next, then close the reader. `_load_chapter` only stops an *active* controller. `_begin_playback` then replaces the inactive controller without cleanup; close only stops the latest controller.
- Evidence: extended `evidence/audio-reader-probe.py` / `.txt`: first completed chapter's temp directory survives reader close; latest chapter's directory is removed. Probe cleans up its generated directory afterward.
- Impact: auto-advance and repeated Play accumulate MP3 chunks in the system temp directory, consuming disk and leaving story audio behind after the reader closes.
- Targeted fix: always dispose the previous controller before replacement, including completed controllers; separate stop/cancel from resource disposal if retained audio is intended for quick resume.

<a id="aud-a14"></a>

### AUD-A14 — P2, confirmed: reader selects the first installed voice regardless of enabled provider or language

- Code: `ficary/reader/gui.py:397-405`; provider aggregate ordering `ficary/tts_providers/__init__.py:128-150`.
- Evidence: extended `evidence/audio-reader-probe.py` / `.txt`: preferences contain `tts_providers='piper'`; a synthetic catalog offers an Edge Arabic voice first and Piper English voice second. `_default_voice()` returns the Edge voice. No reader voice-choice control exists.
- Impact: first Play can choose an unrelated language and use cloud speech despite selecting local Piper elsewhere. The reader lacks a visible way to choose the voice or discover which provider will receive the text.
- Targeted fix: expose/persist a reader voice selection, honor enabled providers, filter by story/user language, and show local/cloud status before playback. Avoid relying on remote catalog ordering.

## Additional UX, missing-feature, and hardening observations

These are distinct product work or static observations; they are not included as additional isolated-probe defects above.

- **P2, static data-loss path: ambient imports overwrite other soundscapes' source files.** `ficary/soundscape/editor.py:158-170` stores imports by basename and uses `shutil.copy2` over an existing path. Importing two unrelated `rain.mp3` files changes every soundscape referencing that name. Use content-based or unique asset filenames and a visible replace choice. `library.save` similarly uses a lossy name slug (`ficary/soundscape/library.py:46-50`); different names that slugify identically replace the same definition.
- **P3, confirmed missing behavior: Reverb room size is editable but does nothing.** The editor exposes and persists the slider (`soundscape/editor.py:56-59`, `201`), while `_OpenALBackend.set_reverb` is explicitly `pass` (`audio/engine.py:434-437`). Disable/hide the control with a clear availability explanation until implemented. No listening-based reverb test was performed.
- **P3, UX: sleep timer has no Cancel action.** `SleepTimer.cancel` exists, but the reader only invokes it on close. The UI provides Set and Status (`reader/gui.py:82-83`, `431-448`). Add Cancel and visible countdown/state; the modal 'Reading stopped' message at expiry can itself wake a listener through the screen reader.
- **P3, UX: soundscape edits lack preview, dirty-state protection, and stable rename identity.** Closing/selecting another item discards unsaved edits; a name change saves a second slug while stories remain assigned to the old slug. Add preview, Save/Discard handling, and preserve assignment identity across rename.
- **P3, robustness: the playback decoder has no timeout or stdin isolation.** `_OpenALBackend._decode_to_wav` (`audio/engine.py:368-370`) invokes ffmpeg without `timeout`/`stdin=DEVNULL`, unlike export helpers. A decode can outlive controller cancellation indefinitely. If OpenAL rejects a successfully decoded WAV, `load` catches and returns without removing that temp file (`378-398`). These paths need bounded process lifecycle and failure cleanup.
- **P3, installer limits: verify downloaded content, installation state, and cancellation.** Piper/model and embedded-Python installers use HTTPS and staged files, but the streaming helpers do not compare received bytes with Content-Length/checksums. Neural pip subprocess readers have no overall deadline/cancel path (`neural_env.py:354-369`, `401-421`). Full Windows install/rollback was outside this Linux audit.
- **P3, export integration: Audiobookshelf uploads are unconditional.** `audiobookshelf.upload_file` documents duplicate creation on re-render (`97-105`); GUI auto-upload is called after every successful render (`gui.py:3256-3257`). Store source/item identity and offer update-versus-new behavior. This should align with root's story-identity findings.
- **Shared identity issue:** TTS voice/pronunciation/accent/profile sidecars use only `story.id` (`tts.py:3227`, `3234`, `3435-3436`). Root confirmed merged series/SubscribeStar can use `id=0`; unrelated stories in the same output folder consequently share user voice and pronunciation settings. Chapter-body cache also buckets by ID but includes content/voices/rate in its hash, so ID collision alone does **not** establish a wrong-audio cache hit. Root owns the primary identity finding.
- **P2, static reader source limitation (root discovery): sparse chapter ranges are presented as a contiguous book.** `StorySource.from_file` uses `chapter_count=max(by_number)` and cache mode similarly uses the maximum stored number (`reader/source.py:95`, `145-148`), while the reader lists `1..count` and initially loads chapter 1 (`reader/gui.py:164-178`). A downloaded chapter range beginning at chapter 5 therefore exposes unavailable chapters 1–4 and initially errors. Expose an ordered list of available chapter IDs instead of deriving identity from list position. Stable-key Webnovel/Wattpad cache layouts also need an adapter or an explicit exported-file fallback; `_cached_chapter_numbers` only recognizes `ch_<digits>` names. This was inspected statically, not added to the isolated-probe findings.
- **Reader navigation/accessibility follow-up:** add Find-in-chapter, visible play/pause/loading/failure state, choose/resume/restart behavior, transport shortcuts documented in the UI, and check font/theme contrast and bookmark editing with real assistive technology. Generic chapter labels omit existing chapter titles. These are product recommendations, not claims of an observed screen-reader failure.

## Coverage, verification, and limits — final

Status: **Complete for this audit pass**, 2026-09-08. All findings above are saved; no application code changed.

- Read/reconstructed: reader source → display/caret/state → live-TTS worker → shared audio engine → OpenAL; soundscape editor/library/model/session; segment retries → chapter assembly/cache → final M4B; attribution dispatcher/LLM request and parsing boundaries; provider catalog/synthesis/Piper installation; character-profile/pronunciation/accent handling; neural/Ollama install lifecycle; SMTP/Audiobookshelf transport.
- Verification artifacts: `audio-reader-probe.py/.txt`, `audio-boundaries-probe.py/.txt`, `audio-cache-race-probe.py/.txt`, `audio-attribution-probe.py/.txt`, `audio-gui-probe.py/.txt`, `audio-live-stop-probe.py/.txt`. All completed successfully using temporary data and mock network/synthesis/process boundaries. Reader probe used native wx controls under Xvfb. Thread races were controlled by barriers/events, not inferred from a random failure.
- Root baseline: **2,190 passed, 62% coverage**, reported by root; no redundant baseline rerun here. The new probes expose behaviors absent from that passing baseline.
- No paid LLM/TTS requests, real email, uploads, provider installs, production preference writes, or application mutations were performed. Real audio devices, acoustics/3D/reverb, Windows/macOS native behavior, NVDA/VoiceOver/Orca announcements, live provider catalogs/API compatibility, and large real-world story attribution quality remain unverified.
- The parser is extensive and fixture-driven; this pass inspected its integration and failure/cache contracts rather than claiming exhaustive correctness over all possible prose. Add a curated human-labelled dialogue corpus with coverage for speaker attribution, missing/duplicated speech, text conservation, and provider failure recovery before changing parser heuristics.
- Highest priority: AUD-A01 SMTP verification; AUD-A03/A14 provider boundaries; AUD-A02 Piper installation; AUD-A06 incomplete-output/cache correctness. Then cancellation/ownership races and reader resume/lifecycle fixes. UX changes should follow those behavioral contracts.
