from __future__ import annotations

from wheel_screener.adapters.http import RateLimiter


class _FakeClock:
    """monotonic() reads `t`; sleep(s) advances `t` and records the wait."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_rate_limiter_throttles_when_window_full() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(2, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    # 2 free, then a 60s wait per extra pair as the window slides
    assert clock.sleeps == [60.0, 60.0]


def test_rate_limiter_disabled_when_nonpositive() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(0, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(100):
        limiter.acquire()
    assert clock.sleeps == []
