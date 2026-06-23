"""Shared HTTP plumbing for provider adapters: a sliding-window rate limiter.

TODO(M1+): persistent, cross-run response caching (hishel). For now FmpClient keeps a
per-run in-memory cache to dedupe identical GETs within a single screen.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Allow at most ``max_per_minute`` ``acquire()`` calls in any rolling 60s window.

    ``monotonic`` and ``sleep`` are injectable for deterministic tests.
    ``max_per_minute <= 0`` disables limiting.
    """

    def __init__(
        self,
        max_per_minute: int,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max = max_per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()  # concurrent chain pulls share one limiter

    def acquire(self) -> None:
        if self._max <= 0:
            return
        with self._lock:
            self._evict(self._monotonic())
            if len(self._calls) >= self._max:
                wait = 60.0 - (self._monotonic() - self._calls[0])
                if wait > 0:
                    self._sleep(wait)
                self._evict(self._monotonic())
            self._calls.append(self._monotonic())

    def _evict(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
