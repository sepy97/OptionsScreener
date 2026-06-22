"""Stage 4 — strike selection: the cash-secured put to sell on a survivor.

Within the 30–45 DTE window, take the put nearest the target delta in each expiry, then
pick the expiry with the best annualized yield (subject to liquidity gates).
"""

from __future__ import annotations

from wheel_screener.core.models import ChainSnapshot, OptionContract, OptionType, ScreenCriteria
from wheel_screener.core.ranking import annualized_csp_yield


def nearest_to_delta(
    contracts: list[OptionContract],
    target_delta: float,
    option_type: OptionType = OptionType.PUT,
) -> OptionContract | None:
    """Return the contract whose delta is closest to ``target_delta`` (ignores wrong type
    / missing delta). For puts, delta is negative and ``target_delta`` is e.g. -0.20."""
    candidates = [c for c in contracts if c.option_type == option_type and c.delta is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.delta - target_delta))


def _premium(c: OptionContract) -> float | None:
    """Credit per share for selling the put — prefer mid, fall back to bid."""
    return c.mid if c.mid else c.bid


def put_yield(c: OptionContract) -> float | None:
    prem = _premium(c)
    if not prem or c.strike <= 0 or c.dte <= 0:
        return None
    return annualized_csp_yield(prem, c.strike, c.dte)


def select_put(snapshot: ChainSnapshot, criteria: ScreenCriteria) -> OptionContract | None:
    """Best cash-secured put for this underlying, or None if nothing qualifies.

    Gates: PUT, has delta, DTE in [min,max], |delta| <= max_abs_delta, open interest and
    bid/ask spread within limits. Among the per-expiry nearest-to-target-delta puts, pick
    the highest annualized yield.
    """
    eligible = [
        c
        for c in snapshot.contracts
        if c.option_type == OptionType.PUT
        and c.delta is not None
        and criteria.min_dte <= c.dte <= criteria.max_dte
        and abs(c.delta) <= criteria.max_abs_delta
        and (c.open_interest or 0) >= criteria.min_open_interest
        and (c.spread_pct is None or c.spread_pct <= criteria.max_bid_ask_spread_pct)
    ]
    if not eligible:
        return None

    target = criteria.target_delta
    best_per_expiry: dict[object, OptionContract] = {}
    for c in eligible:
        cur = best_per_expiry.get(c.expiration)
        if cur is None or abs(c.delta - target) < abs(cur.delta - target):
            best_per_expiry[c.expiration] = c

    priced = [c for c in best_per_expiry.values() if put_yield(c) is not None]
    if not priced:
        return None
    return max(priced, key=lambda c: put_yield(c) or 0.0)
