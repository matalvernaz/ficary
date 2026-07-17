"""Standalone Cloudflare interactive-challenge browser worker.

Run as a SEPARATE process — never imported into the frozen GUI app:

    <python> _cf_worker.py --url <URL> --out <FILE> [--profile DIR]
                           [--timeout SECONDS] [--headless]

It opens a real (headed) Chromium via Playwright, lets the user clear
Cloudflare's interactive "verify you are human" challenge in that window,
then — in the SAME browser context that earned the clearance — reads the
page HTML and writes it to ``--out`` (UTF-8). Progress and the final
outcome are emitted as one-line JSON objects on stdout.

Why a separate process: Playwright's sync API spawns a Node driver via
``asyncio.create_subprocess_exec``. Inside the frozen PyInstaller GUI app
that fails with ``[WinError 6] The handle is invalid`` — the windowed
build's std handles are invalid and the download runs on a worker thread
with no Windows ProactorEventLoop. A plain console Python child (the
neural_env embeddable on frozen Windows, else the source interpreter),
spawned with valid pipe/DEVNULL std handles on its own main thread,
launches the driver cleanly.

Why fetch here instead of handing a cookie back: Cloudflare binds
``cf_clearance`` to the exact browser (TLS/JA3, HTTP/2 settings, header
order, device, session), so replaying the cookie from a different HTTP
stack is re-challenged. The browser that passed the challenge fetching
the bytes itself is the only robust design.

Only stdlib + playwright. No ficary imports — this runs under a different
interpreter that cannot see the frozen bundle.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

# Substrings that mean the visible document is still a Cloudflare
# challenge, not the real page. Matched case-insensitively against the
# page title.
_CHALLENGE_TITLE_MARKERS = (
    "shields are up",
    "just a moment",
    "verify you are human",
    "attention required",
)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _looks_cleared(page) -> bool:
    """True once the challenge is passed: a ``cf_clearance`` cookie exists
    AND the visible document is no longer a challenge page.

    Both halves matter — the cookie can be set a beat before the redirect
    to the real page finishes, and a stale cookie can pre-exist before the
    user has actually cleared this session's challenge.
    """
    try:
        names = {c.get("name") for c in page.context.cookies()}
    except Exception:
        return False
    if "cf_clearance" not in names:
        return False
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return not any(m in title for m in _CHALLENGE_TITLE_MARKERS)


def run(url: str, out_path: str, profile_dir, timeout_s: float,
        headless: bool) -> int:
    from playwright.sync_api import sync_playwright

    launch_kwargs = dict(
        headless=headless,
        # Strip the automation tells Cloudflare's bot probe looks for.
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    with sync_playwright() as pw:
        if profile_dir:
            # Persistent profile: a still-valid clearance from a recent
            # solve is reused, so the user isn't re-challenged every run.
            context = pw.chromium.launch_persistent_context(
                profile_dir, **launch_kwargs
            )
            page = context.pages[0] if context.pages else context.new_page()
            closer = context
        else:
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context()
            page = context.new_page()
            closer = browser
        try:
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined})"
            )
            _emit({"status": "opening", "url": url})
            page.goto(url, timeout=60_000, wait_until="domcontentloaded")

            if _looks_cleared(page):
                # Profile already carries a live clearance — no interaction.
                pass
            else:
                _emit({
                    "status": "await_human",
                    "message": (
                        "A browser window opened. Complete the 'verify you "
                        "are human' step; the download continues on its own."
                    ),
                })
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    if _looks_cleared(page):
                        break
                    page.wait_for_timeout(1000)
                else:
                    _emit({"status": "timeout"})
                    return 3

            # Challenge passed. Let the redirect to the real document
            # settle, then capture what the browser now sees.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            html = page.content()
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            _emit({"status": "ok", "bytes": len(html)})
            return 0
        finally:
            try:
                closer.close()
            except Exception:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ficary Cloudflare browser worker")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)
    try:
        return run(
            args.url, args.out, args.profile or None,
            args.timeout, args.headless,
        )
    except Exception as exc:
        # Missing playwright/browser, launch failure, or the user closing
        # the window all land here; the parent reads this and falls back.
        _emit({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
