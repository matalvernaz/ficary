# Download, export, CLI and distribution audit

Status: complete for this audit pass; consolidated 2026-09-13. Snapshot `8f79e57399686357fcfead0e9fce29e70790beb1`, version 2.19.0. Application code unchanged; synthetic probes operate in temporary directories.

## Verified export and scraper findings

Evidence: [core_repros.py](evidence/core_repros.py), [results](evidence/core_repros.json). Run `.venv/bin/python audits/2026-09-08/evidence/core_repros.py` from the repository.

<a id="core-01"></a>

### CORE-01 — P1: filename collisions overwrite different stories; update staging can delete an unrelated book

`ficary/exporters.py:19`, `:108`, `:614`, `:702` use title/author as the default identity and atomically replace the destination without checking its source URL. Atomic writes and per-path locks prevent torn files, but do not prevent one story replacing another. Two synthetic works, IDs 101 and 202, produce one file containing only 202.

The CLI update path is worse: `ficary/cli.py:1107` exports under the generated name first, then `:1130-1138` replaces the original user-selected name. With an existing user-renamed story and an unrelated book already at the generated name, updating the former destroys the latter. Probe: success=true, unrelated_book_survives=false, updated_file_chapters=2.

Fix: choose the final path before export. For updates, atomically replace only the selected update target. For fresh downloads, compare canonical source identity before overwriting; otherwise allocate a unique source/id-qualified name or surface a conflict. Test distinct URLs sharing metadata and updating a hand-renamed file beside a colliding title. Do not change every existing filename as a prerequisite.

<a id="core-02"></a>

### CORE-02 — P1: ordinal chapter caches substitute old content after insertion/reordering

`ficary/scraper.py:1221-1231` loads cached chapter N without comparing the current source URL or stable chapter ID; `:1338-1358` stores only title/body. `_materialise_chapters` is used by Royal Road, FicWad, MediaMiner, and ScribbleHub. An initial TOC A,B followed by A,INSERTED,B returns bodies A,B,B; only B is fetched. The new chapter is lost and the existing chapter duplicated. This is distinct from intentional reuse for silent textual edits: the chapter identity changed.

Fix: key cache by stable site chapter ID/canonical chapter URL and preserve the requested ordinal separately. Record table-of-contents identity in update metadata so insertion/removal cannot be mistaken for append-only growth. Wattpad/Webnovel already have stable-key cache support to follow. Test insertion, deletion, and reordering between runs.

<a id="core-03"></a>

### CORE-03 — P1: ScribbleHub TOC failure silently turns a recent-chapter excerpt into a complete book

`ficary/scribblehub.py:150-199` treats any failed/blocked/empty AJAX response as a reason to return embedded recent chapters, and `:244-280` persists that fallback length as the story chapter count. A synthetic embedded list of chapters 8 and 9 plus HTTP 503 returns two chapters without error. `_materialise_chapters` subsequently numbers those chapters 1 and 2, contaminating cache ordinals and export metadata.

Fix: distinguish a complete TOC from a partial fallback. Refuse normal complete-book export when enumeration fails, or explicitly label an opt-in partial artifact with source chapter identities and a resumable incomplete state. Do not cache the excerpt under full-book ordinal keys.

<a id="core-04"></a>

### CORE-04 — P2: SubscribeStar CLI audiobook command raises NameError; its progress callback also has the wrong arity

`ficary/cli.py:547` calls `generate_audiobook` without importing it. The real handler with a mocked successful scraper raises `NameError: name 'generate_audiobook' is not defined`. `:549` additionally defines a four-argument audio progress callback, whereas normal audio progress callbacks receive three (`:1090`, `ficary/tts.py` callers). Merely adding the import leaves a second failure.

Fix: use the same import and callback signature as the normal audiobook path, and exercise this CLI dispatch with a fake generator that calls the callback. Ruff detects the missing import; the baseline CI does not run Ruff. The other F821 on quoted `LibraryIndex` at cli.py:3071 is annotation hygiene, not an observed runtime crash.

