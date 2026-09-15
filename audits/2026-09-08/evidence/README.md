# Audit evidence

Baseline commit: `8f79e57399686357fcfead0e9fce29e70790beb1`, Ficary 2.19.0. Original verification began 2026-09-08; environment/dependency inventory was refreshed 2026-09-13 with application source unchanged. Start with [the consolidated report](../REPORT.md) and [finding registry](../findings.json).

## Interpretation

These diagnostic scripts reproduce the current incorrect behavior. **Exit code 0 means the probe completed, not that the finding is fixed.** Captured exceptions, false success values, missing output and controlled race outcomes are evidence. Some results use real exporters, cache files, SQLite, threads or native controls while substituting external boundaries. The matching report identifies exactly what was real and what was mocked.

Stories, credentials and upstream responses are synthetic. Writable library/cache/settings state uses temporary directories or in-memory test doubles. Network, delivery, synthesis and device boundaries are replaced where exercised. No real notification, paid provider request, authenticated scrape, upload, user-library repair or production preference write is part of these probes.

The scripts target private implementation details of the audited revision. Before reusing one on changed code, check that its temporary paths and substituted boundaries still match the new call path. They are not installed application commands or a regression suite.

## Diagnostic inventory

Run from `/home/matt/ffn-dl`. Set `PYTHONPATH=.` for probes importing local test helpers; native GUI probes require a display. Xvfb provides one on this Linux host.

| Script | Captured result | Finding coverage |
| --- | --- | --- |
| [core_repros.py](core_repros.py) | [JSON](core_repros.json) | CORE-01–07: exporter/update collisions, ordinal cache, partial TOC, CLI handler, source identity, escaping and URL registry. |
| [core_entrypoint_repros.py](core_entrypoint_repros.py) | [JSON](core_entrypoint_repros.json) | CORE-01/09/10: update format corruption, pip entrypoint and distinct-output queue deduplication. |
| [core_logging_probe.py](core_logging_probe.py) | [JSON](core_logging_probe.json) | CORE-11: synthetic cookie value in debug response headers. |
| [python39_probe.py](python39_probe.py) | [Log](python39_probe.log) | CORE-08: actual Python 3.9.25 import failure. |
| [library_repros.py](library_repros.py) | [Log](library_repros.log) | L01–08: retention, stale watch writes, failed side effects, poll lifecycle, manual marks and doctor/quarantine behavior. |
| [library_integration_repros.py](library_integration_repros.py) | [Log](library_integration_repros.log) | L06/09–16 and corroborating UX-05: watch closure/queue, scanner/index/doctor, file moves/FTS, Unicode filename and FFN warm-cache edit scan. |
| [ux_preferences_storage_probe.py](ux_preferences_storage_probe.py) | [JSON](ux_preferences_storage_probe.json), [expected native errors](ux_preferences_storage_probe.stderr) | UX-01: config path collision, ignored failed flush and new-instance settings loss. |
| [ux_native_probe.py](ux_native_probe.py) | [JSON](ux_native_probe.json) | UX-02/03/04: blocked search close/reopen and native default control geometry. |
| [ux_jobs_probe.py](ux_jobs_probe.py) | [JSON](ux_jobs_probe.json) | UX-05: preference-to-job-to-scraper constructor mapping. |
| [ux_abs_preferences_probe.py](ux_abs_preferences_probe.py) | [JSON](ux_abs_preferences_probe.json) | UX-06/07: GUI event-loop order and draft persistence on Cancel. |
| [ux_gui_update_probe.py](ux_gui_update_probe.py) | [JSON](ux_gui_update_probe.json) | UX-08: GUI update and TXT export leave a renamed original unchanged. |
| [audio-boundaries-probe.py](audio-boundaries-probe.py) | [Text](audio-boundaries-probe.txt) | AUD-A01/02/03: SMTP context inspection, nested fake Piper archive installation and cloud fallback selection. |
| [audio-reader-probe.py](audio-reader-probe.py) | [Text](audio-reader-probe.txt) | AUD-A04/05/13/14: native reader position, stale resolution, completed-controller resources and provider/voice selection. |
| [audio-cache-race-probe.py](audio-cache-race-probe.py) | [Text](audio-cache-race-probe.txt) | AUD-A06/07/08: missing speech cached as complete, late cancellation and obsolete soundscape worker. |
| [audio-attribution-probe.py](audio-attribution-probe.py) | [Text](audio-attribution-probe.txt) | AUD-A09: attribution failure keys and persistent fallback cache. |
| [audio-gui-probe.py](audio-gui-probe.py) | [Text](audio-gui-probe.txt) | AUD-A10/11: LLM configuration snapshot and shared Cancel ownership. |
| [audio-live-stop-probe.py](audio-live-stop-probe.py) | [Text](audio-live-stop-probe.txt) | AUD-A12: controlled stale playback after Stop. |

