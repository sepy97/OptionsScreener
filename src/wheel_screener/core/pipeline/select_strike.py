"""Stage 4 — strike selection: the cash-secured put(s) to sell.

For the market screen we take ONE put per name (nearest the target delta, best yield).
For a single-ticker search we return the top-N puts nearest the target delta (one per expiry),
so you can compare expiries at a consistent moneyness. Both share the same sellability gates.
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


def credited_premium(c: OptionContract) -> float | None:
    """Conservative credit per share for selling the put: the BID (a price you can
    actually be filled at by hitting the bid). The midpoint is reported separately for
    reference but never credited, so the headline yield isn't optimistic vs. a real fill.
    """
    return c.bid


def put_yield(c: OptionContract) -> float | None:
    prem = credited_premium(c)
    if not prem or prem <= 0 or c.strike <= 0 or c.dte <= 0:
        return None
    return annualized_csp_yield(prem, c.strike, c.dte)


def _eligible_puts(snapshot: ChainSnapshot, criteria: ScreenCriteria) -> list[OptionContract]:
    """Puts that pass the sellability gates: PUT with a delta, DTE within [min,max] (±tolerance),
    |delta| <= max_abs_delta, open interest >= min, a real sellable bid (>0), and a computable
    bid/ask spread within the limit."""
    lo, hi, tol = criteria.min_dte, criteria.max_dte, criteria.dte_tolerance
    return [
        c
        for c in snapshot.contracts
        if c.option_type == OptionType.PUT
        and c.delta is not None
        and (lo - tol) <= c.dte <= (hi + tol)
        and abs(c.delta) <= criteria.max_abs_delta
        and (c.open_interest or 0) >= criteria.min_open_interest
        and c.bid is not None
        and c.bid > 0
        and c.spread_pct is not None
        and c.spread_pct <= criteria.max_bid_ask_spread_pct
    ]


def _best_put_per_expiry(
    puts: list[OptionContract], target_delta: float
) -> list[OptionContract]:
    """The put nearest ``target_delta`` in each expiry, keeping only priceable ones."""
    best: dict[object, OptionContract] = {}
    for c in puts:
        cur = best.get(c.expiration)
        if cur is None or abs(c.delta - target_delta) < abs(cur.delta - target_delta):
            best[c.expiration] = c
    return [c for c in best.values() if put_yield(c) is not None]


def select_put(snapshot: ChainSnapshot, criteria: ScreenCriteria) -> OptionContract | None:
    """Best cash-secured put for this underlying, or None if nothing qualifies.

    By default (dte_tolerance == 0) results stay strictly within [min_dte, max_dte]. A positive
    dte_tolerance also admits expiries within ±tol, preferring in-band ones. Among the chosen
    expiries' per-expiry nearest-to-target-delta puts, pick the highest yield.
    """
    priced = _best_put_per_expiry(_eligible_puts(snapshot, criteria), criteria.target_delta)
    if not priced:
        return None
    lo, hi = criteria.min_dte, criteria.max_dte
    in_band = [c for c in priced if lo <= c.dte <= hi]
    pool = in_band if in_band else priced  # prefer in-band; else the nearest expiry within tol
    return max(pool, key=lambda c: put_yield(c) or 0.0)


def select_top_puts(
    snapshot: ChainSnapshot, criteria: ScreenCriteria, n: int
) -> list[OptionContract]:
    """The N cash-secured puts nearest ``target_delta`` (one per expiry) — for a single-ticker
    search. Same sellability gates as ``select_put``; selects the N expiries whose nearest-to-
    target put is closest to the target delta, returned earliest-expiry-first (term structure)."""
    priced = _best_put_per_expiry(_eligible_puts(snapshot, criteria), criteria.target_delta)
    priced.sort(key=lambda c: abs(c.delta - criteria.target_delta))  # pick the N nearest target
    top = priced[: max(n, 0)]
    top.sort(key=lambda c: c.dte)  # display order: earliest expiry first
    return top
