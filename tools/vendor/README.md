# Vendored binaries

## ZipExtractor.exe

The self-update helper bundled next to `ficary.exe` in the Windows
portable zip (see `ficary/self_update.py`). Waits on the running app's
PID, extracts the update zip over the install dir with Restart
Manager-based locked-file diagnosis, and relaunches the app.

- Source: https://github.com/ravibpatel/AutoUpdater.NET (MIT)
- Commit: `d687b8fb3f604b65238cfd608311638b9603b05c` (dev branch,
  2026-07-07) — includes the de-elevation fix for upstream issue #754
  (an elevated ZipExtractor no longer relaunches the app with the
  admin token) and the high-DPI window fix. CLI arguments
  (`--input/--output/--current-exe/--updated-exe`) are unchanged from
  the v1.9.2 this replaced.
- Built by: `.github/workflows/build-zipextractor.yml`
  (run 29969345562), MSBuild Release, `SignAssembly=false` (upstream
  ships only an encrypted `.snk`).
- SHA-256: `98bcc7082fd7552e2bc7532d77b09d990bcfb5441a747beed10ffbc0f758fb28`
  (105,472 bytes)

`build-windows.yml` verifies this hash before bundling, so the binary
here can't be swapped without also changing the pin in the workflow.
To move to a newer upstream commit: dispatch `build-zipextractor.yml`
with the new ref, replace this file's Commit/SHA-256 entries and the
binary, and update the pinned hash in `build-windows.yml` — all in one
commit.
