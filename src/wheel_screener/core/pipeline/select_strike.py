"""Stage 4 — strike selection: the contract(s) to sell.

For the market screen we take ONE put per name (nearest the target delta, best yield); the screen
is cash-secured-puts only. For a single-ticker search we return the top-N contracts nearest the
target delta (one per expiry) on the requested side — puts to *enter* a position, calls to sell
against shares already held — so you can compare expiries at a consistent moneyness. The screen
drops a contract that fails a liquidity measure; the search shows it, marked.

Both sides share every sellability gate; they differ in exactly two places, and only these two:
the sign of the target delta, and the base the yield is measured against (see ``contract_yield``).
"""

from __future__ import annotations

from collections import defaultdict

from wheel_screener.core.earnings import EarningsGuard
from wheel_screener.core.models import ChainSnapshot, OptionContract, OptionType, ScreenCriteria
from wheel_screener.core.ranking import annualized_cc_yield, annualized_csp_yield


def signed_target_delta(target_delta: float, option_type: OptionType) -> float:
    """The target delta with the sign the requested side actually has.

    Callers configure a magnitude ("0.20 delta"), which is how traders talk about moneyness, but
    puts carry negative delta and calls positive. Taking the magnitude and re-signing here means a
    single criteria value drives both sides — and a caller that passes an already-signed put delta
    (the CLI's -0.20 default) still lands on the right target.
    """
    magnitude = abs(target_delta)
    return -magnitude if option_type is OptionType.PUT else magnitude


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
    """Conservative credit per share for selling the contract: the BID (a price you can
    actually be filled at by hitting the bid). The midpoint is reported separately for
    reference but never credited, so the headline yield isn't optimistic vs. a real fill.
    """
    return c.bid


def contract_yield(c: OptionContract) -> float | None:
    """Annualized premium yield on the capital this trade ties up — by side.

    A put is cash-secured: the base is the strike, the cash you set aside. A call is *share*-
    secured: no cash moves, so the base is the underlying's market price — what the 100 pledged
    shares are worth.

    Returns None for a call whose spot we could not establish, rather than guessing. A missing
    denominator must not become a fabricated yield, and it must not delete the row either — the
    contract still lists, with an empty yield cell (see ``is_priceable``).
    """
    prem = credited_premium(c)
    if not prem or prem <= 0 or c.dte <= 0:
        return None
    if c.option_type is OptionType.PUT:
        return annualized_csp_yield(prem, c.strike, c.dte) if c.strike > 0 else None
    spot = c.underlying_price
    return annualized_cc_yield(prem, spot, c.dte) if spot and spot > 0 else None


def put_yield(c: OptionContract) -> float | None:
    """Back-compat alias — the CSP path (screen + rank) still speaks in puts."""
    return contract_yield(c)


def is_priceable(c: OptionContract) -> bool:
    """Whether the contract can be quoted as a sale at all.

    Deliberately independent of ``contract_yield``: a call on a ticker whose spot is unknown has
    no yield but is perfectly sellable, and dropping it would turn a missing price feed into
    "no contracts available".
    """
    prem = credited_premium(c)
    return prem is not None and prem > 0 and c.strike > 0 and c.dte > 0


def liquidity_issues(c: OptionContract, criteria: ScreenCriteria) -> list[str]:
    """What stands between this contract and a fill at its bid, a few words each — empty when it
    passes all four of the liquidity measures (spread, open interest, volume, bid size; see
    ``ScreenCriteria``). The screen drops a contract with any; a single-ticker search shows it
    with them marked."""
    if c.bid is None or c.bid <= 0:
        return ["no bid"]
    issues = []
    if not _tradeable_spread(c, criteria):
        mid = (c.ask + c.bid) / 2 if c.ask else 0
        issues.append(f"{(c.ask - c.bid) / mid:.1%} spread" if mid > 0 else "no ask")
    if (c.open_interest or 0) < criteria.min_open_interest:
        issues.append(f"{c.open_interest or 0} open interest")
    if (c.volume or 0) < criteria.min_volume:
        issues.append("no trades today")
    if (c.bid_size or 0) < criteria.min_bid_size:
        issues.append(f"{c.bid_size or 0} bid")
    return issues


def _eligible_contracts(
    snapshot: ChainSnapshot,
    criteria: ScreenCriteria,
    option_type: OptionType = OptionType.PUT,
    guard: EarningsGuard | None = None,
    *,
    liquid_only: bool = True,
) -> list[OptionContract]:
    """Contracts of ``option_type`` that pass the sellability gates: has a delta, DTE within
    [min,max] (±tolerance), |delta| <= max_abs_delta, a real sellable bid (>0 — the yield floor,
    not a dollar threshold, is what rejects a token credit), a market liquid enough to trade (see
    ``liquidity_issues``; not checked with ``liquid_only=False``), (when a min_iv floor is set) a
    known IV >= it, and — when a ``guard`` is supplied — no earnings report inside the contract's
    life.

    The earnings check belongs HERE, not at the name level: it is the only stage that knows the
    expiration. Filtering by name against the DTE window instead both over-filters (a name
    reporting after the near expiry is still perfectly sellable on it) and under-filters (a
    positive ``dte_tolerance`` admits expiries past the window that was checked)."""
    lo, hi, tol = criteria.min_dte, criteria.max_dte, criteria.dte_tolerance
    return [
        c
        for c in snapshot.contracts
        if c.option_type == option_type
        and c.delta is not None
        and (lo - tol) <= c.dte <= (hi + tol)
        and abs(c.delta) <= criteria.max_abs_delta
        and c.bid is not None
        and c.bid > 0
        and not (liquid_only and liquidity_issues(c, criteria))
        and (
            criteria.min_iv is None
            or (c.implied_volatility is not None and c.implied_volatility >= criteria.min_iv)
        )
        and not (guard is not None and guard.blocks(c.underlying_symbol, c.expiration))
    ]


