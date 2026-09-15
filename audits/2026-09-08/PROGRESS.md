# Ficary audit progress

**Status: audit complete, 2026-09-13.** Start with [REPORT.md](REPORT.md). Findings remain open; application fixes were not requested or applied.

## Scope and baseline

Request: deep audit of bugs, missing features, UX and other material concerns, with durable progress so work can survive a session interruption.

Repository: `/home/matt/ffn-dl`. Version: `2.19.0`. Audited commit: `8f79e57399686357fcfead0e9fce29e70790beb1`. The source snapshot is unchanged at completion. The initial working tree was clean; the only additions are under `audits/`.

Applicable instructions: `/home/matt/CLAUDE.md` (homelab conventions); no repository `AGENTS.md` or `CLAUDE.md` found. No infrastructure action applies to this audit.

## Completed work and artifacts

| Workstream | Result |
| --- | --- |
| Download/export/CLI/site integration | [CORE.md](CORE.md): 11 confirmed findings, including destructive output/update paths, missing/incorrect content, source identity, launch support and diagnostic cookie disclosure. |
| Library/recovery/watchlists | [LIBRARY.md](LIBRARY.md): 16 confirmed findings, including snapshot eviction, failed side-effect retry, manual metadata loss, competing writers, doctor and full-text consistency. |
| Native desktop/UX | [UX.md](UX.md): 9 confirmed findings plus bounded product recommendations and an explicit accessibility matrix. |
| Reader/audio/integrations | [AUDIO.md](AUDIO.md): 14 confirmed findings plus static/missing-feature observations. Controlled thread races, native reader state and mocked synthesis/device boundaries. |
| Distribution/dependencies | [DISTRIBUTION.md](DISTRIBUTION.md): 5 separately classified distribution observations, dependency applicability assessment and release/test gaps. |
| Consolidation | [REPORT.md](REPORT.md): prioritized complete index, UX roadmap, repair groups, verification results, strengths and limits. |
| Actionable backlog | [findings.json](findings.json): all 50 confirmed findings, 15 P1 / 35 P2, unique IDs, evidence, repair group and individual acceptance check. All remain open. |
| Evidence | [evidence/README.md](evidence/README.md): 17 diagnostic scripts and captured results, baseline artifacts, environment scans and reproduction instructions. |

## Verification checkpoint

- Existing suite on 2026-09-08: **2,190 passed, 13 warnings, 88.81 seconds**. JUnit records zero failures/errors/skips. Native GTK/Xvfb with temporary application persistence.
- Statement coverage: **62.34%**, 17,821 / 28,589 statements. Important native/integration paths have low or no coverage.
- Targeted diagnostics demonstrate current defects; their successful exit is not evidence of a repair. Real files/cache/SQLite/queues/native controls are used where specified; external inputs and side effects are synthetic.
- Actual temporary Python 3.9.25 import confirms the declared minimum-version failure. The ordinary application interpreter/dependencies were not upgraded.
- September 13 dependency refresh: **37 distinct package/advisory-ID pairs across 11 installed packages**, represented by 70 raw records. The original scan's 41 raw records contain the same distinct pairs. Reachability and release-inventory limits are documented.
- `pip check` identifies an unsupported optional NVIDIA package on local ARM64. The baseline still passes; this is not a claim of a release-wide failure.
- Final artifact checks are recorded in [evidence/audit-validation.json](evidence/audit-validation.json): IDs/counts, references, baseline data and unchanged source state.

## Durable chronology

- **2026-09-08:** Identified the repository, read instructions and product/build inventory, isolated test persistence, ran the full suite and static/dependency checks, and saved download, library, native UX and audio findings with diagnostic outputs.
- **During continuation:** Work resumed after usage-limit interruptions. Extended reproductions cover destructive update-format conversion, same-URL/different-output deduplication, native preference persistence, watch/GUI integration, stale cache edit scanning, lower-level Stop races, controller cleanup and reader provider selection. Initial findings received reference/false-positive review.
- **2026-09-13:** Confirmed unchanged HEAD/application tree, refreshed environment/dependency inventory and current primary distribution/advisory sources, finalized detailed reports, consolidated priorities/UX proposals, and saved the machine-readable repair backlog and final evidence checks.

## Remaining validation and safe resumption

No consolidation work remains. The audit is complete within its documented source/offline/native-Linux scope. Windows/macOS packaged execution, assistive-technology speech sessions, authenticated live sites, physical audio/provider quality, controlled external delivery, storage fault/large-library performance and exact released dependency/license inventory remain explicit validation tracks in REPORT.md. They are not represented as passing checks or extra confirmed defects.

For later authorized repairs:

1. Read REPORT.md and the selected finding's detailed section and acceptance check.
2. Compare current HEAD and relevant files with the audited snapshot; preserve unrelated changes.
3. Reproduce in temporary directories with synthetic stories/credentials and controlled external boundaries. Do not use real library/cache/preferences as fixtures.
4. Make the smallest correction that restores the documented invariant. Convert the diagnostic into a regression check asserting correct behavior, then run relevant existing tests.
5. Update the finding status only when its acceptance check passes; record commit, test output and any remaining platform limits. Keep audit evidence from the original snapshot.

No outstanding process or reviewer is required to read or use the completed audit.
