"""The put swap rule: when to buy back a used-up short put and put the cash into a new one.

An open put gets "used up" when the stock runs away from the strike: the put is nearly worthless,
and the cash behind it earns almost nothing for the rest of its life. This finds those and says
what to open instead.

The whole design problem is that it must NOT fire all the time — there is always some put
somewhere paying more — so two limits stand between "a better put exists" and "swap":

1. the fresh put on the SAME ticker must pay at least ``min_ratio`` times what the open one still
   pays. Same stock, same risk, so a gap can only mean the old put is used up — and a jumpy stock
   elsewhere on the list cannot drag this one out of a perfectly good position;
2. the extra premium over the old put's remaining days must clear ``min_extra`` after costs, which
   rules out small positions and puts with days left that would free the cash anyway.

Prices are taken from the side each trade actually crosses — the ASK to buy the old put back, the
BID to sell the new one — so most of the trading cost is inside the comparison, and a thinly
traded contract correctly looks worse than a liquid one.

The rule is a DRAFT (see docs: the two limits are starting values and it has not been backtested
against simply holding to expiry), which is why every verdict shows its numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from wheel_screener.core.models import SwapAction, SwapReview, SwapSuggestion


@dataclass(frozen=True)
class SwapParams:
    """The rule's limits. Starting values from the spec; none is backtested."""

    min_ratio: float = 2.0  # rule 1: the fresh put must pay this many times the old put
    min_extra: float = 100.0  # rule 2: dollars of extra premium, after cost
    swap_cost: float = 10.0  # commission plus the bid/ask loss the prices don't already carry
    top_n: int = 3  # other-ticker suggestions to show


DEFAULT_PARAMS = SwapParams()


@dataclass(frozen=True)
class OpenPut:
    """The open short put under review."""

    symbol: str
    strike: float
    days: int  # to expiry
    contracts: float
    spot: float | None
    ask: float | None  # what buying it back costs, per share


def put_yield(price: float, strike: float, days: int) -> float | None:
    """Yearly pay rate on the cash the strike locks up, as a decimal (0.22 = 22%)."""
    if strike <= 0 or days < 1 or price is None or price < 0:
        return None
    return price / strike * 365 / days


def review(
    old: OpenPut,
    same_ticker: SwapSuggestion | None,
    others: Sequence[SwapSuggestion] = (),
    list_median: float | None = None,
    params: SwapParams = DEFAULT_PARAMS,
) -> SwapReview:
    """Keep or swap, for one open short put.

    ``same_ticker`` is the ONE put the entry rules would open on this ticker today — one fixed
    pick, never the best of several, or the comparison drifts to the riskiest strike the rules
    allow. ``list_median`` is the median yield across the screen's picks, the fallback when this
    ticker has none (it dropped off the screen, reports before the new expiry, ...). The median
    and never the best: measuring against the best put on the list would fire constantly.
    """
    if old.spot is None:
        return SwapReview(
            action=SwapAction.NOT_APPLICABLE,
            reason="no share price, so there is no way to tell whether this put is used up",
        )
    if old.spot <= old.strike:
        return SwapReview(
            action=SwapAction.NOT_APPLICABLE,
            reason="the stock is at or below the strike — that is the assignment question "
                   "(keep, roll, or take the shares), not a swap",
        )
    if old.days < 1:
        return SwapReview(
            action=SwapAction.NOT_APPLICABLE,
            reason="it expires today: there is nothing to buy back",
        )
    if not old.ask:
        return SwapReview(
            action=SwapAction.NOT_APPLICABLE,
            reason="no ask for the open put, so the cost of buying it back is unknown",
        )

    fresh_yield, source = (
        (same_ticker.annualized_yield, "same ticker") if same_ticker is not None
        else (list_median, "list median")
    )
    if fresh_yield is None:
        return SwapReview(
            action=SwapAction.KEEP,
            reason="nothing passes the entry rules right now, so there is nothing to swap into",
        )

    old_yield = put_yield(old.ask, old.strike, old.days)
    if old_yield is None:  # unreachable via the guards above; a belt on the arithmetic
        return SwapReview(action=SwapAction.NOT_APPLICABLE, reason="the open put cannot be priced")
    cash = old.strike * 100 * old.contracts
    extra = (fresh_yield - old_yield) * cash * old.days / 365 - params.swap_cost
    common = {
        "old_yield": old_yield, "fresh_yield": fresh_yield, "fresh_source": source,
        "extra_premium": extra, "cash": cash, "days": old.days,
        "min_ratio": params.min_ratio, "min_extra": params.min_extra,
        "swap_cost": params.swap_cost,
    }

    if fresh_yield < params.min_ratio * old_yield:
        return SwapReview(
            action=SwapAction.KEEP, rule1_passed=False,
            reason=f"rule 1: a fresh put pays {fresh_yield / old_yield:.1f}x this one, under the "
                   f"{params.min_ratio:g}x the rule asks for",
            **common,
        )
    if extra < params.min_extra:
        return SwapReview(
            action=SwapAction.KEEP, rule1_passed=True, rule2_passed=False,
            reason=f"rule 2: the swap would collect ${extra:,.0f} more over the {old.days} days "
                   f"left, under the ${params.min_extra:,.0f} the rule asks for",
            **common,
        )

    suggestions = ([same_ticker] if same_ticker is not None else []) + list(others)[: params.top_n]
    return SwapReview(
        action=SwapAction.SWAP, rule1_passed=True, rule2_passed=True,
        reason="both rules passed: the cash behind this put would work materially harder "
               "somewhere else",
        suggestions=suggestions, **common,
    )


def list_median_yield(yields: Sequence[float]) -> float | None:
    """The median of the screen's picks — the fallback. None when the list is empty."""
    usable = [y for y in yields if y is not None]
    return median(usable) if usable else None