def _tradeable_spread(c: OptionContract, criteria: ScreenCriteria) -> bool:
    """Is the gap between bid and ask one a seller could work inside?

    Percentage against the mid, because the cost of crossing is what matters and a dollar is a
    different cost on a 20c contract than on a $30 one. An absolutely narrow spread is exempt:
    a penny wide on a cheap far-OTM put reads as 25% and is perfectly tradeable, and rejecting
    those is what made the first version of this cap look like it did nothing useful.
    """
    if c.bid is None or c.ask is None or c.ask <= 0:
        return False
    gap = c.ask - c.bid
    if gap <= criteria.spread_abs_exempt:
        return True
    mid = (c.ask + c.bid) / 2
    return mid > 0 and (gap / mid) <= criteria.max_bid_ask_spread_pct


def _best_per_expiry(
    contracts: list[OptionContract], target_delta: float
) -> list[OptionContract]:
    """The contract nearest ``target_delta`` in each expiry, keeping only priceable ones."""
    best: dict[object, OptionContract] = {}
    for c in contracts:
        cur = best.get(c.expiration)
        if cur is None or abs(c.delta - target_delta) < abs(cur.delta - target_delta):
            best[c.expiration] = c
    return [c for c in best.values() if is_priceable(c)]


def select_put(
    snapshot: ChainSnapshot, criteria: ScreenCriteria, guard: EarningsGuard | None = None
) -> OptionContract | None:
    """Best cash-secured put for this underlying, or None if nothing qualifies.

    By default (dte_tolerance == 0) results stay strictly within [min_dte, max_dte]. A positive
    dte_tolerance also admits expiries within ±tol, preferring in-band ones. Among the chosen
    expiries' per-expiry nearest-to-target-delta puts, pick the highest yield.

    With a ``guard``, earnings-spanning expiries are removed before the yield comparison — which
    matters because they are systematically the richest (pre-earnings IV inflates the premium),
    so a yield ranker left unguarded promotes exactly the contracts we mean to avoid.
    """
    target = signed_target_delta(criteria.target_delta, OptionType.PUT)
    priced = _best_per_expiry(
        _eligible_contracts(snapshot, criteria, OptionType.PUT, guard), target
    )
    if not priced:
        return None
    lo, hi = criteria.min_dte, criteria.max_dte
    in_band = [c for c in priced if lo <= c.dte <= hi]
    pool = in_band if in_band else priced  # prefer in-band; else the nearest expiry within tol
    return max(pool, key=lambda c: contract_yield(c) or 0.0)


# In an expiry, a liquid contract within this much delta of the nearest one is shown in its place:
# about a strike apart, and a tradeable quote beside the target beats a thin one on it.
LIQUID_REACH = 0.05


def select_top_contracts(
    snapshot: ChainSnapshot,
    criteria: ScreenCriteria,
    n: int,
    option_type: OptionType = OptionType.PUT,
    guard: EarningsGuard | None = None,
) -> list[OptionContract]:
    """The N contracts nearest ``target_delta`` (one per expiry) on the requested side — for a
    single-ticker search, returned earliest-expiry-first (term structure).

    Thin contracts are kept, for the caller to mark with ``liquidity_issues``: someone who typed a
    ticker is asking what its options look like, and hiding every thin one reads as "none exist".
    On TER (2026-10-29) the weeklies held under 100 open at every strike near the money, and the
    search said there were no calls at all. In each expiry the contract nearest the target is
    shown, or a liquid one within ``LIQUID_REACH`` of it; expiries whose pick is liquid come
    before thin ones, then the nearest the target.

    Ordering is by delta-proximity, NOT by yield — which is why search can safely FLAG rather than
    exclude earnings-spanning expiries: there is no yield ranker here for inflated pre-earnings
    premium to win. Someone who typed a ticker should see its whole term structure with the risky
    expiries marked, not silently shortened.
    """
    target = signed_target_delta(criteria.target_delta, option_type)

    def off(c: OptionContract) -> float:
        return abs(c.delta - target)

    by_expiry: dict[object, list[OptionContract]] = defaultdict(list)
    for c in _eligible_contracts(snapshot, criteria, option_type, guard, liquid_only=False):
        if is_priceable(c):
            by_expiry[c.expiration].append(c)
    picks = []
    for contracts in by_expiry.values():
        nearest = min(contracts, key=off)
        liquid = [c for c in contracts if not liquidity_issues(c, criteria)]
        best = min(liquid, key=off) if liquid else None
        reach = off(nearest) + LIQUID_REACH + 1e-9  # a strike exactly 0.05 away is within it
        picks.append(best if best is not None and off(best) <= reach else nearest)
    picks.sort(key=lambda c: (bool(liquidity_issues(c, criteria)), off(c)))
    top = picks[: max(n, 0)]
    top.sort(key=lambda c: c.dte)  # display order: earliest expiry first
    return top
