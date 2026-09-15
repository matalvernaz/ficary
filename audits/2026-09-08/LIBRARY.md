# Library integrity and watchlist audit

Status: complete for the assigned code scope; 16 consolidated findings reproduced. Baseline: `8f79e57399686357fcfead0e9fce29e70790beb1`. Audit-only; reproductions use isolated temporary data and mocked network/notification calls. Application code and real user data are unchanged.

## Coverage plan

- Library persistence, indexing/scanning, metadata edits, backups/restores, reorganization, healing and doctor repairs.
- Watchlist persistence, polling/update state and notification failure handling.
- Search, review, mirror identification and library-facing UX correctness.

## Verified findings

Run: `PYTHONPATH=. .venv/bin/python audits/2026-09-08/evidence/library_repros.py`. Evidence: [reproduction script](evidence/library_repros.py), [captured output](evidence/library_repros.log). Every case uses a temporary directory; network and delivery are replaced with fakes.

Additional integrations: `PYTHONPATH=. .venv/bin/python audits/2026-09-08/evidence/library_integration_repros.py`. Evidence: [integration reproduction script](evidence/library_integration_repros.py), [captured output](evidence/library_integration_repros.log). Both scripts completed with exit code 0; the captured exceptions are deliberately reproduced defects, not test-harness failures. These are diagnostic scripts rather than passing regression tests: their output records the current incorrect behavior.

<a id="l01"></a>

### L01 — P1 / verified: restoring the oldest retained backup deletes it before reading

`ficary/library/backup.py:155` calls `backup(index_path)`, which prunes the rolling pool to ten, before `backup_path.read_bytes()` at line 162. With ten existing backups, restoring the oldest causes the pre-restore safety snapshot to evict the exact snapshot being restored. The operation raises `FileNotFoundError`, and the selected recovery point is permanently gone. Reproduction: `restore_oldest` reports `selected_backup_survives=false`; current index is unchanged.

Recommendation: read and validate the selected snapshot before pruning, or protect it explicitly throughout the operation. Preserve the pre-restore safety snapshot as well.

<a id="l02"></a>

### L02 — P2 / verified: a poll completion undoes concurrent user edits to a watch

`ficary/watchlist.py:409` reloads current disk state in `WatchlistStore.update()` but replaces the complete matching row with its stale caller object at line 420. `run_once()` holds that object across the network/download work and saves it at line 672. A GUI edit during the poll (pause, label, channel or auto-download changes) is overwritten by the old row. The isolated scraper callback pauses and renames the watch; after polling, `enabled=true` and `label=""` again.

Recommendation: update only poll-owned fields on the latest record, and re-read enabled/channel/auto-download configuration before side effects. User edits and polling state need distinct merge policies; a reload alone does not merge them.

<a id="l03"></a>

### L03 — P2 / verified: failed auto-downloads are forgotten rather than retried

`ficary/watchlist.py:726`, `:764`, and `:815` advance the observed baseline before delivery/downloads. `run_once()` catches a downloader failure at line 615 but saves the advanced baseline; the next poll has no `new_items`, so the download gate at line 607 never retries. `last_error` is also cleared on that next successful probe at line 595. Reproduction: a 2→3 chapter update, a simulated disk failure, and a second poll produce one download attempt total and an empty persisted error.

Recommendation: persist pending download items independently from observation state; retire each only after successful save. Expose Retry Failed and preserve the failure until retry succeeds or the user dismisses it. Author/search watches need per-item progress to avoid re-downloading successes when only one item fails.

<a id="l04"></a>

### L04 — P2 / verified: notification failures silently consume the alert

At `ficary/watchlist.py:654`, `run_once()` ignores the dispatcher's `(delivered, failures)` return value. It advances cooldown at line 668 and saves the already-advanced baseline even if every channel failed. The normal dispatcher reports failures via return values, so the exception-only `last_error` path never runs. Reproduction returns one email failure: one attempt, advanced `last_seen`, nonempty cooldown, empty `last_error`, and no retry on the following poll.

