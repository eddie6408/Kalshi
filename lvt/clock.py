"""Clocks. The engine never calls time.time() directly - it asks a clock, so
replay runs on recorded time and can never peek past the current event."""
from __future__ import annotations

import time


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ReplayClock:
    def __init__(self, start: float = 0.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance_to(self, t: float) -> None:
        if t < self._t:
            raise ValueError(f"replay clock cannot go backwards ({t} < {self._t})")
        self._t = t

    def sleep(self, seconds: float) -> None:
        self._t += max(seconds, 0.0)
