"""Stage 2 — rate the universe on fundamentals and keep the best names.

Strategy (scales to a nightly screen within FMP rate limits):
  1. cheap bulk pre-rank — score everyone on TTM bulk metrics, keep the top N
  2. gate + rank        — hard never-trade gates, then a within-sector percentile composite
  3. earnings           — stamp each name's next report; drop only names with no clean expiry

The earnings *decision* is per contract and lives in ``select_strike``; this stage only skips
chain pulls that provably cannot yield a clean expiry. ``select_top`` is pure (given Underlyings
with ``.metrics`` populated) and fully unit-testable without a provider; ``rate_and_rank`` adds
the provider fetches.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from wheel_screener.core.earnings import EarningsGuard
from wheel_screener.core.fundamentals import gate_reasons, rank_by_fundamentals
from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider

logger = logging.getLogger(__name__)


def apply_earnings_blackout(
    names: list[Underlying], guard: EarningsGuard, today: date, min_dte: int
) -> list[Underlying]:
    """Stamp every name with its next earnings date and drop only those with NO clean expiry.

    This is a chain-pull optimization, not the blackout itself. A name is dropped here only when
    a report lands on/before the earliest expiry we would even consider (``today + min_dte``) —
    i.e. when every expiry in the window is provably dirty, so the chain pull would return
    nothing usable. The real per-contract decision happens in ``select_strike`` once expirations
    are known; vetoing names by the whole DTE window instead would throw away the common good
    case where a name reports after the near expiry but before the far one.
    """
    earliest_expiry = today + timedelta(days=min_dte)
    keep: list[Underlying] = []
    for u in names:
        u.next_earnings = guard.date_for(u.symbol)  # carried through to the results table
        if guard.blocks_every_expiry(u.symbol, earliest_expiry):
            continue
        keep.append(u)
    return keep


def _cap_per_sector(names: list[Underlying], cap: int) -> list[Underlying]:
    """Keep at most ``cap`` names per sector, preserving order (bounds assignment clustering)."""
    counts: dict[str, int] = defaultdict(int)
    out: list[Underlying] = []
    for u in names:
        s = u.sector or "UNKNOWN"
        if counts[s] < cap:
            out.append(u)
            counts[s] += 1
    return out


# Reasons that mean "we could not judge", as opposed to "this name fails". A name is never
# dropped at the cheap pre-rank stage for missing data — it goes to the deep fetch instead.
_COVERAGE_REASONS = frozenset({"insufficient_data", "no_metrics"})


def _hard_gate_reasons(metrics, criteria) -> list[str]:
    """Gate failures that the cheap bulk metrics can decide on their own."""
    return [r for r in gate_reasons(metrics, criteria) if r not in _COVERAGE_REASONS]


def select_top(
    names: list[Underlying],
    criteria: ScreenCriteria,
    guard: EarningsGuard,
    today: date,
) -> list[Underlying]:
    """Gate -> cross-sectional rank -> earnings pre-filter -> (sector cap) -> top N.

    The rank comes BEFORE the earnings step on purpose: the fundamental score is a cross-sectional
    percentile and must not depend on earnings timing, so a market screen and a single-ticker
    search show the same score for a name. Pure and deterministic given Underlyings with
    ``.metrics`` populated.
    """
    gated = [u for u in names if not gate_reasons(u.metrics, criteria)]
    ranked = rank_by_fundamentals(gated, criteria.factor_weights, criteria.stock_profile)
    survivors = apply_earnings_blackout(ranked, guard, today, criteria.min_dte)
    blacked_out = len(gated) - len(survivors)
    if criteria.min_fundamental_score is not None:
        # floors the absolute strength rating (fundamental_score) — "only names this financially
        # strong", independent of the peer field; the percentile drives top_n ordering above.
        floor = criteria.min_fundamental_score
        survivors = [u for u in survivors if (u.fundamental_score or 0.0) >= floor]
    if criteria.max_per_sector is not None:
        survivors = _cap_per_sector(survivors, criteria.max_per_sector)
    kept = survivors[: criteria.top_n]
    logger.info(
        "fundamentals: %d/%d passed gates · %d report before the earliest expiry (≤%dd) · "
        "top %d kept · calendar covers %d symbols",
        len(gated), len(names), blacked_out, criteria.min_dte, len(kept), guard.loaded,
    )
    return kept


def rate_and_rank(
    provider: FundamentalsProvider,
    universe: list[Underlying],
    criteria: ScreenCriteria,
    today: date,
    guard: EarningsGuard,
) -> list[Underlying]:
    """Two-phase: cheap bulk pre-rank over the whole universe, then a deep fetch for the
    pre-rank survivors only (keeps the expensive per-name calls bounded).

    When the bulk endpoints aren't in the FMP subscription (lower tiers), fall back to a
    market-cap-capped deep fetch of ``universe_limit`` names.
    """
    bulk = provider.bulk_metrics([u.symbol for u in universe])
    if bulk:
        for u in universe:
            u.metrics = bulk.get(u.symbol)
        # Gate BEFORE the cap. Gating is free (it reads metrics we already hold), so spending
        # cap slots on names that are about to be gated out is pure waste. Only HARD failures
        # count here: the pre-rank metrics are the cheap bulk ones, so a name with thin coverage
        # must survive to the deep fetch and be judged there, not dropped for missing data.
        viable = [
            u for u in universe
            if u.metrics is not None and not _hard_gate_reasons(u.metrics, criteria)
        ]
        prelim = rank_by_fundamentals(
            viable, criteria.factor_weights, criteria.stock_profile
        )
        cap = max(criteria.prerank_keep, criteria.top_n)
        if criteria.top_n > criteria.prerank_keep:
            # Without this the cap silently overrode top_n and the caller's "check N names" was
            # a no-op above ~prerank_keep — the control looked live and did nothing.
            logger.info(
                "prerank_keep (%d) is below top_n (%d); raising the deep-fetch cap to %d so "
                "top_n means what it says",
                criteria.prerank_keep, criteria.top_n, cap,
            )
        keep = prelim[:cap]
        logger.info(
            "prerank: %d/%d names kept for the deep fetch (%d gated out first)",
            len(keep), len(universe), len(universe) - len(viable),
        )
    else:
        keep = sorted(universe, key=lambda u: u.market_cap or 0.0, reverse=True)[
            : criteria.universe_limit
        ]

    # Deep fetch (sign inputs + DCF) for survivors, then gate + final rank.
    deep = provider.fetch_metrics([u.symbol for u in keep])
    for u in keep:
        if u.symbol in deep:
            u.metrics = deep[u.symbol]
    return select_top(keep, criteria, guard, today)