Recommendation: preserve pending delivery per channel, expose normal returned failures in the watch status, and only advance successful delivery state. New updates detected during cooldown should be queued/coalesced for later delivery rather than discarded.

<a id="l05"></a>

### L05 — P2 / verified: toggling autopoll off then on during a poll leaves it stopped

`ficary/watchlist_poller.py:129` calls `start()` only when the worker is already dead. If a poll is in flight, off sets `_stop`; on observes `is_running()==true` and does nothing. The cancellation logic inside `start()` is never reached, and the worker exits once its current poll returns. Reproduction synchronizes a real worker using events: preference remains enabled, `_stop` remains set, and the worker is dead after release.

Recommendation: when autopoll is enabled, always call the idempotent `start()` so its lock-protected stop cancellation executes. Test the actual `reconfigure()` path, not only direct `stop()`→`start()` calls.

<a id="l06"></a>

### L06 — P2 / verified: normal rescans erase manual adult and abandoned marks

`ficary/library/index.py:339` preserves a fixed list of tracking fields when `record()` replaces a story, omitting both `abandoned_at` and `adult`. An ordinary `scan(clear_existing=False)` therefore silently revives manually retired stories even though `abandoned.py` describes the mark as sticky until explicit revival. It also removes the explicit adult classification written by `ficary/library/browser.py:707`: a manually hidden FFN/AO3 story becomes visible again under the browser's source-derived fallback. Reproductions mark an indexed HTML export abandoned/adult and rescan with the automatic threshold disabled: both fields disappear. The adult case is in the integration log.

Recommendation: preserve `adult` and `abandoned_at` on ordinary rescans; define an explicit revival rule for a verified upstream update. Treat manually assigned identity/provenance and scan-derived metadata separately.

<a id="l07"></a>

### L07 — P2 / verified: doctor heals stale cache signatures without refreshing cached metadata

`ficary/library/doctor.py:343` updates only `file_mtime`/`file_size` for drifted files. `_cached_chapter_count()` in `ficary/library/refresh.py:50` then trusts the old `chapter_count` because the signatures now match. Reproduction scans a two-chapter HTML export, overwrites it with three chapters, and builds the refresh queue: local count is correctly 3 before heal and incorrectly 2 after heal. The “repair” converts detectable stale data into falsely trusted data.

Recommendation: re-extract the metadata represented by the signature before stamping it; if extraction fails, retain invalidation so the refresh path reparses. Include chapter count, status and other file-derived fields under the same invariant.

<a id="l08"></a>

### L08 — P2 / verified: cache quarantine failures fall back to irreversible deletion

`ficary/cache_doctor.py:195` uses second-resolution batch names, and lines 201–205 fall back from failed `os.replace()` to `shutil.rmtree()`. This contradicts the recoverable-quarantine contract. Reproduction prunes a cache, recreates that cache, then prunes again under the same timestamp: the second rename hits the occupied nonempty destination and deletes the new cache. Output still claims one pruned entry and points at the old-generation quarantine directory.

Recommendation: use unique batch directories and fail closed on quarantine errors; report entries that could not be moved. Never delete a source merely because the recovery-preserving move failed. Report quarantined bytes separately from actually reclaimed disk space.

<a id="l09"></a>

### L09 — P2 / verified: fresh watch downloads bypass the configured library and adult routing

`ficary/cli.py:5280` builds `DownloadJob.from_prefs()`, but does not run `_apply_library_autosort()`. `ficary/jobs.py:67` defaults `output=None`; `cli.py:5292` therefore uses `Path(".")` for unindexed stories. The routing helpers require `_library_autosort`, which is also absent. The integration reproduction configures both main and adult roots, invokes the real watch-downloader closure with a fake export boundary, and observes `output_dir="."`, `autosort=false`.

