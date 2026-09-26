"""A short-lived cache partitioned by user, and bounded.

The partition is the whole point. Two caches on the Portfolio tab hold account data, and both were
keyed by the thing being cached and nothing else — the balances by nothing at all. That is correct
for one person and serves one person's numbers to the next as soon as there are two:

* balances: a 30-second entry holding every account. The second visitor inside the window would be
  handed the first one's balances and positions.
* keep-or-swap verdicts: keyed by ``(symbol, quantity, strike, expiration)``, so two people short
  the same contract in the same size shared an entry. Its contents happen to be correct for both,
  because a verdict is computed only from fields already in that key or from market data — but it
  is one field away from not being. The moment a verdict carries the premium collected or the date
  opened, the same collision serves one person's entry price to another.

So the user is part of every key here, and a route cannot spell one that reaches somebody else's
entry: the user is supplied by the dependency that resolved the session, never by a caller.

Bounded too, which the caches it replaces were not. The verdict cache grew for the life of the
process and was emptied only by the Refresh button.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

# Enough for a friends-sized deployment several times over; the cap exists so an attacker cycling
# sessions cannot grow this without bound, not to ration real users.
_MAX_USERS = 64
_MAX_ENTRIES_PER_USER = 256


class PerUserCache:
    """TTL cache of ``{user: {key: value}}``, least-recently-used eviction at both levels.

    Thread-safe: the web app serves sync endpoints from a thread pool, so two requests for the
    same user really can be here at once.
    """

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_users: int = _MAX_USERS,
        max_entries_per_user: int = _MAX_ENTRIES_PER_USER,
        clock=time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_users = max_users
        self._max_entries = max_entries_per_user
        self._clock = clock
        self._lock = threading.Lock()
        # user -> key -> (stored_at, value)
        self._users: OrderedDict[str, OrderedDict[Any, tuple[float, Any]]] = OrderedDict()

    def get(self, user: str | None, key: Any = None) -> Any | None:
        """The live value for this user's key, or None. An expired entry is dropped on sight.

        A ``user`` of None — no session — never hits: an unidentified caller has no partition to
        read, and silently sharing one would be the bug this class exists to prevent.
        """
        if user is None:
            return None
        with self._lock:
            entries = self._users.get(user)
            if entries is None:
                return None
            hit = entries.get(key)
            if hit is None:
                return None
            stored_at, value = hit
            if self._clock() - stored_at >= self._ttl:
                del entries[key]
                return None
            self._users.move_to_end(user)
            entries.move_to_end(key)
            return value

    def put(self, user: str | None, key: Any, value: Any) -> None:
        """Store a value for this user. A ``user`` of None stores nothing, for the reason above."""
        if user is None:
            return
        with self._lock:
            entries = self._users.get(user)
            if entries is None:
                entries = self._users[user] = OrderedDict()
            entries[key] = (self._clock(), value)
            entries.move_to_end(key)
            self._users.move_to_end(user)
            while len(entries) > self._max_entries:
                entries.popitem(last=False)
            while len(self._users) > self._max_users:
                self._users.popitem(last=False)

    def clear(self, user: str | None) -> None:
        """Forget everything cached for ONE user — a relink, a disconnect, a Refresh press.

        Never clears anyone else: the old code emptied a single process-wide dict, which under
        multiple users would be one person's Refresh button costing everybody their cache.
        """
        if user is None:
            return
        with self._lock:
            self._users.pop(user, None)

    def __len__(self) -> int:
        """Users currently holding entries. For tests and diagnostics."""
        with self._lock:
            return len(self._users)
