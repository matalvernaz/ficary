"""GitHub-release self-update for the portable Windows build.

Uses the ZipExtractor.exe pattern popularised by Libation /
ravibpatel's AutoUpdater.NET: we ship a tiny signed helper .exe
next to ficary.exe, copy it to ``%TEMP%`` so it isn't locked in the
install dir, spawn it via ``ShellExecuteW`` (with UAC elevation only
when the install dir isn't user-writable), and exit. The helper
waits for our process handle via ``Process.WaitForExit``, extracts
the update zip over the install with Windows Restart Manager-based
locked-file diagnosis, writes a ``ZipExtractor.log`` to its own
directory, and relaunches ficary.exe.

The old approach (batch script + ``tasklist`` polling + ``robocopy``)
was fragile for several reasons — silent failures, no logging,
Defender heuristics on batch-in-%TEMP%-touching-files-elsewhere, no
UAC path. All of those go away here.

Helper source: ravibpatel/AutoUpdater.NET v1.9.2 (MIT). Built from
source in CI; see ``.github/workflows/build-windows.yml``.
"""

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from curl_cffi import requests as curl_requests

from . import __version__
from .atomic import atomic_write_text

logger = logging.getLogger(__name__)

REPO = "matalvernaz/ficary"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"

# Bundled alongside ficary.exe in the portable zip; vendored at
# tools/vendor/ (see its README for provenance). If it's ever missing
# we refuse to self-replace and direct the user to the release page
# instead.
ZIP_EXTRACTOR_EXE = "ZipExtractor.exe"

# Journal written into the install dir just before ZipExtractor is
# spawned, holding the target tag and a SHA-256 manifest of every file
# in the flat update zip. The next launch verifies the install against
# it: all-match → update confirmed, journal deleted; anything else →
# the app offers a roll-forward repair from the retained zip (or a
# fresh download). Modelled on Libation's InstallUpgradeManager, but
# repairing forward to the already-verified new version instead of
# restoring a backup — a backup of the ~300 MB install per update
# isn't worth the disk, and the new zip is the trusted artifact anyway.
UPDATE_JOURNAL_NAME = "update-journal.json"

# A journal younger than this with a version mismatch is treated as
# "extractor still working" rather than a failed update — the user may
# have relaunched ficary manually while ZipExtractor was waiting on
# the old process or mid-extraction.
_JOURNAL_GRACE_S = 180


