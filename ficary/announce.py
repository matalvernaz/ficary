"""Saying a short message through the user's screen reader, wherever focus is.

Part of the library-first main window: the screen reader says when a
download, a library update or an audiobook finishes, so nobody has to
go and find the status pane to learn it. The status pane still gets
every line; this is said as well, never instead.

It goes through prism (PyPI ``prismatoid``), a maintained library that
reaches NVDA, JAWS, VoiceOver and Orca from Python. Orca needs version 49
or later. Narrator is not reached: prism talks to it only when asked for
it by name, and ``create_best`` never picks it. Two rules shape it:

* Only a screen reader speaks. With none running, prism's best choice
  is the computer's own voice, and Ficary must not start talking out
  loud to someone who does not use a screen reader. A message then goes
  unsaid.
* One thread does all of it. The Windows screen readers are COM objects
  tied to the thread that made them, so the prism context and every call
  into it live on a single daemon thread fed by a queue. ``announce()``
  only puts the message on the queue, so it never holds up the caller.

A fresh backend is made for each message (``create_best``, never
``acquire_best``, which can hand back a reader that has since restarted
and no longer listens). Messages are rare, a few a minute at most, so
that costs nothing worth saving.

Anything that goes wrong, prism missing from a source install included,
is logged at debug level and the message goes unsaid.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_queue: "queue.Queue[Optional[str]]" = queue.Queue()
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()


def _clean(text: str) -> str:
    # prism refuses empty text and embedded NULs with an exception.
    return " ".join((text or "").replace("\x00", " ").split())


def announce(text: str) -> None:
    """Have the screen reader say ``text``, if one is running. Returns at
    once; the speaking happens on the speech thread."""
    message = _clean(text)
    if not message:
        return
    _ensure_thread()
    _queue.put(message)


def _ensure_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_speak_forever, name="ficary-announce", daemon=True)
        _thread.start()


def _speak_forever() -> None:
    context = None
    unavailable = False
    while True:
        message = _queue.get()
        if message is None:
            return
        if unavailable:
            continue
        try:
            if context is None:
                import prism  # optional: the "announce" extra

                context = prism.Context()
            _speak_with(context, message)
        except ImportError:
            # Not installed (a source install without the extra). It will
            # not appear while the program runs, so stop trying.
            logger.debug("prism is not installed; announcements are off", exc_info=True)
            unavailable = True
        except Exception:  # noqa: BLE001 - a message that cannot be said is just not said
            logger.debug("Could not announce %r", message, exc_info=True)


def _speak_with(context, message: str) -> bool:
    """Say ``message`` through the best backend if it is a screen reader.
    Returns whether it was said."""
    backend = context.create_best()
    features = backend.features
    if not features.is_screen_reader:
        return False
    if features.supports_output:
        # Speech and, where the reader has a display, braille.
        backend.output(message, interrupt=False)
    elif features.supports_speak:
        backend.speak(message, interrupt=False)
    else:
        return False
    return True


@dataclass(frozen=True)
class AnnounceCheck:
    """What ``ficary --check-announcements`` found.

    ``working`` is whether prism loaded and started: what a frozen build
    has to prove. ``screen_reader`` is whether one is running now, which
    on a build machine it never is.
    """

    working: bool
    backend: str = ""
    screen_reader: bool = False
    detail: str = ""


def check(message: str = "Ficary can talk to your screen reader.") -> AnnounceCheck:
    """Load prism, find the best backend, and if it is a screen reader say
    ``message`` through it. Runs on the calling thread, for the check
    command only."""
    try:
        import prism

        context = prism.Context()
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return AnnounceCheck(working=False, detail=f"{type(error).__name__}: {error}")
    try:
        backend = context.create_best()
    except Exception as error:  # noqa: BLE001 - prism works; nothing here can speak
        return AnnounceCheck(working=True, detail=f"no voice of any kind is available ({error})")
    name = str(getattr(backend, "name", "") or "")
    try:
        if not backend.features.is_screen_reader:
            return AnnounceCheck(working=True, backend=name)
        said = _speak_with(context, message)
    except Exception as error:  # noqa: BLE001
        return AnnounceCheck(working=True, backend=name, screen_reader=True, detail=f"{type(error).__name__}: {error}")
    return AnnounceCheck(working=True, backend=name, screen_reader=said)
