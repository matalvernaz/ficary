# Ficary audit — 2.19.0

Completed **2026-09-13**. Audit began 2026-09-08; the directory retains that start date. Source snapshot: `8f79e57399686357fcfead0e9fce29e70790beb1`, repository `/home/matt/ffn-dl`.

**Recommendation: prioritize a stabilization release before expanding features.** The audit documents **50 confirmed findings: 15 P1 and 35 P2**. The most consequential failures overwrite existing files, lose requested prose, discard settings or recovery points, violate selected speech-provider boundaries, and silently abandon background work. The existing test suite passes, so its current coverage does not guard these behaviors.

These are 50 documented findings, including related failure modes across layers, not 50 independent architectural problems. Five distribution observations, installed dependency advisories, static concerns and proposed features are tracked separately. P1 means high-impact data/privacy/recovery failure or a blocked supported/default workflow; P2 means a functional or reliability defect with a narrower trigger. Priority describes repair urgency, not exploitability or prevalence.

Application code, dependency versions and real user data are unchanged. The deliverable is the audit, diagnostic evidence, and repair backlog. No fixes, release publication, external messages, paid requests or production migrations were performed.

## Findings that should drive the next release

| Risk | Verified behavior | Findings |
| --- | --- | --- |
| Existing books can be overwritten | Two distinct works with the same title/author resolve to one output file. Updating a renamed book through the CLI can overwrite and then remove an unrelated book. An explicit HTML update can replace an EPUB while retaining its extension. GUI Update File leaves a renamed original stale and writes a second file. | [CORE-01](CORE.md#core-01), [UX-08](UX.md#ux-08) |
| A successful book can omit prose | A warm ordinal cache turns A,B → A,INSERTED,B into A,B,B. ScribbleHub's failed full TOC request can turn recent chapters 8/9 into a supposedly complete two-chapter book. Failed speech segments are omitted and the incomplete audio is reused from the successful cache. | [CORE-02](CORE.md#core-02), [CORE-03](CORE.md#core-03), [AUD-A06](AUDIO.md#aud-a06) |
| Privacy settings are not enforced throughout the path | SMTP TLS defaults do not verify the relay certificate. Failed local synthesis falls back to Edge without consulting the allowed-provider set. Debug response headers include synthetic session-cookie values verbatim. The reader also selects the first catalog voice without enforcing provider/language preferences. | [AUD-A01](AUDIO.md#aud-a01), [AUD-A03](AUDIO.md#aud-a03), [CORE-11](CORE.md#core-11); related P2 [AUD-A14](AUDIO.md#aud-a14) |
| First-run and routine desktop workflows break | Native Linux preferences try to use the data directory as a file and fail to persist. Closing a running search leaves global busy state set. Authentication fields have zero height in the default GTK Preferences dialog. | [UX-01](UX.md#ux-01), [UX-02](UX.md#ux-02), [UX-03](UX.md#ux-03) |
| Recovery can destroy the chosen recovery point | Restoring the oldest of ten retained backups first creates/prunes a new backup, deleting the selected source before reading it. Current index contents survive this failure, but that selected snapshot does not. Cache quarantine has a separate delete-on-move-failure defect. | [L01](LIBRARY.md#l01); related P2 [L08](LIBRARY.md#l08) |
| Source identity and supported installation paths are unreliable | Merged SubscribeStar works share ID 0 and a creator URL that cannot round-trip through update. A real Python 3.9 import fails despite declared support. A nested Unix Piper archive fails installation, after which a directory is accepted as the installed executable. | [CORE-05](CORE.md#core-05), [CORE-08](CORE.md#core-08), [AUD-A02](AUDIO.md#aud-a02) |

The privacy findings use synthetic credentials and mocked synthesis. They demonstrate the incorrect configuration/call paths; they do not establish that an actual account or story was disclosed. Native layout/settings evidence applies to the tested Linux wx/GTK environment; platform-specific consequences are identified separately.

## Complete confirmed finding index

Each linked section contains source references, reproduction evidence, impact and a targeted correction. [findings.json](findings.json) provides the same 50 stable IDs, open status, evidence paths, repair grouping and an individual acceptance check.

### Download, export and CLI — 11

| ID | Priority | Defect |
| --- | --- | --- |
| [CORE-01](CORE.md#core-01) | P1 | Output collisions overwrite other stories; update staging and format changes can destroy existing artifacts. |
| [CORE-02](CORE.md#core-02) | P1 | Ordinal chapter caches reuse the wrong body after TOC insertion/reordering. |
| [CORE-03](CORE.md#core-03) | P1 | A failed ScribbleHub full TOC request silently produces an incomplete book represented as complete. |
| [CORE-04](CORE.md#core-04) | P2 | SubscribeStar CLI audio raises `NameError`; its progress callback also has the wrong arity. |
| [CORE-05](CORE.md#core-05) | P1 | Merged SubscribeStar works lose unique, updateable source identity. |
| [CORE-06](CORE.md#core-06) | P2 | Google Doc plain text is interpolated as HTML, losing literal text in angle brackets. |
| [CORE-07](CORE.md#core-07) | P2 | Shared URL extraction/classification omits ScribbleHub and SubscribeStar story forms. |
| [CORE-08](CORE.md#core-08) | P1 | Declared Python 3.9 support fails on import in an actual Python 3.9 environment. |
| [CORE-09](CORE.md#core-09) | P2 | The pip console entrypoint rejects no arguments instead of opening the documented GUI. |
| [CORE-10](CORE.md#core-10) | P2 | Same-URL deduplication discards a deliberate second output format or destination. |
| [CORE-11](CORE.md#core-11) | P1 | Debug fetch diagnostics write complete session-cookie response headers. |

### Library, recovery and watches — 16

| ID | Priority | Defect |
| --- | --- | --- |
| [L01](LIBRARY.md#l01) | P1 | Restore evicts the oldest selected snapshot before reading it. |
| [L02](LIBRARY.md#l02) | P2 | Poll completion overwrites watch edits made while the poll was running. |
| [L03](LIBRARY.md#l03) | P2 | Failed auto-downloads advance observation state and never retry; the error later disappears. |
| [L04](LIBRARY.md#l04) | P2 | Returned notification failures are ignored and the undelivered alert is consumed. |
| [L05](LIBRARY.md#l05) | P2 | Autopoll off/on during an active poll leaves polling stopped while enabled. |
| [L06](LIBRARY.md#l06) | P2 | Rescans erase manual adult and abandoned classifications. |
| [L07](LIBRARY.md#l07) | P2 | Doctor updates file signatures without refreshing the metadata those signatures validate. |
| [L08](LIBRARY.md#l08) | P2 | Quarantine move failures fall back to irreversible cache deletion. |
| [L09](LIBRARY.md#l09) | P2 | Fresh watch downloads bypass configured library/adult routing and use the working directory. |
| [L10](LIBRARY.md#l10) | P2 | A watch joining a successful GUI download treats its `None` result as failure and receives no saved paths. |
| [L11](LIBRARY.md#l11) | P2 | Two initially empty index instances can silently overwrite one another's first saves. |
| [L12](LIBRARY.md#l12) | P2 | Invalid UTF-8 or malformed nested index data crashes recovery-facing consumers. |
| [L13](LIBRARY.md#l13) | P2 | Doctor removes a story whose primary is missing even when a tracked duplicate survives. |
| [L14](LIBRARY.md#l14) | P2 | Character-count filename limits allow multibyte names that exceed filesystem byte limits. |
| [L15](LIBRARY.md#l15) | P2 | Reorganization/removal leave stale full-text paths and deleted stories in results. |
| [L16](LIBRARY.md#l16) | P2 | Silent-edit scans use warm cached chapter bodies and miss upstream edits. |

### Desktop workflows and UX — 9

| ID | Priority | Defect |
| --- | --- | --- |
| [UX-01](UX.md#ux-01) | P1 | Linux source/pip preference saves collide with the `.ficary` data directory. |
| [UX-02](UX.md#ux-02) | P1 | Closing a running search leaves the application globally busy. |
| [UX-03](UX.md#ux-03) | P1 | Required credential controls collapse to zero height in default GTK Preferences. |
| [UX-04](UX.md#ux-04) | P2 | Default search windows clip site filters horizontally. |
| [UX-05](UX.md#ux-05) | P2 | Library/watch jobs drop saved Cloudflare-solver and newer-site credential settings. |
| [UX-06](UX.md#ux-06) | P2 | Fetching Audiobookshelf libraries performs synchronous network work on the GUI thread. |
| [UX-07](UX.md#ux-07) | P2 | Fetch libraries persists edited connection settings before OK, so Cancel does not discard them. |
| [UX-08](UX.md#ux-08) | P1 | GUI Update File fails to preserve the exact pathname of a renamed book. |
| [UX-09](UX.md#ux-09) | P2 | The first-run manual and shortcut help describe controls/actions that moved. |

### Reader, speech, audio and integrations — 14

| ID | Priority | Defect |
| --- | --- | --- |
| [AUD-A01](AUDIO.md#aud-a01) | P1 | SMTP_SSL and STARTTLS use contexts without certificate/hostname verification. |
| [AUD-A02](AUDIO.md#aud-a02) | P1 | Piper's nested Unix archive installation fails and accepts a directory as the executable. |
| [AUD-A03](AUDIO.md#aud-a03) | P1 | Local-only synthesis can fall back to a cloud voice. |
| [AUD-A04](AUDIO.md#aud-a04) | P2 | App voice starts at the chapter beginning and does not preserve listening progress. |
| [AUD-A05](AUDIO.md#aud-a05) | P2 | Deferred voice resolution can start speech after Stop or a mode switch. |
| [AUD-A06](AUDIO.md#aud-a06) | P1 | Failed speech segments/chapters are omitted and incomplete bodies enter the success cache. |
| [AUD-A07](AUDIO.md#aud-a07) | P2 | Final assembly ignores cancellation; LLM preprocessing lacks cancellation checkpoints. |
| [AUD-A08](AUDIO.md#aud-a08) | P2 | An obsolete soundscape worker can stop the newly selected soundscape. |
| [AUD-A09](AUDIO.md#aud-a09) | P2 | LLM failure-key mismatch caches fallback attribution as success and prevents retry. |
| [AUD-A10](AUDIO.md#aud-a10) | P2 | LLM attribution and note-stripping configuration are incorrectly coupled. |
| [AUD-A11](AUDIO.md#aud-a11) | P2 | Concurrent renders overwrite and clear one another's shared Cancel control. |
| [AUD-A12](AUDIO.md#aud-a12) | P2 | A lower-level live-TTS race starts playback after Stop already returned. |
| [AUD-A13](AUDIO.md#aud-a13) | P2 | Replacing naturally completed TTS controllers leaks temporary chapter audio. |
| [AUD-A14](AUDIO.md#aud-a14) | P2 | The reader chooses the first installed voice regardless of provider/language preference. |

The two Stop findings cover different layers and need both checks. The CLI/GUI update findings have different broken paths but should share one final destination contract. Credential omissions are counted only under UX-05, output-intent deduplication only under CORE-10, and shared story identity primarily under CORE-05.

## UX and missing-feature recommendations

Keep the native-control and library-first foundation. Existing named inputs, keyboard selection, menus and focus restoration are useful. Concentrate product work on making user intent, progress, failure and recovery visible. The items below are proposals or explicitly identified static gaps; they are not added to the 50 confirmed findings.

| Order | Bounded change | User outcome and acceptance |
| --- | --- | --- |
| 1 | Accessible Jobs list using the existing queue | Show story, requested format/destination, queued/running/failed/cancelled state and saved-file action. Support Cancel pending and Retry failed. A failed job remains discoverable after its log scrolls away; screen readers receive a concise status change. Durable queue restart/resume can be scoped separately. |
| 2 | Reader position and transport | Persist listening position; offer Resume/Restart, a named voice/provider choice, visible loading/playing/paused/failed state, Find in chapter and chapter titles. Stop must invalidate every pending playback path. Add Cancel sleep timer and a visible remaining duration; avoid a modal expiry announcement that can wake a listener. |
| 3 | Preferences that fit and save predictably | Scroll long pages or separate site connections, keep controls reachable at large text sizes, show failed saves, and commit edits only on OK. Fetch/Test actions use draft settings asynchronously. Include explicit optional-feature/provider availability. |
| 4 | Watch editing, history and retry | Edit an existing watch's label, channels, filters and auto-download choice without deleting its history. Add Watch this search carrying active filters. Show last successful poll, save and delivery separately, configured channel state, pending failures and a local recent-updates list. |
| 5 | First-run library navigation | Empty-state actions: Add story, Choose library folder, Import existing folder. Show the active destination. Distinguish Focus library from Manage library. Verify the guide from a fresh profile using only its written shortcuts. |
| 6 | Search controls and local progress | Use wrapping/shorter filter rows, an advanced section, explicit checked-result count and select all/none. Show per-site partial failures and Cancel in the search window. Make FFN fandom selection single-select until its adapter handles multiple choices. |
| 7 | Library copies and trustworthy repair | List all formats/copies and the primary update target; offer Promote Copy. Preview affected files and recovery locations before repair/reorganization, with accurate quarantine/reclaimed-space wording. Preserve manual classifications and expose full-text index freshness/rebuild state. |
| 8 | Desktop parity for existing capabilities | Extend the batch picker to local text files and pasted multiline URLs; expose optional chapter ranges under advanced download options. Provide SMTP connection testing and Send to Kindle for an existing export. Match documented actions to actual controls. |
| 9 | Soundscape editing | Add preview and Save/Discard behavior, preserve assignments across rename, and use unique imported asset names. Static inspection shows basename/slug collisions can replace other assets/definitions. Reverb room size is editable but its backend method is `pass`; expose availability accurately until implemented. |
| 10 | Redacted diagnostics and integration identity | Offer a copyable version/path/writability/feature-status report after fixing log redaction. Link existing repair operations. Give repeated Audiobookshelf exports an explicit update-versus-new choice; the current upload API documents duplicate creation. |

Further static concerns are retained in [UX.md](UX.md#static-operational-concerns-requiring-targeted-follow-up), [AUDIO.md](AUDIO.md#additional-ux-missing-feature-and-hardening-observations), and [LIBRARY.md](LIBRARY.md#targeted-ux-and-feature-work): batch paths bypass per-site serialization; some first-run triggers bypass destination selection; malformed nested search preferences can break restoration; sparse chapter ranges/cache layouts do not map cleanly into the reader; decoder and optional installer cancellation is incomplete; interrupted full-text rebuilding can discard the previous complete search projection; renamed mirrors can be excluded by the title prefilter.

## Repair sequence and verification gates

Use small patches per invariant. The groups below organize dependencies; they are not instructions to combine every row into one large change. Every confirmed finding is assigned to exactly one group in [findings.json](findings.json).

| Group | Scope | Acceptance gate |
| --- | --- | --- |
| R01 | Export destinations and source identity: CORE-01/05, UX-08 | Preserve both same-title works, update only the selected file, retain correct format/extension, and round-trip distinct merged-work identities through index/update/reading state. |
| R02 | Privacy and connection boundaries: CORE-11, AUD-A01/03/14 | Reject untrusted/wrong-host SMTP certificates, invoke no cloud synthesizer under local-only settings, and exclude synthetic secrets from logs. Reader selection follows the same provider policy. |
| R03 | Chapter and speech completeness: CORE-02/03/06, L16, AUD-A06 | Check expected source chapter identities and requested speech coverage. Failed/partial results cannot become complete cache entries. Warm-cache retry and upstream edits retain all intended prose. |
| R04 | Persistent settings and launch/site contracts: UX-01, CORE-07/08/09 | New-instance/restart settings reads succeed; a clean installation works at the declared Python floor; all entrypoints dispatch consistently; registered sites round-trip through common URL flows. |
| R05 | Conservative library recovery: L01/06/07/08/11/12/13/14/15 | Preserve selected backups, manual metadata and surviving copies. Move failures preserve source data. Malformed storage returns a recoverable state. Competing first writers cannot silently lose entries. FTS results follow current files. |
| R06 | Download intent and watch outcomes: CORE-10, UX-05, L02/03/04/05/09/10, AUD-A11 | Use consistent settings/destinations and structured saved/failed/cancelled results across callers. Preserve user edits and pending side effects through retries. Distinct export requests and render cancellation remain distinct. |
| R07 | Desktop lifecycle/layout: UX-02/03/04/06/07/09 | Closed windows cannot strand global busy state; late callbacks cannot clear newer work. All controls fit/scroll; slow requests leave input responsive; Cancel discards drafts; help matches behavior. |
| R08 | Reader and soundscape ownership: AUD-A04/05/08/12/13 | Resume from saved position, invalidate stale callbacks and playback, stop only owned audio handles, and dispose completed controllers/resources on replacement. Use event/barrier tests for the observed races. |
| R09 | Audio installation, cancellation and LLM configuration: CORE-04, AUD-A02/07/09/10 | Validate real package layouts and executable files; honour cancellation before final success/upload; retry failed attribution; test all four independent LLM feature-setting combinations and the actual CLI callback signature. |

Start with P1 findings inside these groups; include directly coupled P2 fixes such as reader provider selection when enforcing the provider boundary. Follow with watch/recovery/reader reliability, then the bounded UX additions. Correct installation/help mismatches alongside the behavior they describe. Gate a stabilization release on the platform checks below.

The recurring design issues are ownership and success contracts: a window drops a callback that owns global busy state; one job clears another's cancellation event; a poll saves stale user fields; a completed observation is mistaken for delivered output; a filename or ordinal substitutes for source identity. Repair these contracts at their existing boundaries. Atomic file writes, a single-flight registry or a generation counter only protect the specific invariant their caller actually enforces.

## Verification and release assessment

| Check | Actual result | Interpretation |
| --- | --- | --- |
| Existing suite, 2026-09-08 | **2,190 passed, 13 warnings, 88.81 seconds**, zero failures/errors/skips in JUnit | Offline baseline under Xvfb/native GTK, with isolated app persistence. Passing tests do not cover all integration contracts above. |
| Statement coverage | **62.34%**, 17,821 / 28,589 statements | Reader GUI and soundscape editor: 0%; dialogs: 15%; neural environment: 23%; CLI: 26%. Coverage guides missing checks; it is not a quality score. |
| Targeted diagnostics | Local source/real exporter/cache/index/queue/native-control paths with synthetic inputs and controlled races | Probe exit 0 means the incorrect behavior was reproduced, not repaired. UX-09 is a direct documentation/code comparison. Network, audio and provider boundaries are mocked where described. |
| Ruff `F,E9` | 13 diagnostics, including reproduced undefined `generate_audiobook` | Another F821 is a quoted annotation; do not present every lint message as a runtime bug. |
| Python floor | Actual Python 3.9.25 CLI import raises `TypeError` | Confirms CORE-08. The ordinary tested environment uses Python 3.13.5; release workflows use 3.12. |
| Dependency inventory, 2026-09-13 | 70 raw records, **37 distinct package/advisory-ID pairs across 11 installed packages** | The September 8 scan has the same 37 distinct pairs with fewer duplicate records. This is not a count of exploitable Ficary flaws. |
| `pip check`, 2026-09-13 | `nvidia-cusparselt-cu13 0.8.0 is not supported on this platform` | Local ARM64 optional-environment inconsistency; does not establish that a released package fails. |
| Repository | Same audited commit; only `audits/` added | No application fix is implied by the saved probes. |

Detailed release and dependency conclusions, with current primary-source links, are in [DISTRIBUTION.md](DISTRIBUTION.md):

- **DIST-01:** Apple Silicon packaging downloads Intel FFmpeg helpers; the source mismatch is confirmed, actual release execution is untested.
- **DIST-02:** A moving Linux build host does not establish the older supported-OS compatibility promised in the README.
- **DIST-03:** macOS installation instructions name an archive/layout different from the current workflow.
- **DIST-04:** Release workflows lack a dependency on successful tests for the source revision; packaged smoke tests only exercise `--help`.
- **DIST-05:** Floating dependency/helper inputs and incomplete artifact pinning limit reproducibility. Existing ZipExtractor digest verification and updater journal/size/digest checks are useful protections to preserve.

Prioritize dependency assessment by actual use: Edge's aiohttp client path merits review, while server-only advisories are not automatically reachable in a desktop client. SoupSieve selector issues require untrusted selectors, not merely downloaded HTML. Optional ML libraries and build tools need inventory separate from the ordinary packaged runtime. No dependency upgrades were applied; [DISTRIBUTION.md](DISTRIBUTION.md) records the evidence and limits.

## Coverage boundaries and remaining validation

The completed pass covers the major source workflows: site dispatch/fetch/cache/export/update, CLI and entrypoints, GUI/search/preferences/queues, library scan/persistence/doctor/reorganization/full-text, watches/delivery, reader/state/playback/soundscapes, audiobook synthesis/attribution/providers/installers, SMTP/Audiobookshelf, manifest/build/self-update paths and documentation.

The repository has substantial fixture tests, atomic export writes and locking, shared queue primitives, native controls, and updater/recovery mechanisms. These are concrete existing assets. The findings show where their integration does not yet preserve the intended contract. The audit does not claim every line, every scraper response, or every supported platform was exhaustively exercised.

| Remaining validation | Required evidence before claiming support |
| --- | --- |
| Accessibility and low vision | Complete first download, search, watch, update, reading, cancellation and recovery with NVDA/JAWS on Windows, VoiceOver on macOS and Orca on Linux. Record labels, focus, dynamic announcements, large fonts, high contrast and 150–200% scaling. GTK geometry checks do not establish speech behavior. |
| Packaged platforms and upgrades | Clean-profile first launch, settings reopen, export/read, bundled helper execution, short synthetic M4B, and previous-version upgrade on each named supported OS/architecture. Include interrupted update/recovery in disposable installations. |
| Live site compatibility | Public and authenticated fixtures against each supported site using authorized test accounts; blocked/expired login, rate limits, full TOCs, deleted/reordered chapters and changed markup. No live authenticated site was contacted in this pass. |
| Real audio and providers | Actual devices, pause/Stop, sleep/auto-advance, provider catalog/language selection, offline failure, install interruption and quality on a human-labelled prose corpus. No paid synthesis/LLM call or listening-based quality test was performed. |
| External delivery | Test SMTP certificate rejection and delivery, notification retries and Audiobookshelf identity/cancellation against controlled test services. No real email, alert or upload was sent. |
| Scale and storage faults | Synthetic large-library timings, high watch counts, queued work, low disk space, read-only/unplugged destinations, file locks, Unicode paths and cross-process writers. No large-library or removable/network-filesystem benchmark was performed. |
| Release dependency and license inventory | Record exact shipped and optional-backend artifacts, evaluate applicable advisories, and review dependency/license notices. The development environment is not a substitute for an inspected release archive or a legal-compliance assessment. |

These limits are outstanding validation tracks, not hidden passes or extra bug counts.

## Audit artifacts and resumption

- [CORE.md](CORE.md), [LIBRARY.md](LIBRARY.md), [UX.md](UX.md), [AUDIO.md](AUDIO.md): detailed findings and source references.
- [DISTRIBUTION.md](DISTRIBUTION.md): build/install/dependency/test assessment.
- [findings.json](findings.json): actionable backlog with acceptance checks and evidence links.
- [evidence/README.md](evidence/README.md): diagnostic inventory, baseline environment and reproduction commands.
- [PROGRESS.md](PROGRESS.md): completed checkpoint, repository baseline and instructions for safely resuming repair work.

Source references are tied to the audited commit. Before a later repair, compare the current code to that snapshot, reproduce the selected finding in temporary data, make the targeted change, then convert its diagnostic into an assertion of the correct behavior. Keep a finding open until that acceptance check and relevant existing tests pass.
