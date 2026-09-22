"""Early assignment: will the holder of a short option exercise it before expiry?

The holder of an American option exercises early only when that pays more than SELLING the
option, and what selling would capture that exercising throws away is the option's remaining
time value. So every cause is the same test — is the time value left smaller than what
exercising now gains? — and only the gain differs:

* **Calls — the dividend.** Exercising the day before an ex-date collects the dividend. An
  in-the-money call whose time value is below it gets exercised then, and the writer loses the
  shares a day early and the dividend with them.
* **Puts — interest on the strike.** Exercising takes the strike in cash now instead of at
  expiry, and that cash earns interest meanwhile. A deep in-the-money put whose time value is
  below that interest gets exercised, any day. A dividend ahead DEFERS this: holding the put
  through the ex-date gains the drop, so exercise waits until it has passed.
* **Either — no time value at all.** Deep in the money or close to expiry, an option trading at
  parity gives up nothing when exercised, so it can be at any time.

Out of the money, exercising would cost the holder money, so it does not happen.

Not modelled, because a quote cannot show them: tender offers and mergers (exercise to tender
the shares) and hard-to-borrow stocks (calls trade under parity when borrowing is expensive).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from wheel_screener.core.models import (
    AssignmentCause,
    AssignmentRisk,
    Dividend,
    EarlyAssignment,
    OptionType,
)

# The short-term rate a put holder earns on the strike cash. A default, overridable per
# deployment (PORTFOLIO__CARRY_RATE); the verdict is not sensitive to a point either way.
DEFAULT_CARRY_RATE = 0.04
# Per share. At or under this an option trades at parity — nothing is lost by exercising it.
NO_TIME_VALUE = 0.05
# Time value under this multiple of the gain from exercising is close enough to watch.
THIN_FACTOR = 2.0


def interest_on_strike(strike: float, days: int, rate: float) -> float:
    """What the strike cash earns between now and ``days`` out, per share."""
    return strike * (math.exp(rate * max(days, 0) / 365.0) - 1.0)


def assess(
    option_type: OptionType,
    strike: float,
    spot: float | None,
    price: float | None,
    expiration: date,
    today: date,
    dividends: list[Dividend] | None = None,
    rate: float = DEFAULT_CARRY_RATE,
) -> EarlyAssignment | None:
    """The early-assignment verdict for one short option, or None when spot is unknown.

    ``price`` is the option's price per share — ideally the bid, which is what the holder could
    sell for instead. ``dividends`` are the ex-dates inside the contract's life.
    """
    if spot is None or spot <= 0:
        return None  # cannot even tell in the money from out
    ahead = sorted(
        (d for d in dividends or [] if today < d.ex_date <= expiration), key=lambda d: d.ex_date
    )
    is_call = option_type is OptionType.CALL
    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    base = {"in_the_money": intrinsic > 0, "intrinsic": intrinsic, "dividends": ahead}
    if intrinsic <= 0:
        # Out of the money: exercising would cost the holder money. A call's dividend still
        # carries a threshold, because it is what the position faces if the stock rises.
        return EarlyAssignment(
            risk=AssignmentRisk.LOW,
            on=(ahead[0].ex_date - timedelta(days=1)) if is_call and ahead else None,
            threshold=ahead[0].amount if is_call and ahead else None,
            time_value=None if price is None else price - intrinsic,
            **base,
        )
    if price is None:
        return EarlyAssignment(risk=AssignmentRisk.UNKNOWN, **base)
    time_value = price - intrinsic
    base["time_value"] = time_value

    if is_call:
        if ahead:
            first = ahead[0]
            eve = first.ex_date - timedelta(days=1)
            common = {"cause": AssignmentCause.DIVIDEND, "on": eve, "threshold": first.amount}
            if time_value < first.amount:
                return EarlyAssignment(risk=AssignmentRisk.LIKELY, **common, **base)
            if time_value < THIN_FACTOR * first.amount:
                return EarlyAssignment(risk=AssignmentRisk.POSSIBLE, **common, **base)
        if time_value <= NO_TIME_VALUE:
            return EarlyAssignment(
                risk=AssignmentRisk.LIKELY, cause=AssignmentCause.NO_TIME_VALUE,
                threshold=0.0, **base,
            )
        # without a dividend to collect, a call holder gains nothing by exercising early
        return EarlyAssignment(
            risk=AssignmentRisk.LOW, threshold=ahead[0].amount if ahead else None,
            on=(ahead[0].ex_date - timedelta(days=1)) if ahead else None, **base,
        )

    # puts: the gain is the interest on the strike, from now until expiry
    carry = interest_on_strike(strike, (expiration - today).days, rate)
    if ahead:
        # Holding through the ex-date gains the drop, so a rational holder waits for it; what
        # decides it afterwards is the interest over the life that remains from then.
        first = ahead[0]
        after = interest_on_strike(strike, (expiration - first.ex_date).days, rate)
        if time_value < max(after, NO_TIME_VALUE):
            return EarlyAssignment(
                risk=AssignmentRisk.POSSIBLE, cause=AssignmentCause.INTEREST, on=first.ex_date,
                deferred=True, threshold=after, **base,
            )
        return EarlyAssignment(
            risk=AssignmentRisk.LOW, on=first.ex_date, deferred=True, threshold=after, **base
        )
    if time_value <= NO_TIME_VALUE:
        return EarlyAssignment(
            risk=AssignmentRisk.LIKELY, cause=AssignmentCause.NO_TIME_VALUE, threshold=carry,
            **base,
        )
    if time_value < carry:
        return EarlyAssignment(
            risk=AssignmentRisk.LIKELY, cause=AssignmentCause.INTEREST, threshold=carry, **base
        )
    if time_value < THIN_FACTOR * carry:
        return EarlyAssignment(
            risk=AssignmentRisk.POSSIBLE, cause=AssignmentCause.INTEREST, threshold=carry, **base
        )
    return EarlyAssignment(risk=AssignmentRisk.LOW, threshold=carry, **base)