Impact: new works discovered by author/search watches can land in the application's current working directory, stay absent from the library, or fail under a nonwritable working directory. Dedicated adult routing is also skipped. Existing indexed-story updates take their indexed path and are unaffected by this specific defect.

Recommendation: initialize watch jobs through the same library/output resolution path as fresh CLI/GUI downloads. Use the passed preference object consistently and show the resolved destination in watch setup. The same reproduction verifies dropped `CF_SOLVE`, ScribbleHub and SubscribeStar cookies; that broader preference-plumbing defect is owned by the UX audit and should be counted once there.

<a id="l10"></a>

### L10 — P2 / verified: joining a successful GUI download reports auto-download failure

`ficary/cli.py:5319` joins the canonical URL's queue future, then treats any falsy `fut.result()` as failure at line 5325. GUI queue jobs call `MainFrame._run_download()` (`ficary/gui.py:2377`), whose successful export path falls through with `None` (`:3499` onward). The joined job also never runs the watch closure's `on_export=saved.append` callback. Reproduction claims an unfinished future through the real `single_flight` registry, verifies the real queue joins it, then completes it with the GUI success value `None`: the watch raises `download failed ...`, and its own download callback never runs.

Recommendation: give queued downloads a shared result contract containing success, saved paths and failures; joiners must receive those results. A future's truthiness is not a portable success protocol across GUI and CLI entry points. Keep failure reporting distinct from a successfully deduplicated job.

<a id="l11"></a>

### L11 — P2 / verified: two first-time index writers silently overwrite one another

`ficary/library/index.py:164` returns a missing-file instance with `_loaded_sig=None`; `save()` only checks conflicts when `_loaded_sig is not None` at line 217. Two instances loaded before the first write both believe they are first writers. A saves story A, B saves story B, both succeed, and A disappears. The reproduction uses two independent instances and sequential saves, so no timing race or coarse filesystem clock is needed. This can occur when first-run cross-site downloads or simultaneous GUI/CLI operations both initialize the index.

Recommendation: represent “loaded an absent file” as a checked state, reject save if a file has appeared, and use an atomic cross-writer compare/write strategy or shared storage transaction for the broader concurrency contract. The existing docstring explicitly identifies concurrent CLI/GUI use as a supported concern. This reproduction does not claim cross-process behavior was exercised; it demonstrates the same persistence defect without requiring processes.

<a id="l12"></a>

### L12 — P2 / verified: malformed index files still crash recovery-facing consumers

`ficary/library/index.py:168` catches JSON and I/O errors but omits `UnicodeDecodeError`. Schema validation at line 181 checks only the outer `libraries` dictionary; `_migrate_non_canonical_keys()` assumes each library value is a dictionary at line 622. Reproduction loads a one-byte non-UTF-8 file and then a valid JSON file with a null library value: `UnicodeDecodeError` and `AttributeError` respectively escape `LibraryIndex.load()`, despite its never-raises contract. Integrated doctor calls the loader directly, so these inputs prevent its recovery report.

Recommendation: validate nested schema and text decoding at this storage boundary, preserve or snapshot the original, and return an explicitly blocked recovery state. Avoid silently dropping malformed nested entries while keeping healthy entries looking complete.

<a id="l13"></a>

### L13 — P2 / verified: doctor drops a story even when an indexed duplicate still exists

`ficary/library/doctor.py:287` deletes the whole story entry when its primary file is missing. It does not promote a surviving `duplicate_relpaths` entry. `check_integrity()` already regards those duplicates as tracked, so they are absent from the orphan list used later in the same heal. Reproduction moves a story file manually, rescans (recording the new path as a duplicate of the missing old primary), then heals with `drop_missing=True, scan_orphans=True`: one entry is removed, zero orphans are scanned, the renamed file survives, and the story is no longer indexed.

Recommendation: promote an existing readable duplicate before dropping a story. Detect a renamed/moved primary during scan and preserve the story's user metadata. A one-pass doctor should leave surviving content tracked and updatable.

