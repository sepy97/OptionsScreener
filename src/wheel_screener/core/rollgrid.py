"""The roll surface: every strike against every expiry, priced in one view.

A ladder answers "how long should I roll for" and a second ladder answers "to what strike", but
a roll is one decision over two axes and the interesting cells are the diagonal ones. Repricing a
strike at a time to compare them is the interaction the grid removes.

Each cell is the NET CREDIT PER SHARE against buying the current leg back — the number that
would actually hit the account — with credit per added day beneath it, because a bigger credit
bought with more time is not obviously better and dividing settles it. The current expiry's
column has no added days to divide by, so it carries assignment odds instead, which is the other
thing a strike choice is really about.

Odds are |delta|, the market's own estimate of finishing in the money. It is a working
approximation rather than a probability — delta is the hedge ratio and only equals the
risk-neutral exercise chance under assumptions that do not quite hold — but it is what every
options desk uses for this, and it is quoted rather than modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wheel_screener.core.models import OptionContract, OptionType

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class GridCell:
    strike: float
    expiration: date
    added_days: int
    net_credit: float | None = None       # per share, against buying the current leg back
    per_day: float | None = None          # that credit spread over the days it adds
    assignment_odds: float | None = None  # |delta|
    open_interest: int | None = None
    is_current: bool = False

    contracts: float = 1.0

    @property
    def credit_total(self) -> float | None:
        """What the roll actually books, for the contracts held."""
        if self.net_credit is None:
            return None
        return self.net_credit * CONTRACT_MULTIPLIER * self.contracts

    @property
    def per_day_total(self) -> float | None:
        """That total spread over the days it adds."""
        total = self.credit_total
        if total is None or self.added_days <= 0:
            return None
        return total / self.added_days


@dataclass(frozen=True)
class RollGrid:
    strikes: list[float]
    expiries: list[date]
    cells: dict[tuple[float, date], GridCell]
    current_strike: float
    current_expiry: date
    close_cost: float          # per share: the ask, what buying the leg back costs
    collected: float | None    # per share, what was originally sold — None if the broker is quiet
    contracts: float
    opened_on: date | None = None   # from the broker's transactions; None past its 60-day window
    today: date | None = None

    def cell(self, strike: float, expiration: date) -> GridCell | None:
        return self.cells.get((strike, expiration))

    @property
    def days_held(self) -> int | None:
        if self.opened_on is None or self.today is None:
            return None
        return (self.today - self.opened_on).days

    @property
    def planned_yield(self) -> float | None:
        """What the trade was sold FOR, judged when it was sold — not what it is worth now."""
        if self.collected is None or self.opened_on is None:
            return None
        original_dte = (self.current_expiry - self.opened_on).days
        if original_dte <= 0 or self.current_strike <= 0:
            return None
        return (self.collected / self.current_strike) * (365.0 / original_dte)

    @property
    def realised_yield(self) -> float | None:
        """What closing right now would have earned, over the days actually held.

        The counterpart to ``planned_yield``: one is the trade you intended, the other the trade
        you would have made. Capturing 41% of the premium in half the time beats holding to
        expiry, and only these two numbers side by side say so.
        """
        held = self.days_held
        if self.collected is None or not held or held <= 0 or self.current_strike <= 0:
            return None
        return ((self.collected - self.close_cost) / self.current_strike) * (365.0 / held)

    @property
    def captured(self) -> float | None:
        """Fraction of the original premium already earned. None without a cost basis."""
        if not self.collected or self.collected <= 0:
            return None
        return (self.collected - self.close_cost) / self.collected

    @property
    def close_books(self) -> float | None:
        """Cash the position realises if closed right now — the roll-to-nothing case.

        Closing IS a cell of this grid with no new leg, so it is priced the same way and belongs
        beside the rolls rather than in a separate view.
        """
        if self.collected is None:
            return None
        return (self.collected - self.close_cost) * CONTRACT_MULTIPLIER * self.contracts


def _pick_strikes(listed: set[float], around: float, spot: float | None, span: int) -> list[float]:
    """``span`` strikes either side of the position's own, ordered high to low.

    Centred on the CURRENT strike rather than on spot: the question is what to roll this
    position to, and a grid that wanders off with the share price stops containing the row the
    reader came to compare against.
    """
    ordered = sorted(listed)
    if around not in listed and ordered:
        around = min(ordered, key=lambda k: abs(k - (spot or around)))
    if around not in listed:
        return []
    i = ordered.index(around)
    return sorted(ordered[max(0, i - span): i + span + 1], reverse=True)


def build(
    chain: list[OptionContract], *, strike: float, expiration: date, contracts: float,
    spot: float | None, today: date, option_type: OptionType = OptionType.PUT,
    collected: float | None = None, opened_on: date | None = None,
    strike_span: int = 4, expiry_count: int = 8,
) -> RollGrid | None:
    """Price every listed strike against every listed expiry, from the current leg outward."""
    own = [c for c in chain if c.option_type is option_type]
    current = next(
        (c for c in own if c.strike == strike and c.expiration == expiration), None
    )
    if current is None or current.ask is None:
        return None
    close_cost = current.ask

    expiries = sorted({c.expiration for c in own if c.expiration >= expiration})[:expiry_count]
    # Only strikes that are actually QUOTED somewhere in the window. A chain lists every
    # increment it has ever issued, and the odd ones between the round numbers trade so thinly
    # they have no bid at all — counting them into the span spends half the grid on empty rows.
    quoted = {
        c.strike for c in own
        if c.expiration in expiries and c.bid is not None and c.bid > 0
    }
    strikes = _pick_strikes(quoted, strike, spot, strike_span)
    if not strikes or not expiries:
        return None

    cells: dict[tuple[float, date], GridCell] = {}
    for c in own:
        if c.strike not in strikes or c.expiration not in expiries:
            continue
        added = (c.expiration - expiration).days
        is_current = c.strike == strike and c.expiration == expiration
        credit = None if c.bid is None else round(c.bid - close_cost, 2)
        cells[(c.strike, c.expiration)] = GridCell(
            strike=c.strike, expiration=c.expiration, added_days=added,
            net_credit=credit,
            # No added days on the current expiry, so no per-day figure exists there; that
            # column carries the odds instead rather than a divide-by-zero dressed as data.
            per_day=(round(credit / added, 4) if credit is not None and added > 0 else None),
            contracts=contracts,
            assignment_odds=(abs(c.delta) if c.delta is not None else None),
            open_interest=c.open_interest, is_current=is_current,
        )
    return RollGrid(
        strikes=strikes, expiries=expiries, cells=cells, current_strike=strike,
        current_expiry=expiration, close_cost=close_cost, collected=collected,
        contracts=contracts, opened_on=opened_on, today=today,
    )
