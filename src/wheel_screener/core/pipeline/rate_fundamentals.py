"""Stage 2 — rate the universe on fundamentals and keep the best names.

Strategy (scales to a nightly screen within FMP rate limits):
  1. cheap bulk pre-rank — score everyone on TTM bulk metrics, keep the top N
  2. gate + rank        — hard never-trade gates, then a within-sector percentile composite
  3. earnings blackout  — drop names reporting inside [today, today + max_dte]

``select_top`` is pure (given Underlyings with ``.metrics`` populated) and fully
unit-testable without a provider; ``rate_and_rank`` adds the provider fetches.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from wheel_screener.core.fundamentals import gate_reasons, rank_by_fundamentals
from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider


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
    """Gate -> earnings blackout -> cross-sectional rank -> (sector cap) -> top N.

    Pure and deterministic given Underlyings with ``.metrics`` populated.
    """
    survivors = [u for u in names if not gate_reasons(u.metrics, criteria)]
    survivors = apply_earnings_blackout(survivors, earnings, today, criteria.max_dte)
    ranked = rank_by_fundamentals(survivors, criteria.factor_weights, criteria.stock_profile)
    if criteria.min_fundamental_score is not None:
        floor = criteria.min_fundamental_score
        ranked = [u for u in ranked if (u.fundamental_score or 0.0) >= floor]
    if criteria.max_per_sector is not None:
        ranked = _cap_per_sector(ranked, criteria.max_per_sector)
    return ranked[: criteria.top_n]


def rate_and_rank(
    provider: FundamentalsProvider,
    universe: list[Underlying],
    criteria: ScreenCriteria,
    today: date,
) -> list[Underlying]:
    """Fetch fundamentals + earnings for the universe, then ``select_top``.

    TODO(M1): add the cheap TTM-bulk pre-rank to trim the universe before the deep
    ``fetch_metrics`` call, to stay inside FMP rate limits.
    """
    metrics = provider.fetch_metrics([u.symbol for u in universe])
    for u in universe:
        u.metrics = metrics.get(u.symbol)
    earnings = provider.earnings_calendar(today, today + timedelta(days=criteria.max_dte))
    return select_top(universe, criteria, earnings, today)
