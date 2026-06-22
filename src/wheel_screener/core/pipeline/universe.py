"""Stage 1 — build the candidate universe from cheap price/market-cap/exchange data."""

from __future__ import annotations

from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider


def build_universe(provider: FundamentalsProvider, criteria: ScreenCriteria) -> list[Underlying]:
    """Return the price/market-cap/exchange-filtered universe (FMP company-screener).

    The whole universe is cheaply pre-ranked downstream (stage 2 bulk pre-rank), so this
    only applies an optional hard size cap (``criteria.universe_limit``, default None).
    """
    universe = provider.screen_universe(criteria)
    if criteria.universe_limit and len(universe) > criteria.universe_limit:
        universe = universe[: criteria.universe_limit]
    return universe
