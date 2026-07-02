"""Upload a finished M4B to an Audiobookshelf server.

Parallel to :mod:`ficary.mailer`'s send-to-Kindle: after an audiobook
render, optionally push the file straight into the user's
Audiobookshelf library. Config comes from prefs first, env second, so
CLI users can export the four vars once and the GUI can override.

Audiobookshelf API (server >= 2.x):
* ``GET /api/libraries`` — enumerate libraries and their folders.
* ``POST /api/upload`` (multipart) — fields ``title``, ``author``,
  ``library`` (id), ``folder`` (id), plus the file part(s).
Both authenticate with ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# M4Bs are hundreds of MB and the server transcodes/scans on receipt —
# a short timeout would abort a legitimate upload mid-stream.
UPLOAD_TIMEOUT_S = 900
LIST_TIMEOUT_S = 30


class ABSConfigError(RuntimeError):
    """Raised when required Audiobookshelf settings aren't available."""


def _config(prefs=None) -> dict:
    """Read ABS config. Prefs override env; env is the fallback.

    Requires server URL + API token; library id is required for an
    upload but optional for :func:`list_libraries` (which is how the
    user discovers ids). Raises :class:`ABSConfigError` listing the
    missing keys."""
    def _read(pref_key, env_key):
        if prefs is not None:
            value = prefs.get(pref_key)
            if value:
                return value
        return os.environ.get(env_key, "").strip()

    cfg = {
        "url": _read("abs_url", "ABS_URL").rstrip("/"),
        "token": _read("abs_token", "ABS_TOKEN"),
        "library_id": _read("abs_library_id", "ABS_LIBRARY_ID"),
        "folder_id": _read("abs_folder_id", "ABS_FOLDER_ID"),
    }
    missing = [k for k in ("url", "token") if not cfg[k]]
    if missing:
        raise ABSConfigError(
            "Missing Audiobookshelf settings: " + ", ".join(missing) + ". "
            "Set ABS_URL / ABS_TOKEN (and optionally ABS_LIBRARY_ID / "
            "ABS_FOLDER_ID) in your environment, or configure them in "
            "the GUI preferences. The token is a plain API key from the "
            "Audiobookshelf user page — stored as-is, same as the other "
            "credentials."
        )
    return cfg


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}"}


def list_libraries(prefs=None, *, transport=None) -> list[dict]:
    """Return ``[{id, name, mediaType, folders: [{id, fullPath}]}]`` for
    every library on the server. ``transport`` is an injection seam for
    tests; production uses curl_cffi."""
    cfg = _config(prefs)
    get = transport or _default_get
    data = get(f"{cfg['url']}/api/libraries", _headers(cfg))
    libraries = data.get("libraries", data) if isinstance(data, dict) else data
    out = []
    for lib in libraries or []:
        out.append({
            "id": lib.get("id", ""),
            "name": lib.get("name", ""),
            "mediaType": lib.get("mediaType", ""),
            "folders": [
                {"id": f.get("id", ""), "fullPath": f.get("fullPath", "")}
                for f in (lib.get("folders") or [])
            ],
        })
    return out


def upload_file(path, *, title, author, prefs=None,
                library_id=None, folder_id=None, transport=None) -> None:
    """Upload ``path`` (an M4B) into the configured library. Raises
    :class:`ABSConfigError` when no library id is resolvable, or
    ``RuntimeError`` on a transport/HTTP failure. ``transport`` is an
    injection seam for tests."""
    cfg = _config(prefs)
    lib_id = library_id or cfg["library_id"]
    if not lib_id:
        raise ABSConfigError(
            "No Audiobookshelf library id. Set ABS_LIBRARY_ID (or the "
            "GUI library field), or pass --abs-library ID. Run "
            "--abs-list-libraries to see the ids."
        )
    fold_id = folder_id or cfg["folder_id"]
    fields = {"title": title, "author": author, "library": lib_id}
    if fold_id:
        fields["folder"] = fold_id
    post = transport or _default_post
    post(f"{cfg['url']}/api/upload", _headers(cfg), fields, Path(path))


def _default_get(url: str, headers: dict) -> dict:
    from curl_cffi import requests as curl_requests
    resp = curl_requests.get(
        url, headers=headers, impersonate="chrome", timeout=LIST_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Audiobookshelf {url} -> HTTP {resp.status_code}")
    return resp.json()


def _default_post(url: str, headers: dict, fields: dict, path: Path) -> None:
    from curl_cffi import requests as curl_requests
    with open(path, "rb") as handle:
        files = {"file": (path.name, handle, "audio/mp4")}
        resp = curl_requests.post(
            url, headers=headers, data=fields, files=files,
            impersonate="chrome", timeout=UPLOAD_TIMEOUT_S,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Audiobookshelf upload -> HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )
