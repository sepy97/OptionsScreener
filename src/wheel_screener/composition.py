"""Composition root — the one place concrete adapters are wired to the service.

Swapping a provider is a one-line change here; tests inject fakes instead.
"""

from __future__ import annotations

from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.config import Settings
from wheel_screener.core.service import ScreenerService


def build_service(settings: Settings | None = None) -> ScreenerService:
    settings = settings or Settings()
    return ScreenerService(
        fundamentals=FmpFundamentalsProvider(settings.fmp),
        chains=SchwabChainProvider(settings.schwab),
    )
