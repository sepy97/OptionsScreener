"""Stage 1 — build the candidate universe from cheap price/market-cap/exchange data."""

from __future__ import annotations

from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider


def build_universe(provider: FundamentalsProvider, criteria: ScreenCriteria) -> list[Underlying]:
    """Return the price/market-cap/exchange-filtered universe (FMP company-screener).

    For now the universe is truncated to ``criteria.universe_limit`` before the deep
    per-name fetch. TODO(M1+): replace the naive cap with a cheap TTM-bulk pre-rank so
    the *best* names (not the first N) survive into the deep fetch.
    """
    universe = provider.screen_universe(criteria)
    if criteria.universe_limit and len(universe) > criteria.universe_limit:
        universe = universe[: criteria.universe_limit]
    return universe
