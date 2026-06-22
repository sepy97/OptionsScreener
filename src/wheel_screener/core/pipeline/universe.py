"""Stage 1 — build the candidate universe from cheap price/market-cap/exchange data."""

from __future__ import annotations

from wheel_screener.core.models import ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider


def build_universe(provider: FundamentalsProvider, criteria: ScreenCriteria) -> list[Underlying]:
    """Return the full price/market-cap/exchange-filtered universe (FMP company-screener).

    No truncation here — stage 2 either bulk-pre-ranks the whole universe or (when bulk
    is unavailable) caps the deep fetch by market cap.
    """
    return provider.screen_universe(criteria)
