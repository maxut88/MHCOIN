"""Rate limiting helpers for adversarial / DoS defenses."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class RateLimiter:
    """Sliding-window event counter keyed by peer/host identity."""

    def __init__(self, *, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """Record one event. Returns False if over limit (event still counted)."""
        now = time.time()
        with self._lock:
            stamps = self._events[key]
            stamps[:] = [t for t in stamps if now - t < self.window]
            stamps.append(now)
            return len(stamps) <= self.limit

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
