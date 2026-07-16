"""Per-IP sliding-window rate limiting for the expensive web endpoints.

The public instance runs screens/searches on shared API keys and a small droplet, so a few
endpoints (screen starts + ticker search) are throttled per client IP. Cheap reads — the
dashboard, the 2s job polling, ``/health``, ``/static`` — are never limited. The app runs as a
single uvicorn worker, so an in-memory window is sufficient (no Redis).
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Allow at most ``per_window`` hits per key within a rolling ``window_seconds``.

    Pure/testable: the caller passes the current monotonic time to :meth:`allow`. Bounded memory —
    once the key count exceeds ``max_keys`` a sweep drops entries whose window has fully expired,
    so a flood of one-off IPs can't grow the map without limit.
    """

    def __init__(
        self, per_window: int, window_seconds: float = 60.0, max_keys: int = 10_000
    ) -> None:
        self._max = per_window
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        """Record a hit for ``key`` at ``now``; return False if it exceeds the window budget."""
        with self._lock:
            if len(self._hits) > self._max_keys:
                self._sweep(now)
            dq = self._hits[key]
            cutoff = now - self._window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self._max:
                return False
            dq.append(now)
            return True

    def _sweep(self, now: float) -> None:
        cutoff = now - self._window
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]
        for k in stale:
            del self._hits[k]


# "expensive" = triggers external API calls + heavy compute, vs a cheap read of stored state.
# Matched on the exact start endpoints (not the cancel/poll/detail reads under the same prefixes).
def is_expensive(method: str, path: str) -> bool:
    if method == "POST" and path in ("/runs", "/screen", "/search"):
        return True  # start a screen (HTML + JSON) / run a ticker search
    return method == "GET" and path == "/search/export.csv"  # a fresh search behind a download


def client_ip(forwarded_for: str | None, peer: str) -> str:
    """Real client IP behind exactly one trusted proxy (Caddy), else the direct peer.

    Caddy APPENDS the true client to X-Forwarded-For, so the real IP is the LAST hop. We must NOT
    trust the first hop: a client can forge earlier entries (and rotate them to dodge the per-IP
    limit), but cannot control the value Caddy appends. Assumes a single proxy in front (no CDN).
    """
    if forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return peer or "unknown"
