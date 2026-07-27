"""The earnings guard — one place that answers "does a report land inside this contract's life?"

Why this exists as its own object: the rule is per *contract* (a date vs an expiration), but the
calendar is fetched per *screen*. Keeping the loaded dates and the verdict together lets the
chain stage ask the question at the only point where both dates are known, and lets the screen
and single-ticker paths share identical semantics.

Three-state on purpose (see ``EarningsStatus``): a missing date is ``UNKNOWN``, never ``CLEAN``.
``resolve`` upgrades an unknown by asking the provider for that one symbol — authoritative and
cheap, because it only ever runs on the handful of names that survive to the results table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

from wheel_screener.core.models import EarningsPolicy, EarningsStatus

logger = logging.getLogger(__name__)


class EarningsGuard:
    """Verdicts over a loaded calendar, with a per-symbol fallback for gaps.

    ``dates`` maps symbol -> next earnings date on/after ``today`` (within the loaded horizon).
    ``resolver`` is an optional ``(symbol, on_or_after) -> date | None`` used to settle unknowns.
    """

    def __init__(
        self,
        dates: dict[str, date],
        today: date,
        *,
        buffer_days: int = 0,
        policy: EarningsPolicy = EarningsPolicy.EXCLUDE,
        exclude_unknown: bool = True,
        resolver: Callable[[str, date], date | None] | None = None,
    ) -> None:
        self._dates = dict(dates)
        self._today = today
        self._buffer = timedelta(days=max(buffer_days, 0))
        self.policy = policy
        self.exclude_unknown = exclude_unknown
        self._resolver = resolver
        self._resolved: set[str] = set()  # symbols already sent to the resolver (ask once)

    @property
    def loaded(self) -> int:
        """How many symbols the calendar covers — 0 means the guard is blind."""
        return len(self._dates)

    def date_for(self, symbol: str) -> date | None:
        return self._dates.get(symbol)

    def resolve(self, symbol: str) -> date | None:
        """Settle a symbol against the authoritative per-symbol endpoint (once per symbol).

        Used before acting on an UNKNOWN, so a hole in the bulk calendar doesn't get mistaken
        for "this name has no upcoming report".
        """
        known = self._dates.get(symbol)
        if known is not None or self._resolver is None or symbol in self._resolved:
            return known
        self._resolved.add(symbol)
        try:
            found = self._resolver(symbol, self._today)
        except Exception:  # noqa: BLE001 - a lookup failure must not sink the whole screen
            logger.warning("earnings: per-symbol lookup failed for %s", symbol, exc_info=True)
            return None
        if found is not None:
            self._dates[symbol] = found
            logger.info("earnings: resolved %s -> %s (missing from the calendar)", symbol, found)
        return found

    def status(self, symbol: str, expiration: date) -> EarningsStatus:
        """The verdict for one contract. Does NOT hit the resolver — call ``resolve`` first
        when you're about to act on the answer (the chain stage runs this thousands of times)."""
        when = self._dates.get(symbol)
        if when is None:
            return EarningsStatus.UNKNOWN
        # buffer extends the danger zone past expiry: an unconfirmed date can move earlier
        return EarningsStatus.SPANS if when <= expiration + self._buffer else EarningsStatus.CLEAN

    def blocks(self, symbol: str, expiration: date) -> bool:
        """Should this contract be filtered out? Honors ``policy`` and the unknown setting."""
        if self.policy is not EarningsPolicy.EXCLUDE:
            return False
        status = self.status(symbol, expiration)
        if status is EarningsStatus.SPANS:
            return True
        return status is EarningsStatus.UNKNOWN and self.exclude_unknown

    def blocks_every_expiry(self, symbol: str, earliest_expiry: date) -> bool:
        """True when a report lands on/before even the NEAREST candidate expiry — so no expiry in
        the window can be clean. The only name-level shortcut that is safe: it skips a chain pull
        we know would produce nothing, without vetoing names whose later report leaves an early
        expiry perfectly sellable."""
        if self.policy is not EarningsPolicy.EXCLUDE:
            return False
        when = self._dates.get(symbol)
        return when is not None and when <= earliest_expiry + self._buffer
