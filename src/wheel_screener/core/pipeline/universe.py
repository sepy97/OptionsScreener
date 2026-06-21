"""Stage 1 — build the candidate universe from cheap price/market-cap/exchange data."""

from __future__ import annotations

from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider


def build_universe(provider: FundamentalsProvider, criteria: ScreenCriteria) -> list[Underlying]:
    """Return the price/market-cap/exchange-filtered universe (FMP company-screener).

    TODO(M1): delegate to the FMP adapter's ``screen_universe``.
    """
    raise NotImplementedError("Stage 1 (universe) lands in M1")
