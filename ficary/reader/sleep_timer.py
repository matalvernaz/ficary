"""Sleep timer for the reader: stop reading after a set delay.

Pure logic (no wx, no audio) so it's unit-testable. The reader wires
``on_expire`` to stop live TTS and fade the soundscape out.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

MIN_MINUTES = 5
MAX_MINUTES = 120


class SleepTimer:
    def __init__(self, on_expire: Callable[[], None]):
        self._on_expire = on_expire
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[float] = None  # time.monotonic() target

    def start(self, minutes: int) -> int:
        """Start (or restart) the timer. Clamps to [MIN, MAX]; returns the
        clamped minute count actually used."""
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(minutes)))
        self.cancel()
        secs = minutes * 60
        self._deadline = time.monotonic() + secs
        self._timer = threading.Timer(secs, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return minutes

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._deadline = None

    def _fire(self) -> None:
        self._timer = None
        self._deadline = None
        self._on_expire()

    @property
    def active(self) -> bool:
        return self._deadline is not None

    def remaining_seconds(self) -> int:
        if self._deadline is None:
            return 0
        return max(0, int(round(self._deadline - time.monotonic())))
