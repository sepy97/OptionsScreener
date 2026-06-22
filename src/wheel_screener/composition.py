"""Composition root — the one place concrete adapters are wired to the service.

Swapping a provider is a one-line change here; tests inject fakes instead.
"""

from __future__ import annotations

from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.adapters.local.provider import LocalFundamentalsProvider
from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.config import Settings
from wheel_screener.core.ports import FundamentalsProvider
from wheel_screener.core.service import ScreenerService


def _build_fundamentals(settings: Settings) -> FundamentalsProvider:
    if settings.fundamentals_source == "local":
        # earnings isn't in the bulk store; delegate it to live FMP if a key is configured
        earnings = (
            FmpFundamentalsProvider(settings.fmp)
            if settings.fmp.api_key.get_secret_value()
            else None
        )
        return LocalFundamentalsProvider(settings.data_dir, earnings_provider=earnings)
    return FmpFundamentalsProvider(settings.fmp)


def build_service(settings: Settings | None = None) -> ScreenerService:
    settings = settings or Settings()
    return ScreenerService(
        fundamentals=_build_fundamentals(settings),
        chains=SchwabChainProvider(settings.schwab),
    )
