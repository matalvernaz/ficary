"""Checking the library for new chapters when the main window opens.

Part of the library-first main window, chosen by the maintainer: when
Ficary opens it quietly asks each site how many chapters the stories it
has not checked lately now have, and says in one line how many stories
have new chapters waiting. Nothing is downloaded here. Library > Check
for Story Updates (Ctrl+U) does that, and finds what this check found
without asking the sites again: the answers go into the library index
as ``remote_chapter_count``, which an update reads as "owed chapters".

It runs the same probe loop as Ctrl+U (``cli._run_update_queue`` in
dry-run), so the Cloudflare breaker and the per-site pacing apply, and
it holds each site's download slot around every check, so it takes
turns with downloads instead of adding to a site's request rate.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Stories checked within this long are not asked about again at launch.
# A day, where Ctrl+U uses six hours: at six hours most launches would
# re-check nearly the whole library, which for a big FanFiction.net
# library is over an hour of requests every time Ficary opens.
LAUNCH_RECHECK_INTERVAL_S = 24 * 60 * 60

# Answers kept in memory before they are written to the index, so a
# check cut short by closing Ficary still keeps most of what it learned.
_STAMP_FLUSH_EVERY = 25

# A recheck interval longer than any library can be old. Used to read
# what is waiting without counting anything as due for a check.
_NEVER_DUE_S = 10 ** 12


def _quiet(line: str) -> None:
    """Where the probe loop's per-story lines go: the debug log only.
    At launch one line at the end is the whole report."""
    logger.debug("%s", line.rstrip())


def library_roots(prefs) -> list[Path]:
    """The library folder and the separate adult folder, when set, that
    exist on disk: the two roots Ctrl+U checks."""
    from .. import prefs as _p

    roots: list[Path] = []
    for key in (_p.KEY_LIBRARY_PATH, _p.KEY_LIBRARY_ADULT_PATH):
        raw = (prefs.get(key) or "").strip()
        if not raw:
            continue
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            continue
        try:
            resolved = folder.resolve()
        except OSError:
            resolved = folder
        if all(resolved != existing for existing in roots):
            roots.append(resolved)
    return roots


def stories_waiting(root: Path, *, index_path: Optional[Path] = None) -> int:
    """How many stories in ``root`` have more chapters on their site than
    in their file, counted the way Ctrl+U will find them: unfinished
    stories whose last check found more chapters than the file has."""
    from .refresh import build_refresh_queue

    queue, _skipped = build_refresh_queue(
        root, index_path=index_path, skip_complete=True,
        recheck_interval_s=_NEVER_DUE_S, progress=_quiet,
    )
    return sum(
        1 for entry in queue
        if isinstance(entry.get("remote"), int) and entry["remote"] > (entry.get("local") or 0)
    )


def _has_stories(roots: Iterable[Path], index_path: Optional[Path]) -> bool:
    from .index import LibraryIndex

    try:
        index = LibraryIndex.load(index_path)
    except Exception:  # noqa: BLE001 - an unreadable index is reported elsewhere
        logger.debug("library index could not be read for the launch check", exc_info=True)
        return False
    for root in roots:
        for _url, _entry in index.stories_in(root):
            return True
    return False


def check_for_new_chapters(
    roots: Iterable[Path],
    *,
    cancel_event: threading.Event,
    index_path: Optional[Path] = None,
    recheck_interval_s: int = LAUNCH_RECHECK_INTERVAL_S,
    run_update_queue: Optional[Callable] = None,
) -> Optional[int]:
    """Ask each site for the chapter count of every unfinished story in
    ``roots`` not checked within ``recheck_interval_s``, record the
    answers in the library index, and return how many stories have new
    chapters waiting.

    Returns None when there is nothing to report on: no stories in the
    index for these folders, or ``cancel_event`` was set before the end
    (Stop, Ctrl+U, or the window closing). Nothing is downloaded.
    """
    from .. import cli
    from ..download_queue import site_slot
    from .index import LibraryIndex
    from .refresh import build_refresh_queue, default_refresh_args

    roots = list(roots)
    if not roots or not _has_stories(roots, index_path):
        return None
    run = run_update_queue or cli._run_update_queue

    for root in roots:
        if cancel_event.is_set():
            return None
        probe_queue, skipped = build_refresh_queue(
            root, index_path=index_path, skip_complete=True,
            recheck_interval_s=recheck_interval_s, progress=_quiet,
        )
        # Stories whose last check already found new chapters are known
        # to be waiting; only the rest need asking about.
        to_ask = [entry for entry in probe_queue if "remote" not in entry]
        if not to_ask:
            continue
        args = default_refresh_args(dry_run=True, recheck_interval_s=recheck_interval_s)
        answers: dict = {}
        answers_lock = threading.Lock()

        def flush_locked(root=root, answers=answers):
            if not answers:
                return
            try:
                index = LibraryIndex.load(index_path)
                index.mark_probed(root, dict(answers))
            except Exception:  # noqa: BLE001 - a lost batch is asked about again next time
                logger.warning("Could not record the launch check's answers", exc_info=True)
            answers.clear()

        def on_probe_complete(url, remote_count=None, answers=answers, flush_locked=flush_locked):
            with answers_lock:
                answers[url] = remote_count
                if len(answers) >= _STAMP_FLUSH_EVERY:
                    flush_locked()

        try:
            run(
                to_ask, args, args.probe_workers,
                skipped_count=len(skipped),
                label="Launch check",
                progress=_quiet,
                on_probe_complete=on_probe_complete,
                cancel_event=cancel_event,
                probe_guard=lambda site_name: site_slot(site_name, cancel_event=cancel_event),
            )
        finally:
            with answers_lock:
                flush_locked()

    if cancel_event.is_set():
        return None
    return sum(stories_waiting(root, index_path=index_path) for root in roots)


def waiting_summary(waiting: int) -> str:
    """The one line the status pane gets when the check is done."""
    if waiting <= 0:
        return "No stories in your library have new chapters."
    stories = "1 story in your library has" if waiting == 1 else f"{waiting} stories in your library have"
    them = "it" if waiting == 1 else "them"
    return (
        f"{stories} new chapters. Library > Check for Story Updates "
        f"(Ctrl+U) downloads {them}."
    )
