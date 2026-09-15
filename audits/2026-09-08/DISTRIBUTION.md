# Distribution, dependencies, and release verification

Completed 2026-09-13. Repository remains at `8f79e57399686357fcfead0e9fce29e70790beb1` / 2.19.0. This section distinguishes reproduced application bugs (CORE-08/09), confirmed configuration mismatches, and release risks requiring platform execution. It does not claim downloaded release binaries were exercised.

## Distribution observations

| ID | Priority | Evidence and consequence | Recommended verification/fix |
| --- | --- | --- | --- |
| DIST-01 | P2, confirmed build-source mismatch; platform outcome untested | `.github/workflows/build-macos.yml:28` builds on Apple Silicon and fetches ffmpeg/ffprobe from Evermeet at lines 52/56. Evermeet identifies its binaries as Intel, and does not publish native Apple Silicon binaries. The workflow tests only Ficary `--help`; it does not execute ffmpeg/ffprobe. | Select matching architecture assets, verify with `file`/`lipo`, run both bundled executables and generate a short synthetic M4B on a clean Apple Silicon environment. An Intel helper requires a translation environment; whether the currently released app fails or prompts for translation on a particular Mac was not tested. |
| DIST-02 | P2, unverified compatibility claim | Linux builds use `ubuntu-latest` (`build-linux.yml:22`), but README promises Ubuntu 22.04+, Debian 12+, and Fedora 38+. A moving build host plus PyInstaller does not establish compatibility with older libc/GTK installations. | Build against the oldest supported ABI/runtime and launch the packaged GUI/CLI there. Record an explicit supported-OS matrix and test it. Do not assume this audit proves the current binary fails on all named systems. |
| DIST-03 | P2, confirmed documentation mismatch | `README.md:65-69` directs macOS users to `ficary-macos-arm64.tar.gz` and `./ficary/ficary`. Current workflow packages and releases `ficary-macos-arm64.zip` containing `ficary.app` (`build-macos.yml:110-123`); self-update also selects a macOS zip. | Correct the asset name, Finder launch steps, CLI path inside the bundle and minimum supported macOS. Include an offline copy of matching-version help in packaged builds if offline reading is an intended use. |
| DIST-04 | P2, release verification gap | Test workflow uses Python 3.13 on Ubuntu; release builds use 3.12 and separate workflows. Packaged smoke tests call `--help`, so reader startup, optional dependencies, native audio, update swap/restart and real settings persistence are outside the gate. Release workflows do not depend on successful completion of the test workflow. | Gate release publication on the intended source revision's tests and packaged smoke tests. Add temporary-profile first launch, export/read/reopen, helper execution and previous-version upgrade checks on each supported platform. |
| DIST-05 | P3, reproducibility/supply-chain gap | Runtime/build dependencies have broad lower bounds; build tools and external ffmpeg endpoints resolve changing artifacts. Windows ZipExtractor is positively pinned by SHA-256, but ffmpeg downloads are mainly size-checked and self-update permits an absent digest with a warning. | Capture a tested per-platform resolved dependency/artifact manifest and checksums with each release; verify upstream digests/signatures where available. Use planned update PRs rather than silently floating every component at release time. Preserve the existing ZipExtractor verification and size/digest/journal checks. |