<a id="l14"></a>

### L14 — P2 / verified: library template length limits count characters rather than filesystem bytes

`ficary/library/template.py:104` caps segments using `len(s)` and character slices. On UTF-8 filesystems, a modest non-Latin title can exceed the usual component byte limit well below 200 characters. Reproduction renders a 100-character CJK title plus `.html`: 105 characters, 305 UTF-8 bytes; creating the path on this host raises `OSError: errno=36` (`ENAMETOOLONG`). This affects reorganizing into title-based paths and templates that use long Unicode fields as folders.

Recommendation: cap encoded component bytes without splitting a Unicode character, reserve extension/suffix space, and make truncation collision-safe. Validate the rendered path during preview so apply does not discover predictable filesystem errors.

<a id="l15"></a>

### L15 — P2 / verified: reorganization and removal leave stale full-text results

`ficary/library/reorganizer.py:252` saves only the JSON metadata index; `LibraryIndex.remove()` (`ficary/library/index.py:518`) likewise does not synchronize SQLite. `FullTextIndex.search()` (`ficary/library/fulltext.py:315`) returns its own stored relpaths without checking the metadata index, and the CLI renders those paths directly (`ficary/cli.py:2583`). The integration reproduction builds the actual SQLite FTS index, applies a real file move, and receives hits for the now-nonexistent old path. Removing the story from the metadata index leaves both chapter hits searchable.

Recommendation: synchronize path changes/removals with the FTS projection, or reconcile result identities against the current metadata index. Add a stale-index indicator/rebuild action. Existing full-text incremental refresh is only wired through the CLI library-update callback, so fresh GUI downloads and ordinary rescans also need an explicit synchronization policy.

<a id="l16"></a>

### L16 — P2 / verified: silent-edit scans reuse cached chapters and miss the edits they seek

`ficary/library/edits.py:221` constructs default scrapers with caching enabled and `scan_edits()` calls `download()` at line 272 without forcing fresh chapter bodies. In `FFNScraper.download()`, chapter 1 comes from the new metadata fetch, but chapters 2 onward are taken from `_load_chapter_cache()` at `ficary/scraper.py:2465`. Thus a normal warm-cache installation can report an edited story unchanged.

The integration reproduction uses the real `FFNScraper.download()` and actual temporary on-disk chapter cache; only upstream HTML, parsed metadata and pacing are mocked. After caching two original chapters, the fake upstream changes chapter 2. `scan_edits()` reports `unchanged=1`, `silent_edits=0`, and fetches only chapter 1. Turning `use_cache=False` on the same scraper causes both chapters to fetch and correctly reports `changed_chapters=[2]`.

Recommendation: silent-edit checks must bypass local chapter caches and cached mirrors, while retaining authentication/session setup and rate limiting. Apply this invariant to supplied scraper instances too, so a shared scraper cache cannot accidentally disable freshness. Tests should exercise the real cache layer, not only fake scrapers that always return fresh `Story` objects.

## Targeted UX and feature work

- **Recovery and delivery status:** provide persistent pending/failed download and per-channel notification states, last successful poll/delivery timestamps, and Retry Failed. Current “last checked” is probe activity, not evidence that a download or alert arrived (L03–L05, L10).
- **Preserve user decisions:** adult and abandoned flags need durable ownership distinct from extracted metadata; normal Scan/Rescan should not undo classifications (L06).
- **Visible copy management:** show all copies/formats of a URL, identify which is primary, and offer Promote Copy. Current duplicate tracking is largely bookkeeping while updates/browser actions follow only one path; this contributes to L13.
- **Repair previews and Undo:** describe actual recoverable artifacts and affected files, protect selected restore sources, and report quarantine failures. Cache quarantine does not immediately free the bytes its `bytes_freed` count suggests (L01, L08).
- **Search health:** expose full-text readiness, last successful build, changed/missing source counts and rebuild progress. `populate_from_library()` drops all prior root rows before rebuilding (`ficary/library/fulltext.py:455`), so an interrupted rebuild leaves only partial results; a staged or incremental rebuild would preserve last-known good search while completing.
- **Reorganization preview quality:** preview existing on-disk target conflicts as well as multiple planned sources sharing a destination; current `plan_with_conflicts()` explicitly defers pre-existing targets to apply-time skips. Keep a move journal if offering undo; restoring the JSON index alone cannot move files back.
- **Matching limits:** the mirror prefilter groups by the first two non-stopword title tokens (`ficary/library/mirrors.py:352`) before running content/author comparisons. Renamed mirrors or title word-order changes can be excluded before their otherwise strong signals are evaluated. Surface this limitation and consider author-based candidate expansion before increasing algorithmic complexity.

