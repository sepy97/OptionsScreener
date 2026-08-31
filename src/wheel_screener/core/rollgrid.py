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
    # Why this cell's number should not be believed, or None if it can be. The grid quotes every
    # listed strike, including ones nobody trades — and an untraded contract's closing bid is a
    # market maker's parked placeholder, not a price. KGC's $30.5 put quoted 1.06 at 18 Sep and
    # 0.68 at 2 Oct: the bid FALLING as expiry extends, which no real option can do. Zero open
    # interest, no volume, a 90% spread. Shown, because a strike existing is worth knowing, but
    # never shown as a clean figure.
    untradeable: str | None = None

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
    # Whether the leg being BOUGHT BACK is itself thinly traded. Its ask is subtracted from every
    # cell in the table, so when that ask is a placeholder the whole grid is wrong at once — a
    # per-cell mark cannot say that, because the fault is not in any one cell.
    close_untradeable: str | None = None

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


# Enough rows to reach the money from a deep out-of-the-money strike without the panel growing
# without bound. Past this the range is trimmed from the far end, never from the money.
MAX_STRIKE_ROWS = 19


def _liquidity_problem(
    c: OptionContract, *, max_spread: float, spread_exempt: float,
    min_oi: int, min_volume: int, min_bid_size: int,
) -> str | None:
    """The screen's four measures — spread, open interest, volume, bid depth — or None if the
    contract passes all of them.

    The same measures, but NOT the same open-interest floor, and the difference is deliberate.
    The screen chooses a handful of names out of ~800 and can afford to demand 100 contracts
    outstanding; the grid describes ONE board and has to say which of these cells are real.
    Measured 2026-08-31 at the screen's floor: AAPL's at-the-money 32-day put, 88 open interest,
    came back "untradeable", and half of every board hatched — QCOM 65%, AAPL 50%, SOFI 50%.
    A mark that lands on everything is one the reader learns to skip past. At 10 the same boards
    read 58 / 35 / 23% while KGC, the board that prompted this, still hatches 81%.

    The other three carry over unchanged: they are already at their loosest useful setting, and
    it was the SPREAD that caught KGC's ghost strike, not the open interest.
    """
    if c.bid is None or c.ask is None or c.bid <= 0:
        return "no bid — nothing to sell into"
    gap, mid = c.ask - c.bid, (c.ask + c.bid) / 2
    if gap > spread_exempt and mid > 0 and gap / mid > max_spread:
        return f"{gap / mid:.0%} spread — no price you could work inside"
    if (c.open_interest or 0) < min_oi:
        return f"{c.open_interest or 0} open interest — nobody holds this"
    if (c.volume or 0) < min_volume:
        return "never traded today — the quote is a leftover"
    if (c.bid_size or 0) < min_bid_size:
        return f"{c.bid_size or 0} contracts bid — too thin to sell into"
    return None


def _pick_strikes(
    listed: set[float], around: float, spot: float | None, span: int
) -> list[float]:
    """Strikes worth rolling to, ordered high to low.

    Centred on the position's own strike AND stretched to reach spot, because those are two
    different places and the second is where the tradeable rolls are. Extrinsic peaks at the
    money, so a put sitting far out of it — QCOM's $150 against a $170 share — has its whole
    interesting range between the strike and the price, and a window of four either side of
    $150 contains none of it.

    The current strike is always included: it is the row the reader came to compare against.
    """
    ordered = sorted(listed)
    if not ordered:
        return []
    if around not in listed:
        around = min(ordered, key=lambda k: abs(k - (spot or around)))
    lo_anchor = hi_anchor = ordered.index(around)
    if spot is not None:
        at_money = ordered.index(min(ordered, key=lambda k: abs(k - spot)))
        lo_anchor, hi_anchor = min(lo_anchor, at_money), max(hi_anchor, at_money)
    lo = max(0, lo_anchor - span)
    hi = min(len(ordered) - 1, hi_anchor + span)

    # Trim from whichever end is further from the money, so a long reach never costs the rows
    # that matter. The held strike is protected because it is one of the two anchors.
    while hi - lo + 1 > MAX_STRIKE_ROWS:
        drop_low = abs(ordered[lo] - (spot or around)) >= abs(ordered[hi] - (spot or around))
        if drop_low and lo < lo_anchor:
            lo += 1
        elif not drop_low and hi > hi_anchor:
            hi -= 1
        elif lo < lo_anchor:
            lo += 1
        elif hi > hi_anchor:
            hi -= 1
        else:
            break  # everything left is between the anchors; keep it and let the grid scroll
    return sorted(ordered[lo : hi + 1], reverse=True)


def build(
    chain: list[OptionContract], *, strike: float, expiration: date, contracts: float,
    spot: float | None, today: date, option_type: OptionType = OptionType.PUT,
    collected: float | None = None, opened_on: date | None = None,
    strike_span: int = 4, expiry_count: int = 8,
    max_spread: float = 0.30, spread_exempt: float = 0.05,
    min_oi: int = 10, min_volume: int = 1, min_bid_size: int = 10,
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
            untradeable=(None if is_current else _liquidity_problem(
                c, max_spread=max_spread, spread_exempt=spread_exempt, min_oi=min_oi,
                min_volume=min_volume, min_bid_size=min_bid_size,
            )),
        )
    return RollGrid(
        strikes=strikes, expiries=expiries, cells=cells, current_strike=strike,
        current_expiry=expiration, close_cost=close_cost, collected=collected,
        contracts=contracts, opened_on=opened_on, today=today,
        close_untradeable=_liquidity_problem(
            current, max_spread=max_spread, spread_exempt=spread_exempt, min_oi=min_oi,
            min_volume=min_volume, min_bid_size=min_bid_size,
        )
    )
