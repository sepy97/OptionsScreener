"""Stage 2 — rate the universe on fundamentals and keep the best names.

Strategy (scales to a nightly screen within FMP rate limits):
  1. cheap bulk pre-rank — score everyone on TTM bulk metrics, keep the top N
  2. gate + rank        — hard never-trade gates, then a within-sector percentile composite
  3. earnings blackout  — drop names reporting inside [today, today + max_dte]

``select_top`` is pure (given Underlyings with ``.metrics`` populated) and fully
unit-testable without a provider; ``rate_and_rank`` adds the provider fetches.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from wheel_screener.core.fundamentals import gate_reasons, rank_by_fundamentals
from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider

logger = logging.getLogger(__name__)


def apply_earnings_blackout(
    names: list[Underlying], earnings: dict[str, date], today: date, max_dte: int
) -> list[Underlying]:
    """Drop names with an earnings date inside the DTE window (pure helper)."""
    window_end = today + timedelta(days=max_dte)
    keep: list[Underlying] = []
    for u in names:
        d = earnings.get(u.symbol)
        if d is not None and today <= d <= window_end:
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


def select_top(
    names: list[Underlying],
    criteria: ScreenCriteria,
    earnings: dict[str, date],
    today: date,
) -> list[Underlying]:
    """Gate -> cross-sectional rank -> earnings blackout -> (sector cap) -> top N.

    The rank comes BEFORE the blackout on purpose: the fundamental score is a cross-sectional
    percentile and must not depend on earnings timing. The blackout is only a display filter (a
    limiting criterion), so a market screen and a single-ticker search show the same score for a
    name. Pure and deterministic given Underlyings with ``.metrics`` populated.
    """
    gated = [u for u in names if not gate_reasons(u.metrics, criteria)]
    ranked = rank_by_fundamentals(gated, criteria.factor_weights, criteria.stock_profile)
    survivors = apply_earnings_blackout(ranked, earnings, today, criteria.max_dte)
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
        "fundamentals: %d/%d passed gates · %d blacked out (earnings ≤%dd) · top %d kept",
        len(gated), len(names), blacked_out, criteria.max_dte, len(kept),
    )
    return kept


def rate_and_rank(
    provider: FundamentalsProvider,
    universe: list[Underlying],
    criteria: ScreenCriteria,
    today: date,
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
        prelim = rank_by_fundamentals(
            [u for u in universe if u.metrics is not None],
            criteria.factor_weights,
            criteria.stock_profile,
        )
        keep = prelim[: criteria.prerank_keep]
    else:
        keep = sorted(universe, key=lambda u: u.market_cap or 0.0, reverse=True)[
            : criteria.universe_limit
        ]

    # Deep fetch (sign inputs + DCF) for survivors, then gate + final rank.
    deep = provider.fetch_metrics([u.symbol for u in keep])
    for u in keep:
        if u.symbol in deep:
            u.metrics = deep[u.symbol]
    earnings = provider.earnings_calendar(today, today + timedelta(days=criteria.max_dte))
    return select_top(keep, criteria, earnings, today)