def _parse_version(tag: str):
    """Parse 'v1.2.3' → (1, 2, 3). Returns None for unrecognised formats.

    Anchored so prerelease tags like ``v1.2.3-beta`` / ``v1.2.3rc1`` don't
    parse as stable ``(1, 2, 3)`` — GitHub's ``releases/latest`` skips
    prereleases by default, but if one slips through unmarked we'd
    silently treat it as a stable update.
    """
    if not tag:
        return None
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def check_for_update(allow_equal: bool = False):
    """Fetch the GitHub latest-release JSON.

    Returns a dict {tag, download_url, size, digest} when a newer
    version exists than the currently running one, else None. Network
    errors raise; callers should catch broadly and skip silently so a
    transient failure doesn't bother the user.

    ``allow_equal`` also accepts the currently-running version — used
    by the torn-update repair path, which needs to re-download the
    release the install already claims to be.
    """
    resp = curl_requests.get(LATEST_URL, impersonate="chrome", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    latest = _parse_version(data.get("tag_name", ""))
    current = _parse_version(__version__)
    if not latest or not current:
        return None
    if latest < current or (latest == current and not allow_equal):
        return None

    # Platform-appropriate asset. macOS: the ditto'd .app zip. Windows
    # (and everything else, so pre-existing Linux behavior — a
    # release-page prompt — is unchanged): prefer the portable zip,
    # falling back to a single-file .exe only if one is still attached
    # to an old release so 1.9.x clients keep working.
    zip_asset = None
    exe_asset = None
    if sys.platform == "darwin":
        for asset in data.get("assets") or []:
            name = asset.get("name", "").lower()
            if name.endswith(".zip") and "macos" in name:
                zip_asset = asset
                break
    else:
        for asset in data.get("assets") or []:
            name = asset.get("name", "").lower()
            if name.endswith(".zip") and "portable" in name:
                zip_asset = asset
                break
            if name.endswith(".exe") and exe_asset is None:
                exe_asset = asset
    chosen = zip_asset or exe_asset
    if not chosen:
        return None

    return {
        "tag": data["tag_name"],
        "download_url": chosen["browser_download_url"],
        "size": chosen.get("size", 0),
        "digest": chosen.get("digest"),  # "sha256:<hex>" when present
        "release_url": data.get("html_url"),
        "is_zip": zip_asset is not None,
    }


def _default_releases_get():
    resp = curl_requests.get(
        RELEASES_URL, params={"per_page": 100},
        impersonate="chrome", timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_changelog_since(current_version=None, *, max_versions=25, transport=None):
    """Aggregate release notes for every published release newer than the
    running version, newest first, as one human-readable string.

    The ``releases/latest`` endpoint only carries the newest release's
    body, so a user several versions behind would see notes for just the
    top release and miss everything they skipped. This walks the full
    release list and concatenates each intervening release's notes so the
    update prompt can show "everything since your version".

    Best-effort — returns ``""`` on any network/parse failure or when
    nothing is newer, so a changelog hiccup never blocks the update
    prompt. Only meant to be called once an update is actually being
    offered (after :func:`check_for_update` returns a newer version), so
    the extra API call happens once per real update, not once per check.

    ``current_version`` defaults to the running build; ``transport`` is an
    injection seam for tests.
    """
    current = _parse_version(current_version or __version__)
    if current is None:
        return ""
    try:
        releases = (transport or _default_releases_get)()
    except Exception:
        logger.debug("Couldn't fetch release list for changelog", exc_info=True)
        return ""

    entries = []
    for rel in releases or []:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        ver = _parse_version(rel.get("tag_name", ""))
        if ver is None or ver <= current:
            continue
        entries.append(
            (ver, rel.get("tag_name", "") or "", (rel.get("body") or "").strip())
        )

    if not entries:
        return ""
    # Newest first. Sort on the parsed version tuple, not the tag string,
    # so 2.10.0 orders above 2.9.0 (lexical tag sort would invert them).
    entries.sort(key=lambda e: e[0], reverse=True)

    truncated = 0
    if len(entries) > max_versions:
        truncated = len(entries) - max_versions
        entries = entries[:max_versions]

    blocks = [f"{tag}\n{body}" if body else tag for _ver, tag, body in entries]
    if truncated:
        blocks.append(
            f"(and {truncated} older release{'' if truncated == 1 else 's'} — "
            "see the release page for the full history.)"
        )
    return "\n\n".join(blocks)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# Gatekeeper runs a quarantined app from a randomized read-only mount
# containing this path segment. The bundle we'd be replacing isn't the
# one the user installed, so self-update must refuse.
_APP_TRANSLOCATION_MARKER = "/AppTranslocation/"


def _macos_bundle_path():
    """Return the .app bundle the frozen macOS build runs from, or None.

    ``sys.executable`` is ``<bundle>.app/Contents/MacOS/<exe>``; anything
    that doesn't match that shape (stray onedir run outside a bundle)
    disqualifies self-update rather than guessing.
    """
    if not (is_frozen() and sys.platform == "darwin"):
        return None
    exe = Path(sys.executable).resolve()
    if len(exe.parents) < 3:
        return None
    bundle = exe.parents[2]
    if bundle.suffix != ".app" or not (bundle / "Contents" / "Info.plist").is_file():
        return None
    return bundle


def _pid_suffix_alive(name: str) -> bool:
    """True when the trailing ``-<pid>`` of ``name`` is a live process.

    An unparseable suffix reports dead so malformed debris still gets
    swept. PID reuse can at worst defer one sweep to the next launch.
    """
    tail = name.rsplit("-", 1)[-1]
    if not tail.isdigit():
        return False
    try:
        os.kill(int(tail), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        # EPERM etc. — something owns that PID; err on the safe side.
        return True


def _macos_can_self_replace() -> bool:
    bundle = _macos_bundle_path()
    if bundle is None:
        return False
    if _APP_TRANSLOCATION_MARKER in str(bundle):
        return False
    return _is_writable(bundle.parent)


def _windows_can_self_replace() -> bool:
    """``.is_file()`` rather than ``.exists()`` so a directory accidentally
    named ``ZipExtractor.exe`` doesn't fool the GUI into offering an
    in-place update that would fail at the ``shutil.copy2`` step.
    """
    if not (is_frozen() and sys.platform.startswith("win")):
        return False
    return (Path(sys.executable).parent / ZIP_EXTRACTOR_EXE).is_file()


def can_self_replace() -> bool:
    """True when this build can swap itself in place.

    Windows: frozen portable build with ZipExtractor.exe bundled.
    macOS: frozen .app bundle, not App-Translocated, parent writable —
    the update replaces the whole bundle atomically, no helper needed.
    Linux: not supported; the GUI offers the release page instead.
    """
    if sys.platform == "darwin":
        return _macos_can_self_replace()
    return _windows_can_self_replace()


def _sha256_file(path: Path) -> str:
    """SHA-256 hex digest of ``path``. Streams in 1 MiB chunks so a
    large portable build doesn't load the whole file into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_digest(path: Path, digest: str) -> None:
    # No digest on the release asset → log loudly so the absence is
    # auditable, but don't block: GitHub doesn't always populate the
    # ``digest`` field, and the download itself happened over HTTPS
    # against api.github.com so the URL→bytes path is already
    # authenticated.
    if not digest or ":" not in digest:
        logger.warning(
            "Update asset has no SHA-256 digest; skipping content "
            "verification (URL was HTTPS so the channel is still "
            "authenticated)."
        )
        return
    algo, expected = digest.split(":", 1)
    if algo.lower() != "sha256":
        logger.warning(
            "Update asset advertises unsupported digest algorithm %r; "
            "skipping content verification.",
            algo,
        )
        return
    if _sha256_file(path).lower() != expected.lower():
        raise RuntimeError(
            "Downloaded update failed SHA-256 verification. The file was "
            "not installed; the running version is unchanged."
        )


def _journal_path() -> Path:
    return Path(sys.executable).resolve().parent / UPDATE_JOURNAL_NAME


def _read_update_journal():
    """Return the parsed journal dict, or None when absent/corrupt.

    A corrupt journal is deleted on sight: it can't drive a repair, and
    leaving it would re-log the same parse failure every launch.
    """
    path = _journal_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Could not read update journal: %s", exc)
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("target_tag"):
            raise ValueError("journal missing target_tag")
        return data
    except (ValueError, TypeError) as exc:
        logger.warning("Discarding corrupt update journal: %s", exc)
        path.unlink(missing_ok=True)
        return None


def _delete_update_journal() -> None:
    try:
        _journal_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not delete update journal: %s", exc)


def _flat_zip_manifest(flat_zip: Path) -> dict:
    """Map each file member of ``flat_zip`` to its size and SHA-256.

    Keys are the archive paths (forward slashes), exactly what lands
    relative to the install dir. Streamed member-by-member so the
    decompressed payload never sits in memory at once.
    """
    manifest = {}
    with zipfile.ZipFile(flat_zip) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            h = hashlib.sha256()
            with zf.open(info) as member:
                for chunk in iter(lambda: member.read(1 << 20), b""):
                    h.update(chunk)
            manifest[info.filename] = {
                "size": info.file_size,
                "sha256": h.hexdigest(),
            }
    return manifest


def _write_update_journal(tag: str, flat_zip: Path, manifest: dict) -> None:
    payload = json.dumps(
        {
            "target_tag": tag,
            "started": time.time(),
            "flat_zip": str(flat_zip),
            "manifest": manifest,
        },
        indent=1,
    )
    atomic_write_text(_journal_path(), payload)


def _verify_manifest(install_dir: Path, manifest: dict) -> list:
    """Return the archive paths whose installed copies don't match.

    Size is checked first so the common all-good case only pays the
    full hash pass, and a torn file usually fails without hashing.
    Only files named in the manifest are touched — user data next to
    the exe (settings.ini, cache/, neural/) is never read.
    """
    bad = []
    for relpath, expected in manifest.items():
        target = install_dir / Path(*relpath.split("/"))
        try:
            if target.stat().st_size != expected["size"]:
                bad.append(relpath)
                continue
            if _sha256_file(target) != expected["sha256"]:
                bad.append(relpath)
        except OSError:
            bad.append(relpath)
    return bad


def pending_update_status():
    """Check the update journal against the running install.

    Returns None when there's nothing to do (no journal, verified OK,
    or an update is plausibly still being applied). Otherwise returns
    ``{"state": "torn"|"stale", "target_tag": str, "flat_zip": Path|None}``
    for the GUI to offer a repair:

    - ``stale`` — the running version predates the journal's target:
      ZipExtractor never swapped the install (crashed, was cancelled,
      or the machine lost power before extraction).
    - ``torn`` — versions match but some installed files don't match
      the manifest: extraction was interrupted partway.
    """
    journal = _read_update_journal()
    if journal is None:
        return None

    target = _parse_version(journal.get("target_tag", ""))
    current = _parse_version(__version__)
    if target is None or current is None:
        logger.warning("Update journal has unparseable versions; discarding.")
        _delete_update_journal()
        return None

    flat_zip = Path(journal.get("flat_zip") or "")
    flat_zip = flat_zip if flat_zip.is_file() else None
    install_dir = Path(sys.executable).resolve().parent

    if current < target:
        started = float(journal.get("started") or 0)
        if started and time.time() - started < _JOURNAL_GRACE_S:
            # Probably still mid-update: the user relaunched while
            # ZipExtractor waits on the old process or is extracting.
            return None
        return {
            "state": "stale",
            "target_tag": journal["target_tag"],
            "flat_zip": flat_zip,
        }

    if current > target:
        # Running something newer than the journal's target — a manual
        # reinstall or downgrade happened around a failed update. The
        # journal no longer describes this install; drop it.
        _delete_update_journal()
        return None

    bad = _verify_manifest(install_dir, journal.get("manifest") or {})
    if not bad:
        logger.info(
            "Update to %s verified: all %d files match the manifest.",
            journal["target_tag"], len(journal.get("manifest") or {}),
        )
        _delete_update_journal()
        if flat_zip is not None:
            workdir = flat_zip.parent
            if workdir.name.startswith("ficary-update-"):
                shutil.rmtree(workdir, ignore_errors=True)
        return None

    logger.warning(
        "Update to %s is torn: %d of %d files fail verification (first: %s).",
        journal["target_tag"], len(bad),
        len(journal.get("manifest") or {}), bad[0],
    )
    return {
        "state": "torn",
        "target_tag": journal["target_tag"],
        "flat_zip": flat_zip,
    }


def retry_pending_update(status) -> None:
    """Re-apply a torn/stale update from the retained flat zip.

    Re-spawns ZipExtractor with the zip the journal points at; the
    caller must exit shortly after, exactly as with
    :func:`download_and_replace`. Raises when the zip is missing or
    unreadable (or the helper can't be staged) — callers fall back to
    a fresh download via :func:`check_for_update` with
    ``allow_equal=True``.
    """
    flat_zip = status.get("flat_zip")
    if not flat_zip or not Path(flat_zip).is_file():
        raise RuntimeError("Retained update zip is gone; a fresh download is needed.")
    flat_zip = Path(flat_zip)
    try:
        with zipfile.ZipFile(flat_zip) as zf:
            corrupt = zf.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"Retained update zip is unreadable: {exc}") from exc
    if corrupt is not None:
        raise RuntimeError(f"Retained update zip is corrupt at {corrupt!r}.")

    if not can_self_replace():
        raise RuntimeError("ZipExtractor.exe is missing; cannot re-apply.")

    current_exe = Path(sys.executable).resolve()
    journal = _read_update_journal() or {}
    manifest = journal.get("manifest") or {}
    # Same crossed-install migration decision download_and_replace
    # makes: a still-ffn-dl.exe install relaunches the ficary.exe the
    # zip carries.
    updated_exe = None
    if current_exe.name.lower() == "ffn-dl.exe" and "ficary.exe" in manifest:
        updated_exe = "ficary.exe"
    # Refresh the journal timestamp so the relaunch lands inside the
    # grace window instead of immediately re-prompting for repair.
    _write_update_journal(journal.get("target_tag", ""), flat_zip, manifest)
    _stage_and_spawn_extractor(
        flat_zip, current_exe.parent, current_exe, updated_exe,
    )


def cleanup_old_exe() -> None:
    """Remove debris from earlier update flows.

    - ``<name>.exe.old`` left behind by the pre-1.10 rename-in-place path.
    - ``%TEMP%/ficary-update-*`` workdirs older than 24 hours that the
      batch-script updater used to leave on disk.
    """
    if not is_frozen():
        return

    # macOS: reap the renamed-aside bundle(s) a previous swap left
    # behind. The ``-<pid>`` suffix is the PID of the instance that
    # performed the swap; if that process is still alive it is still
    # executing (and lazy-loading _internal files) from the .old
    # bundle — a user who declined the post-update restart to let a
    # download finish. Deleting it underneath them breaks later
    # imports, so skip live ones; the next launch retries.
    if sys.platform == "darwin":
        bundle = _macos_bundle_path()
        if bundle is not None:
            for stale in bundle.parent.glob(f"{bundle.stem}.app.old-*"):
                if _pid_suffix_alive(stale.name):
                    continue
                shutil.rmtree(stale, ignore_errors=True)
            for stale in bundle.parent.glob(".ficary-update-staged-*.app"):
                shutil.rmtree(stale, ignore_errors=True)

    try:
        current = Path(sys.executable)
        old = current.with_name(current.stem + ".exe.old")
        if old.exists():
            old.unlink()
    except OSError as exc:
        logger.debug("Could not remove stale old exe: %s", exc)

    # Remove the ffn-dl.exe rename shim once we're running as ficary.exe.
    # Release zips shipped it through the ffn-dl -> ficary rename so
    # pre-rename auto-updaters could cross over, which meant it reappeared
    # on every update. Only delete it when we are ficary.exe; an install
    # still running as ffn-dl.exe must not unlink its own binary.
    try:
        current = Path(sys.executable)
        if current.name.lower() == "ficary.exe":
            shim = current.with_name("ffn-dl.exe")
            if shim.exists():
                shim.unlink()
    except OSError as exc:
        logger.debug("Could not remove ffn-dl.exe rename shim: %s", exc)

    try:
        temp = Path(tempfile.gettempdir())
        cutoff = time.time() - 24 * 3600
        # A live journal's workdir holds the zip a repair would reuse —
        # never sweep it, even past the cutoff (the user may simply not
        # have relaunched for a day after a torn update).
        journal = _read_update_journal()
        keep = None
        if journal and journal.get("flat_zip"):
            keep = Path(journal["flat_zip"]).parent
        # Both prefixes: pre-rename clients left ffn-dl-update-* workdirs
        # (including the ~60 MB one from their failed cross-rename
        # attempt) that the new glob alone would never reclaim.
        for pattern in ("ficary-update-*", "ffn-dl-update-*"):
            for d in temp.glob(pattern):
                try:
                    if keep is not None and d.resolve() == keep.resolve():
                        continue
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    continue
    except OSError as exc:
        logger.debug("Could not sweep stale update workdirs: %s", exc)


def _download(url: str, dest: Path, progress_cb=None, expected_size: int = 0) -> None:
    """Stream ``url`` to ``dest``; raises on HTTP errors, truncation, or overshoot.

    A short per-read timeout (12 s) is preferred over the previous 60 s
    so a user clicking Abort during a stalled HTTPS read sees the
    cancellation observed within ~12 s instead of up to a minute. The
    download itself routinely takes longer than that — the timeout is
    *per recv*, not per request — so a healthy slow connection still
    completes.

    Both the ``Content-Length`` header AND the API-declared
    ``expected_size`` are checked independently — a malicious or buggy
    server can otherwise serve a short body that matches its own header
    but disagrees with the release-asset size. Without this fallback
    layer, the only thing standing between a short-zip update and the
    install dir is the SHA-256 digest, which GitHub doesn't always
    populate.
    """
    resp = curl_requests.get(url, impersonate="chrome", timeout=12, stream=True)
    resp.raise_for_status()
    header_size = int(resp.headers.get("content-length") or 0)
    api_size = int(expected_size or 0)
    if header_size > 0 and api_size > 0 and header_size != api_size:
        raise RuntimeError(
            f"Update size mismatch: server reports {header_size} bytes, "
            f"release API reports {api_size}. Refusing to install."
        )
    max_expected = max(s for s in (header_size, api_size) if s > 0) if (header_size or api_size) else 0
    done = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if max_expected and done > max_expected:
                raise RuntimeError(
                    f"Update download exceeded expected size: got at least "
                    f"{done} bytes, expected {max_expected}."
                )
            if progress_cb:
                progress_cb(done, max_expected)
    # Catch truncation on connection drops where the underlying stream
    # ended cleanly (no exception) but the declared sizes weren't
    # satisfied. Without this guard a partial zip lands on disk and
    # either fails extraction or — worse — extracts a half-installed
    # app over the user's existing install.
    if header_size > 0 and done != header_size:
        raise RuntimeError(
            f"Update download truncated: got {done} bytes, expected {header_size}. "
            "The current version is unchanged; please retry."
        )
    if api_size > 0 and done != api_size:
        raise RuntimeError(
            f"Update download size mismatch: got {done} bytes, release API "
            f"declared {api_size}. The current version is unchanged."
        )


def _is_writable(path: Path) -> bool:
    """Probe whether the current process can create/remove a file in ``path``.

    If the user unzipped into ``C:\\Program Files\\`` we need to elevate
    via UAC; in the common case (Downloads, Desktop, their home) we
    don't, and skipping the prompt makes the update one-click.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".ficary-update-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _repack_flat(src_dir: Path, dest_zip: Path) -> None:
    """Re-zip ``src_dir``'s *contents* at the archive root.

    The release zip has ``ficary/`` as the top-level entry so humans
    who double-click to extract get a tidy folder. ZipExtractor
    unpacks the archive as-is into the install dir, though, so if
    we handed it the wrapped zip we'd end up with
    ``install/ficary/ficary.exe``. Re-packing flat is a few seconds
    on a 30 MB archive and keeps both paths working from one release.
    """
    with zipfile.ZipFile(
        dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3,
    ) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))


def _shell_execute(verb: str, file: Path, params: str, cwd: Path) -> None:
    """Thin wrapper around ``ShellExecuteW`` that raises on failure.

    ``subprocess.Popen`` can't request the ``runas`` verb; Win32
    ``ShellExecuteW`` is the only stdlib-accessible way to trigger a
    UAC elevation prompt from Python.

    Argtypes/restype are declared so the 64-bit ``HINSTANCE`` return
    value isn't truncated by ctypes' default ``c_int`` — the
    ``rc <= 32`` success-code semantics survive truncation in practice
    but declaring the signature is the correct defensive shape.
    """
    if sys.platform.startswith("win"):
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        if not getattr(shell32.ShellExecuteW, "_ficary_signature_set", False):
            shell32.ShellExecuteW.argtypes = [
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                ctypes.c_int,
            ]
            shell32.ShellExecuteW.restype = wintypes.HINSTANCE
            shell32.ShellExecuteW._ficary_signature_set = True
        SW_SHOWNORMAL = 1
        rc_handle = shell32.ShellExecuteW(
            None, verb, str(file), params, str(cwd), SW_SHOWNORMAL,
        )
        rc = int(ctypes.cast(rc_handle, ctypes.c_void_p).value or 0)
    else:
        raise RuntimeError("ShellExecuteW only available on Windows")
    # ShellExecuteW returns > 32 on success; values <= 32 are Win32
    # error codes (2 = ENOENT, 5 = access denied, 1223 = UAC cancelled
    # — though the UAC-cancel case usually returns SE_ERR_ACCESSDENIED).
    if rc <= 32:
        raise RuntimeError(f"ShellExecuteW failed (code {rc}) launching {file}")


def _spawn_extractor(
    extractor: Path, zip_path: Path, install_dir: Path, exe: Path,
    updated_exe=None,
) -> None:
    """Launch the bundled helper to swap files + relaunch ficary.

    Uses ``runas`` only when the install dir isn't writable — the
    common case (user unzipped to Downloads, Desktop, home) doesn't
    need elevation and skipping the prompt makes the flow one-click.

    ``updated_exe`` (a bare filename) makes ZipExtractor relaunch a
    *different* binary than the one it waited on: it still waits on
    ``--current-exe`` (matched by full path against the live process)
    but relaunches ``--updated-exe`` resolved against ``--output``.
    Used to migrate a crossed-over ``ffn-dl.exe`` install onto
    ``ficary.exe`` — wait on the live ffn-dl.exe, relaunch ficary.exe —
    without racing the file swap.

    Argument quoting goes through ``subprocess.list2cmdline`` because
    hand-rolled ``f'"{path}"'`` breaks at drive roots:
    ``"D:\\"`` parses as an escaped quote under
    ``CommandLineToArgvW``, swallowing the next token. ``list2cmdline``
    doubles trailing backslashes correctly per MS docs.
    """
    argv = [
        "--input", str(zip_path),
        "--output", str(install_dir),
        "--current-exe", str(exe),
    ]
    if updated_exe:
        argv += ["--updated-exe", updated_exe]
    params = subprocess.list2cmdline(argv)
    verb = "open" if _is_writable(install_dir) else "runas"
    # ZipExtractor writes its log (ZipExtractor.log) to AppDomain.
    # CurrentDomain.BaseDirectory — i.e. its own folder. Point it at
    # the per-update workdir so the log lives next to the binary.
    _shell_execute(verb, extractor, params, extractor.parent)


def download_and_replace(update_info, progress_cb=None) -> Path:
    """Download the update and put the swap in motion.

    Windows: spawns the ZipExtractor helper; the caller MUST exit
    shortly after — the helper blocks on our PID before it touches any
    file in the install. macOS: the whole .app bundle is swapped
    in-process before this returns; the caller restarts (execv) rather
    than exits. Returns the install directory (for logging).
    """
    if not can_self_replace():
        raise RuntimeError(
            "In-place update isn't supported for this installation. "
            "Please download the new version manually from the release "
            "page."
        )

    if sys.platform == "darwin":
        return _macos_download_and_swap(update_info, progress_cb)

    if not update_info.get("is_zip"):
        raise RuntimeError(
            "Update asset is not a portable zip. Please download the new "
            "version manually from the release page."
        )

    current_exe = Path(sys.executable).resolve()
    install_dir = current_exe.parent

    workdir = Path(tempfile.mkdtemp(prefix="ficary-update-"))
    zip_path = workdir / "ficary-portable.zip"
    extracted = workdir / "extracted"
    flat_zip = workdir / "ficary-flat.zip"

    try:
        _download(
            update_info["download_url"],
            zip_path,
            progress_cb=progress_cb,
            expected_size=update_info.get("size", 0),
        )
        _verify_digest(zip_path, update_info.get("digest"))

        # Unwrap the top-level folder so ZipExtractor can extract
        # straight into install_dir. Validate every member's resolved
        # path against the extraction root before writing — the stdlib
        # ``zipfile.extractall`` does not block path traversal
        # (``../../etc/passwd``) or absolute Windows paths
        # (``C:\Windows\System32\...``). The SHA-256 digest check on
        # the asset already authenticates the zip's bytes against the
        # release API, but defense-in-depth: if a compromised release
        # or weakened TLS pipeline ever delivered a Zip Slip payload,
        # we'd refuse it instead of writing files outside ``extracted``.
        extracted.mkdir()
        extracted_resolved = extracted.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # Reject absolute paths and ``..`` segments outright;
                # they can't appear in a well-formed release zip.
                name = info.filename
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                    raise RuntimeError(
                        "Refusing to extract update — zip contains a "
                        f"suspicious path: {name!r}"
                    )
                target = (extracted / name).resolve()
                try:
                    target.relative_to(extracted_resolved)
                except ValueError as exc:
                    raise RuntimeError(
                        "Refusing to extract update — zip member would "
                        f"land outside the extract directory: {name!r}"
                    ) from exc
                zf.extract(info, extracted)
        zip_path.unlink(missing_ok=True)

        # Determine the directory whose *contents* should land in the
        # install dir. The release zip wraps everything under
        # ``ficary/``, so the common case is a single top-level dir;
        # but a malformed release (stray README, __MACOSX, etc.) can
        # produce multiple top-level entries. Look for a child dir that
        # actually contains the running exe name — that's the real app
        # root regardless of sibling debris. Fall back to the bare
        # extracted root only when the exe sits there directly. If
        # nothing matches, refuse the update rather than half-install.
        # Accept either exe name across the ffn-dl -> ficary rename: an
        # install still running as ffn-dl.exe (crossed over via the
        # release zip's compat shim) must keep updating from zips whose
        # primary exe is ficary.exe, and vice versa.
        expected_names = [current_exe.name]
        for alias in ("ficary.exe", "ffn-dl.exe"):
            if alias not in expected_names:
                expected_names.append(alias)
        candidate = None
        for expected_exe in expected_names:
            for child in extracted.iterdir():
                if child.is_dir() and (child / expected_exe).is_file():
                    candidate = child
                    break
            if candidate is None and (extracted / expected_exe).is_file():
                candidate = extracted
            if candidate is not None:
                break
        if candidate is None:
            raise RuntimeError(
                f"Downloaded portable zip does not contain {expected_names[0]} "
                "at the expected location. Update aborted; install unchanged."
            )
        src_for_repack = candidate
        # candidate is deleted with ``extracted`` just below, so record
        # now whether the payload carries ficary.exe — this drives the
        # crossed-install migration at relaunch time.
        payload_has_ficary = (candidate / "ficary.exe").is_file()
        _repack_flat(src_for_repack, flat_zip)
        shutil.rmtree(extracted, ignore_errors=True)

        # Migrate a crossed-over install onto ficary.exe. Such an install
        # crossed the ffn-dl -> ficary rename via the old compat shim and
        # is still running as ffn-dl.exe. Now that the zip no longer ships
        # ffn-dl.exe, have the extractor relaunch the ficary.exe it just
        # wrote (it still waits on the live ffn-dl.exe via --current-exe).
        # cleanup_old_exe() drops the leftover ffn-dl.exe on the next launch.
        updated_exe = None
        if current_exe.name.lower() == "ffn-dl.exe" and payload_has_ficary:
            updated_exe = "ficary.exe"

        # Journal before spawn: once ZipExtractor is loose the app is
        # exiting and nothing else can record what "complete" looks
        # like. The next launch verifies the install against this
        # manifest and offers repair from the retained flat zip.
        _write_update_journal(
            update_info["tag"], flat_zip, _flat_zip_manifest(flat_zip),
        )
        try:
            _stage_and_spawn_extractor(
                flat_zip, install_dir, current_exe, updated_exe,
            )
        except Exception:
            _delete_update_journal()
            raise
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return install_dir


def _stage_and_spawn_extractor(
    flat_zip: Path, install_dir: Path, current_exe: Path, updated_exe=None,
) -> None:
    """Copy ZipExtractor out of the install dir and launch it.

    Staged into the flat zip's own workdir so the helper isn't locked
    inside ``install_dir`` when it overwrites its own binary there.

    The staging copy is hash-compared against the source immediately
    before spawn — defence-in-depth against UAC-bypass-via-temp:
    low-priv malware on the same account could otherwise swap the
    copied helper between ``shutil.copy2`` and an elevated
    ``ShellExecuteW("runas", ...)`` and ride the user's "Yes" prompt
    to admin. The TOCTOU window left is near-zero, and tampering
    surfaces as an error instead of silently elevating untrusted code.
    """
    extractor_src = install_dir / ZIP_EXTRACTOR_EXE
    extractor_tmp = flat_zip.parent / ZIP_EXTRACTOR_EXE
    shutil.copy2(extractor_src, extractor_tmp)
    if _sha256_file(extractor_src) != _sha256_file(extractor_tmp):
        raise RuntimeError(
            "Update aborted — ZipExtractor.exe staging copy did not "
            "match the source binary's SHA-256. Refusing to launch "
            "a possibly tampered helper. The install is unchanged."
        )
    _spawn_extractor(
        extractor_tmp, flat_zip, install_dir, current_exe, updated_exe,
    )


def _macos_download_and_swap(update_info, progress_cb=None) -> Path:
    """Download the macOS zip and atomically swap the .app bundle.

    Because ficary downloads the zip itself (not via a browser), the
    extracted app carries no ``com.apple.quarantine`` attribute, so
    the relaunch faces no Gatekeeper prompt even unsigned. Extraction
    goes through ``ditto`` — the same tool CI packed with — because
    Python's ``zipfile`` drops the exec bits and symlinks a .app needs.

    POSIX renames make the swap atomic per step: the new bundle is
    staged next to the install (same volume), the old bundle is
    renamed aside, the staged one renamed into place, and a failure
    between the two renames restores the original. The old bundle is
    left as ``<name>.app.old-<pid>`` for the next launch to sweep —
    the running process still executes from it until the execv.
    """
    bundle = _macos_bundle_path()
    if bundle is None or not _macos_can_self_replace():
        raise RuntimeError(
            "In-place update isn't supported for this installation. "
            "Please download the new version manually from the release "
            "page."
        )

    workdir = Path(tempfile.mkdtemp(prefix="ficary-update-"))
    zip_path = workdir / "ficary-macos.zip"
    extracted = workdir / "extracted"
    try:
        _download(
            update_info["download_url"],
            zip_path,
            progress_cb=progress_cb,
            expected_size=update_info.get("size", 0),
        )
        _verify_digest(zip_path, update_info.get("digest"))

        extracted.mkdir()
        proc = subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(extracted)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Update extraction failed (ditto exited "
                f"{proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        zip_path.unlink(missing_ok=True)

        apps = [p for p in extracted.iterdir() if p.suffix == ".app"]
        if len(apps) != 1:
            raise RuntimeError(
                "Downloaded zip doesn't contain exactly one .app bundle. "
                "Update aborted; install unchanged."
            )
        new_bundle = apps[0]
        new_exe = new_bundle / "Contents" / "MacOS" / Path(sys.executable).name
        if not new_exe.is_file():
            raise RuntimeError(
                f"Downloaded bundle is missing {new_exe.name}. "
                "Update aborted; install unchanged."
            )

        # Stage on the install's volume so both swap steps are single
        # atomic renames (a cross-device rename would raise EXDEV).
        staged = bundle.parent / f".ficary-update-staged-{os.getpid()}.app"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.move(str(new_bundle), str(staged))

        old = bundle.parent / f"{bundle.stem}.app.old-{os.getpid()}"
        os.rename(bundle, old)
        try:
            os.rename(staged, bundle)
        except OSError:
            os.rename(old, bundle)
            raise
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    shutil.rmtree(workdir, ignore_errors=True)
    return bundle


def restart() -> None:
    """Relaunch the current executable with the original args and exit.

    On Windows the child is spawned DETACHED so it doesn't inherit the
    parent's console, handles, or process group. PyInstaller onefile
    builds extract to a random ``_MEI<rand>`` temp dir at startup and
    the bootloader cleans that dir on exit — if the child's extraction
    races with the parent's cleanup (both touching %TEMP% at once),
    DLLs and data files can end up half-written. Detaching the child
    plus letting the parent finish its ``sys.exit`` keeps the two
    processes' teardown / startup from stepping on each other, which
    otherwise shows up as "app restarted but network/search is broken"
    on the first post-update launch.

    On POSIX we use ``os.execv``, which replaces the current process
    image in place — same PID, no race, nothing to detach.
    """
    args = [sys.executable] + sys.argv[1:]

    if sys.platform.startswith("win"):
        # DETACHED_PROCESS (0x8) — no console inheritance.
        # CREATE_NEW_PROCESS_GROUP (0x200) — Ctrl-C in a dying parent
        # console can't propagate to the child.
        # CREATE_BREAKAWAY_FROM_JOB (0x1000000) — if the parent is in a
        # Job object (installer, AV sandbox) the child escapes the
        # lifetime tie that would otherwise kill it with us.
        creationflags = 0x8 | 0x200 | 0x1000000
        try:
            subprocess.Popen(
                args,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError:
            # Job-breakaway isn't always permitted (some installers
            # run inside a Job with JOB_OBJECT_LIMIT_BREAKAWAY_OK
            # disabled). Retry without the breakaway flag — we still
            # get detach + new-group, which is the important part.
            subprocess.Popen(
                args,
                close_fds=True,
                creationflags=0x8 | 0x200,
            )
        sys.exit(0)

    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, args)