## Coverage matrix and limits

| Surface | Work performed | Result / limit |
| --- | --- | --- |
| JSON persistence and backups | Read load/save/schema/migration/backup/restore paths; isolated malformed-file, competing-writer and retention-limit checks | L01, L11, L12; no real index was loaded or restored by reproductions |
| Scan/identify/metadata state | Read scanner, identifier, candidate and record merge paths; real exported HTML scan/rescan/rename cases | L06, L13; real library trees were not scanned |
| Refresh/doctor/heal | Read local count cache, status/abandoned gates and integrated repair orchestration; actual file rewrite and doctor checks | L07, L08, L13; root agent owns detailed CLI orchestration and restore handler coverage |
| Watch persistence/polling | Read all watch models, CRUD, polling, cooldown, notifications and background lifecycle; deterministic edited-row/event/failure cases | L02–L05; no actual notifications or live remote calls |
| Watch download integration | Real closure/queue/single-flight with isolated prefs, mocked download boundary and controlled future | L09, L10; preference omissions coordinated with UX audit |
| Silent-edit hashing | Read hashes/content diff and scanner; real FFN cache and download engine with mocked upstream | L16; no live-site freshness or auth verification |
| Full-text search | Read SQLite schema/transaction/search/bootstrap; create actual isolated DB, index actual HTML, move/remove metadata and query | L15; no large-library performance benchmark |
| Reorganization/templates | Read planning/apply/collision/path constraints; actual temporary move and Unicode filename create | L14, L15; no Windows, NFS or removable-drive execution |
| Browser/review/abandonment | Read browser data loading, adult/abandoned actions, deletion/rescan/re-export and URL promotion; state-layer reproductions | L06, L13; UX agent owns live GUI/accessibility observations |
| Mirrors/find/stats | Read normalization, candidate bucketing, comparison and reporting logic | Matching limitation recorded as opportunity, not an independently reproduced release blocker |
| Heal manifest retention | Read manifest snapshots, listing, pruning and restoration metadata | No separate confirmed defect added; second-resolution ordering and multi-file restoration warrant future interruption/fault-injection coverage |

Priority scale: P1 = high-impact recovery/data-loss defect; P2 = actionable functional/reliability defect; UX items above are recommendations, not additional defect counts. Confidence “verified” refers to local deterministic evidence, not unperformed live site or platform testing.

## Progress and limits

- Enumerated library and related persistence modules (approximately 10,000 lines).
- `/home/matt/AGENTS.md` is absent; no repository `AGENTS.md` or `CLAUDE.md` found. Read applicable `/home/matt/CLAUDE.md` (homelab conventions); this repository audit requires no infrastructure changes.
- Root agent runs the baseline suite; this audit uses targeted reproductions rather than duplicating it.
- Completed the remaining browser/template/mirror review and recorded the explicit limits above. The final additional real-scraper check confirms cached silent-edit false negatives (L16).
- Both diagnostic scripts and logs are persisted under `evidence/`; the final integration log includes the corrected in-flight-future reproduction (a completed future is deliberately not joined, which was verified and excluded as a false-positive scenario).