UX-09 is a source/documentation comparison in [UX.md](../UX.md#ux-09), with no separate runtime probe. Core/distribution reports also cite manifest/workflow contents directly.

Example diagnostic invocations:

```bash
cd /home/matt/ffn-dl
PYTHONPATH=. .venv/bin/python audits/2026-09-08/evidence/core_repros.py
PYTHONPATH=. .venv/bin/python audits/2026-09-08/evidence/library_integration_repros.py
PYTHONPATH=. xvfb-run -a .venv/bin/python audits/2026-09-08/evidence/ux_native_probe.py
PYTHONPATH=. xvfb-run -a .venv/bin/python audits/2026-09-08/evidence/audio-reader-probe.py
```

Other scripts use the same invocation shape with their filename. Use Xvfb for native controls, or an existing graphical display. Save later rerun output under a new name/date to retain the original evidence.

The Python 3.9 probe used `/tmp/ficary-audit-python/venv39/bin/python`. Its interpreter and declared core dependencies were installed only into a temporary environment. [Interpreter install](python39-install.log), [venv creation](python39-venv.log), and [dependency installation](python39-deps.log) logs identify that setup. Temporary files may disappear after cleanup/restart; reproducing the claim requires another isolated Python 3.9 environment, not changing the ordinary application interpreter.

## Existing suite and environment

The baseline used Python 3.13.5, pytest 9.0.3 and wxPython 4.2.3 / GTK3 / wxWidgets 3.2.7 on Linux ARM64. [run_baseline.py](run_baseline.py) redirects app bootstrap/configuration to temporary paths before invoking existing tests.

Historical command:

```bash
cd /home/matt/ffn-dl
xvfb-run -a .venv/bin/python audits/2026-09-08/evidence/run_baseline.py
```

This runner writes `coverage.json` and `pytest.xml` here. Preserve original snapshots before rerunning it. Original stdout is [pytest.log](pytest.log).

- [pytest.xml](pytest.xml): 2,190 tests, zero failures/errors/skips.
- [pytest.log](pytest.log): `2190 passed, 13 warnings in 88.81s (0:01:28)`; warnings concern unclosed SQLite connections.
- [coverage.json](coverage.json): 17,821 of 28,589 statements covered, 62.33516387421736%.
- [ruff.json](ruff.json): 13 diagnostics from the `F,E9` selection; reports distinguish runtime relevance.
- [environment-2026-09-13.json](environment-2026-09-13.json): versions of the inspected core/test packages.
- [pip-check-2026-09-13.log](pip-check-2026-09-13.log): unsupported optional NVIDIA package in this local ARM64 environment.
- [pip-audit.json](pip-audit.json), [original log](pip-audit.log): September 8 installed-environment scan.
- [pip-audit-2026-09-13.json](pip-audit-2026-09-13.json), [refreshed log](pip-audit-2026-09-13.log): September 13 scan. Raw duplicates differ; both contain 37 distinct package/advisory-ID pairs across 11 packages.

Dependency scans depend on the advisory feed at execution time. They describe the installed development environment, not an inspected released binary or proven exploit paths. See [DISTRIBUTION.md](../DISTRIBUTION.md) for applicability and primary sources.

The baseline was not repeated during consolidation because application source remained unchanged. No product tests are claimed to pass after a fix: no fix was made.

## Artifact verification

[audit-validation.json](audit-validation.json) records final finding/priority counts, link/anchor/evidence validation, baseline result checks and repository state. Local audit ignore rules explicitly retain text probe output and the coverage snapshot despite repository-wide generated-file exclusions.

The report is complete for its documented source/offline/native-Linux scope. Windows/macOS release execution, real screen-reader listening, live accounts/providers, physical audio output and large-library performance remain explicit follow-up validation tracks.
