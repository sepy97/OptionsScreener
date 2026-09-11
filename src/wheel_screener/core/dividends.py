"""Ex-dividend dates inside a contract's life, and what they do to it.

A flag, never a filter — and the reason is worth keeping next to the code. An earnings report
moves the stock by an amount nobody knows in advance; an ex-dividend date moves it by the
dividend, on a date announced weeks ahead. The options market prices that drop from the day it
is declared (put-call parity on live chains shows it: expiries after VZ's ex-date implied the
dividend, expiries before it implied none), so a short put's value does not jump when the stock
opens lower. Filtering those contracts would drop a quarter to a third of a screen — mostly the
dividend-paying stalwarts the fundamentals favour — for no reduction in risk.

What the dividend DOES change, and what the warning explains:

* for a put, the cushion. "7% out of the money" is measured against today's price, and the
  price the contract will actually be judged against is lower by the dividend;
* for a call, early assignment. An in-the-money call whose time value is below the dividend
  gets exercised the day before the ex-date, and the writer loses the shares and the dividend.

Most ex-dates inside a 45-day contract are not announced when it is sold — companies declare a
few weeks ahead — so a regular payer's next dates are projected from its schedule and marked
``estimated``. Showing only announced ones would read as "no dividend" for exactly the dates
furthest out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from wheel_screener.core.models import Dividend, OptionType

# Days between ex-dates for each regular schedule. Irregular and special dividends are shown
# when announced but never projected: there is no schedule to extend.
SCHEDULE_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 91,
    "semi-annual": 182,
    "annual": 365,
}

# How long past a missed date a schedule still counts as live. Past this the payer has most
# likely suspended the dividend, and projecting it would invent money that is not coming.
_LAPSE_AFTER_STEPS = 1.5


def upcoming(history: list[Dividend], today: date, until: date) -> list[Dividend]:
    """Ex-dividends after ``today`` and on or before ``until``, earliest first.

    Every announced one, plus the regular schedule projected past the last known ex-date. A
    projection that is already overdue (due, but nothing announced yet) is placed tomorrow: it
    is coming, and "any day now" falls inside every contract being considered.
    """
    known = sorted(history, key=lambda d: d.ex_date)
    out = [d for d in known if today < d.ex_date <= until]

    regular = [d for d in known if d.frequency in SCHEDULE_DAYS and not d.estimated]
    if regular:
        anchor = regular[-1]
        step = SCHEDULE_DAYS[anchor.frequency]
        lapse = today - timedelta(days=int(step * _LAPSE_AFTER_STEPS))
        if anchor.ex_date >= lapse:
            est = max(anchor.ex_date + timedelta(days=step), today + timedelta(days=1))
            while est <= until:
                out.append(Dividend(
                    ex_date=est, amount=anchor.amount, frequency=anchor.frequency,
                    estimated=True,
                ))
                est += timedelta(days=step)
    return sorted(out, key=lambda d: d.ex_date)


def in_life(history: list[Dividend], today: date, expiration: date) -> list[Dividend]:
    """The ex-dividends a contract expiring on ``expiration`` lives through.

    On the expiry day itself counts: the stock opens ex-dividend that morning and the contract
    settles against that price.
    """
    return upcoming(history, today, expiration)


@dataclass(frozen=True)
class DividendImpact:
    """What the dividends inside one contract's life do to it, in the numbers the UI explains."""

    first: date  # the first ex-date the contract lives through
    first_amount: float  # per share, on that date — what an early exercise is weighed against
    total: float  # per share, every ex-dividend inside the life
    count: int
    estimated: bool  # at least one of them projected rather than announced
    frequency: str | None
    pct_of_spot: float | None  # total as a fraction of the share price
    per_contract: float  # total × 100 shares
    # Distance from the share price to the strike, as a fraction of the price: positive means
    # out of the money. Measured today and against the price net of the dividend.
    cushion_now: float | None
    cushion_after: float | None
    # calls only — the day an in-the-money call is exercised to collect the dividend
    exercise_eve: date | None
    time_value: float | None  # premium beyond intrinsic, now
    early_assignment_likely: bool  # in the money now, with less time value than the dividend


def impact(
    dividends: list[Dividend],
    option_type: OptionType,
    strike: float,
    spot: float | None,
    premium: float | None = None,
) -> DividendImpact | None:
    """The effect of ``dividends`` on one contract, or None when there are none."""
    if not dividends:
        return None
    ordered = sorted(dividends, key=lambda d: d.ex_date)
    total = sum(d.amount for d in ordered)
    first = ordered[0]
    priced = spot is not None and spot > 0
    after = spot - total if priced else None

    def cushion(price: float | None) -> float | None:
        if price is None or price <= 0:
            return None
        gap = price - strike if option_type is OptionType.PUT else strike - price
        return gap / price

    exercise_eve = time_value = None
    likely = False
    if option_type is OptionType.CALL:
        exercise_eve = first.ex_date - timedelta(days=1)
        if priced and premium is not None:
            time_value = premium - max(0.0, spot - strike)
            likely = spot > strike and time_value < first.amount

    return DividendImpact(
        first=first.ex_date,
        first_amount=first.amount,
        total=total,
        count=len(ordered),
        estimated=any(d.estimated for d in ordered),
        frequency=first.frequency,
        pct_of_spot=(total / spot) if priced else None,
        per_contract=total * 100,
        cushion_now=cushion(spot if priced else None),
        cushion_after=cushion(after),
        exercise_eve=exercise_eve,
        time_value=time_value,
        early_assignment_likely=likely,
    )