<a id="core-05"></a>

### CORE-05 — P1: merged SubscribeStar stories lose their source identity and cannot be updated correctly

`ficary/subscribestar.py:398-405` gives every merged work `id=0` and the same creator URL. Two different synthetic works return identical IDs and URLs. The same scraper's `parse_story_id` rejects that creator URL (`:105`), so an exported work's source cannot round-trip through single-file updating. Indexing/deduplication and reading-state keys also conflate works from the same creator. The synthetic per-story URL already exists (`:263-266`); it is discarded in the exported Story.

Fix: persist the synthetic per-work URL and a stable source-qualified identity, and provide chapter-count/update semantics for it. Preserve identity through export/import and migration; avoid process-random hash values. `merge_stories` also uses id=0 for all series; see the audio report for sidecar collision implications.

<a id="core-06"></a>

### CORE-06 — P2: Google Doc plain text is reinterpreted as HTML and loses literal content

`ficary/subscribestar.py:252-258` obtains text with `get_text()` and interpolates it directly inside `<p>` without HTML escaping. The source text `The literal &lt;secret&gt; vanishes.` becomes `<p>The literal <secret> vanishes.</p>` and renders without the word in angle brackets. The probe demonstrates lost prose. Reinterpreting text as active markup is a further code-based risk; browser execution was not tested.

Fix: escape plain text when rebuilding paragraphs, or retain a sanitized set of original inline elements. Test literal angle brackets, ampersands, and quoted markup as prose.

<a id="core-07"></a>

### CORE-07 — P2: newer sites are absent from the shared paste/clipboard story registry

`ficary/sites.py:47-214` defines story regexes without ScribbleHub or SubscribeStar, though host detection and ALL_SCRAPERS include both (`:240`, `:273`). `extract_story_url` returns None for valid ScribbleHub series and SubscribeStar post URLs; `url_classifier.classify` labels both unknown. Direct host-based downloading and fixture tests pass, masking failure in clipboard-watch and common URL-list workflows.

Fix: add supported story shapes to the shared story registry and pin a capability-contract test per registered scraper: host detection, story extraction, classification, canonical URL, and source URL round-trip.

<a id="core-08"></a>

### CORE-08 — P1: declared Python 3.9 support fails at import

`pyproject.toml:19` accepts Python >=3.9, but `ficary/exporters.py:49` evaluates a PEP 604 union annotation without postponed annotations. A clean temporary Python 3.9.25 environment with the declared core dependencies installed fails `import ficary.cli` with `TypeError: unsupported operand type(s) for |: '_CallableGenericAlias' and 'NoneType'`. This is an actual interpreter run, not an inferred compatibility warning. CI tests only Python 3.13; release builders use 3.12.

Fix: establish the intended minimum Python version, declare that floor accurately, and run an import/CLI smoke test on it. Raising the declared floor to match supported syntax is smaller than retrofitting Python 3.9 compatibility, if 3.9 support is not intended. Do not claim 3.10 is fully supported solely because it supports this annotation; verify all imports and dependencies at the chosen floor.

Evidence: `evidence/python39_probe.py`, `.log`, `python39-deps.log`. Interpreter and dependencies installed only in `/tmp/ficary-audit-python`; the application's environment was not upgraded.

<a id="core-09"></a>

### CORE-09 — P2: the pip entry point rejects no arguments instead of opening the GUI

`pyproject.toml:40` registers `ficary.cli:main`, which always parses a CLI operation. The no-argument GUI dispatch exists separately in `ficary/__main__.py:45-61`. The actual installed `.venv/bin/ficary` also imports `ficary.cli.main`. Invoking the registered target with no arguments exits 2 with `ficary: error: either a URL, --batch FILE, --update FILE, or --author URL is required`, contrary to README Getting started.

