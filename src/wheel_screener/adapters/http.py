"""Shared HTTP plumbing for provider adapters: a sliding-window rate limiter.

TODO(M1+): persistent, cross-run response caching (hishel). For now FmpClient keeps a
per-run in-memory cache to dedupe identical GETs within a single screen.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TypeVar

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

_T = TypeVar("_T")


def is_retryable(exc: BaseException) -> bool:
    """A transient HTTP failure worth retrying: 429, any 5xx, or a transport error."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


def run_with_retry(
    fn: Callable[[], _T],
    *,
    max_attempts: int = 4,
    multiplier: float = 1.0,
    max_wait: float = 30.0,
) -> _T:
    """Call ``fn`` with exponential-backoff retry on transient HTTP failures.

    Re-raises the final exception when attempts are exhausted (so the caller maps it).
    ``multiplier=0`` disables the backoff wait (used in tests).
    """
    retryer: Retrying = Retrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=multiplier, max=max_wait),
        retry=retry_if_exception(is_retryable),
    )
    return retryer(fn)


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