Evermeet explicitly describes the architecture of its published binaries. [Evermeet FFmpeg distribution](https://evermeet.cx/ffmpeg/). PyInstaller documents that libc is not bundled and older-system compatibility requires building against the oldest supported environment. [PyInstaller platform compatibility](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible). These primary sources were checked on 2026-09-13; they support the build-risk assessment, not a claim of a tested release failure.

The Python 3.9 import failure and pip no-argument entrypoint failure are separately reproduced as CORE-08/09. They should be corrected before treating the installation guide as verified.

## Installed dependency inventory

The 2026-09-13 scan reports **70 advisory records across 11 packages**, but repeated records inflate that total: there are **37 distinct package/advisory-ID pairs**. The earlier 2026-09-08 scan reported 41 raw records with the same 37 distinct pairs. This is not evidence of 70 independently exploitable Ficary vulnerabilities or 29 newly introduced vulnerabilities.

| Installed package | Version | Distinct advisory IDs |
| --- | --- | --- |
| aiohttp | 3.13.5 | 14 |
| click | 8.3.2 | 1 |
| idna | 3.11 | 1 |
| keras | 3.14.0 | 7 |
| msgpack | 1.1.2 | 1 |
| pip | 25.1.1 | 6 |
| setuptools | 81.0.0 | 1 |
| soupsieve | 2.8.3 | 2 |
| torch | 2.11.0 | 1 |
| transformers | 5.5.4 | 1 |
| urllib3 | 2.6.3 | 2 |

Source: `evidence/pip-audit-2026-09-13.json` and `.log`; original September 8 result retained. No dependency upgrades were applied.

Reachability assessment:

- `aiohttp` is used by installed `edge-tts` for its client traffic. At least one advisory concerns a client response parser and therefore merits prioritization; the maintainer identifies 3.14.3 as patched for that specific issue. This audit did not inject malicious upstream responses or demonstrate an exploit in Ficary. [aiohttp client parser advisory](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273).
- Many other aiohttp advisories concern server request parsing, custom DigestAuthMiddleware, persisted CookieJar input or unusual per-request options. Ficary is a desktop/CLI client; their presence in a package report is insufficient to establish reachability.
- SoupSieve is a normal Beautiful Soup dependency, but its reported selector-parser issues require untrusted **selector strings**. Reviewed application selectors are code-defined constants or helpers receiving constants. Untrusted downloaded HTML alone does not establish that attack path. Upgrade hygiene still applies. [SoupSieve selector advisory](https://github.com/facelessuser/soupsieve/security/advisories/GHSA-836r-79rf-4m37).
- PyTorch, Transformers, Keras, msgpack and Click require separate optional-model/dependency analysis. The development environment contains more than the core runtime; these versions cannot be assumed to equal packaged releases. Audit each release's resolved contents and each optional backend's installed environment.
- pip and setuptools findings affect the installation/build toolchain; do not present them as direct story-downloading exploits. urllib3/idna likewise need call-path-specific assessment; most Ficary HTTP code uses curl_cffi.

`pip check` currently exits 1: `nvidia-cusparselt-cu13 0.8.0 is not supported on this platform`. This is a verified local ARM64 environment inconsistency, not proof that the normal app or a released x86_64 package fails. The full application baseline still passes. Preserve/recreate tested environment manifests and keep large optional neural stacks isolated from the ordinary runtime.

## Testing findings

Baseline: **2,190 passed, 13 warnings, 88.81 seconds**, under native wx/GTK with Xvfb and temporary preference/data paths. Statement coverage is **62.34% (17,821 / 28,589 statements)**. Coverage is a diagnostic, not a release-quality score.

| Under-exercised surface | Baseline statement coverage | Why it matters |
| --- | --- | --- |
| `reader/gui.py` | 0% | Native reader state, selection, Stop and voice-provider integration are outside existing tests. |
| `soundscape/editor.py` | 0% | Editable-but-inert reverb and import/rename behavior lack coverage. |
| `gui_dialogs.py` | 15% | Native layout and asynchronous dialog cancellation paths are largely untested. |
| `neural_env.py` | 23% | Real installer/runtime mismatch and interruption paths are weakly covered. |
| `cli.py` | 26% | Handlers can reference missing imports or corrupt an update despite parser/unit tests passing. |

`ruff check ficary --select F,E9` reports 13 diagnostics; one is the reproduced undefined `generate_audiobook` call. The other F821 is a quoted `LibraryIndex` annotation, not an observed runtime failure. Do not equate all lint messages with product defects. The 13 pytest warnings are unclosed SQLite connections; investigate ownership/cleanup where appropriate, without treating the warnings as proven production data loss.

Prioritize cross-boundary regression cases from this audit: exact update destinations, output collisions, cache freshness and source identity, settings reopen, closed-window worker completion, watch pending side effects, reader resume/Stop, incomplete TTS and cancellation, backup retention boundaries. Keep fixture tests; add assertions that conserve requested prose and preserve existing user files. Do not substitute a larger count of mocked tests for these integration checks.

## Boundaries

No installed release archive inspection, Windows/macOS runtime execution, native screen-reader speech session, live authenticated site check, real notification, paid LLM/TTS request, or large-library performance/load experiment was performed. Licensing and a full transitive software supply-chain assessment were not completed; repository/build inventory alone does not establish legal compliance. These are explicit follow-up validation tracks, not concealed passes or automatically counted defects.