Fix: expose one import-safe entrypoint that dispatches no arguments to the GUI and arguments to the CLI; use it for console scripts, module invocation and frozen builds. Account for `__main__.py` currently calling `main()` unconditionally when imported. Test each entry path with GUI dispatch mocked.

Evidence: `evidence/core_entrypoint_repros.py`, `.json` (`pip_no_args`).

<a id="core-10"></a>

### CORE-10 — P2: deduplication discards deliberate exports to another format or folder

`ficary/gui.py:1225` deduplicates jobs only by canonical story URL. Two requests for the same story, with different output destinations/formats, are joined even though the second job represents a different requested artifact. A deterministic real `_enqueue_site_job` probe blocks the first job, submits the second, and releases the first: both futures are identical and return `epub`; only the EPUB job runs.

Fix: separate reusable fetch identity from requested export identity. Include output path/format and behavior-affecting options in the job equivalence rule, or explicitly reject incompatible duplicate requests in the UI. Continue coalescing identical accidental double-clicks. A single result type containing saved paths also addresses L10.

Evidence: `evidence/core_entrypoint_repros.json` (`different_output_deduped`). This verifies the concern listed statically in UX.md; count it once here.

<a id="core-11"></a>

### CORE-11 — P1: debug diagnostics write session cookies into logs

`ficary/scraper.py:466-499` strips values from the cookie-jar summary but logs every response header unchanged. `Set-Cookie` therefore includes full session values. A synthetic successful response with `Set-Cookie: session=SYNTHETIC_SESSION_VALUE; Secure; HttpOnly` reproduces the value verbatim in the debug log.

Impact is conditional on debug logging and a response that sets a sensitive cookie. A support log or synced log folder can disclose authenticated sessions. This does not assert that a real secret was inspected or leaked during the audit.

Fix: redact cookie/authentication headers at the diagnostic boundary and sanitize URLs/body excerpts before offering log export. Preserve useful status, server/challenge metadata and cookie names. Pin a log test using synthetic secrets in headers, query parameters and errors.

Evidence: `evidence/core_logging_probe.py`, `.json`. No real session values used.

## Additional CORE-01 reproduction: changing format while updating corrupts the original extension

`_handle_update_file` accepts an explicit `-f` (`ficary/cli.py:4943-4944`), while `_download_one` always renames the finished artifact onto `update_path` (`:1137`). The isolated actual EPUB export/update probe uses `-u book.epub -f html`: exit code 0, original `.epub` now begins with `<!DOCTYPE html>`, and `zipfile.is_zipfile` is false. The original EPUB is replaced with another format while retaining its old extension.

Treat this as part of the update-destination safety fix in CORE-01: reject in-place format changes, or write the requested new format to its own extension while preserving the original. Evidence: `evidence/core_entrypoint_repros.json` (`update_format`).

## Coverage and limits

Reviewed CLI dispatch and download/update orchestration, exporter writes/metadata/markup preparation, shared scraper fetching/cache orchestration, site registry/classification, latest ScribbleHub/SubscribeStar integrations, series identity, self-update verification/staging/journal flow, optional feature and portable setup, manifest and CI/build workflows. The baseline exercises offline fixture parsers across supported adapters; this pass did not manually validate every line or contact every upstream site.

No new self-updater exploit is claimed: existing digest/size checks, Windows extraction checks and recovery-journal tests are meaningful. OS packaging compatibility and dependency inventory are separately documented in `DISTRIBUTION.md`. Live authenticated upstreams, paid providers, Windows/macOS release executables, interruption during a real self-update, and large-library performance remain explicitly unvalidated.

CORE-01 through CORE-07 received an independent source/evidence review; reference and wording corrections are incorporated. Other findings retain the exact diagnostic outputs above. These probes record failures; exit code 0 from a probe means the reproduction ran, not that the product defect is fixed.
