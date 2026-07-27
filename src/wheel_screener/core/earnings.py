"""The earnings guard — one place that answers "does a report land inside this contract's life?"

Why this exists as its own object: the rule is per *contract* (a date vs an expiration), but the
calendar is loaded per *request*. Keeping the loaded dates and the verdict together lets the chain
stage ask the question at the only point where both dates are known, and lets the screen and
single-ticker paths share identical semantics.

The three states (see ``EarningsStatus``) turn on ``covers_through`` — how far the loaded calendar
is *vouched* to be complete:

* covered, symbol absent  -> CLEAN. Absence from a verified sweep is a positive fact: nobody
  reports in that range without appearing in it. This is what makes a narrow sweep sufficient —
  we only ever need the window a contract actually lives in.
* not covered that far    -> UNKNOWN. Absence proves nothing, so it must not read as safe. This
  is the conflation that let an earnings-spanning contract reach the results table (issue #113).

The coverage guarantee is load-bearing, so it has to come from a checked fetch (see
``verify_calendar_coverage``), never from an assumption about the provider.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from wheel_screener.core.models import EarningsPolicy, EarningsStatus

logger = logging.getLogger(__name__)


class EarningsGuard:
    """Verdicts over a loaded calendar.

    ``dates`` maps symbol -> next earnings date on/after ``today``. ``covers_through`` is the last
    date the calendar is known to be complete for; leave it None when the load can't vouch for a
    range (a single-symbol lookup), and absence then reads as UNKNOWN rather than CLEAN.
    """

    def __init__(
        self,
        dates: dict[str, date],
        today: date,
        *,
        buffer_days: int = 0,
        policy: EarningsPolicy = EarningsPolicy.EXCLUDE,
        exclude_unknown: bool = True,
        covers_through: date | None = None,
    ) -> None:
        self._dates = dict(dates)
        self._today = today
        self._buffer = timedelta(days=max(buffer_days, 0))
        self.policy = policy
        self.exclude_unknown = exclude_unknown
        self.covers_through = covers_through

    @property
    def loaded(self) -> int:
        """How many symbols the calendar covers — 0 means the guard is blind."""
        return len(self._dates)

    def date_for(self, symbol: str) -> date | None:
        return self._dates.get(symbol)

    def status(self, symbol: str, expiration: date) -> EarningsStatus:
        """The verdict for one contract."""
        deadline = expiration + self._buffer  # an unconfirmed date can drift earlier
        when = self._dates.get(symbol)
        if when is not None:
            return EarningsStatus.SPANS if when <= deadline else EarningsStatus.CLEAN
        # No row. Only the sweep's coverage can turn that into an answer.
        if self.covers_through is not None and deadline <= self.covers_through:
            return EarningsStatus.CLEAN
        return EarningsStatus.UNKNOWN

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
