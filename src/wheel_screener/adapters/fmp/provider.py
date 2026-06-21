"""FundamentalsProvider backed by Financial Modeling Prep (https://financialmodelingprep.com/stable/).

This is the same provider pythonBot uses; the rating thresholds live in
``core.fundamentals``. The adapter's job is just to fetch + map FMP JSON into
``Underlying`` / ``FundamentalMetrics``.
"""

from __future__ import annotations

from datetime import date

from wheel_screener.config import FmpSettings
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying


class FmpFundamentalsProvider:
    """Universe + fundamentals + earnings via FMP.

    Endpoint plan (M1):
      - screen_universe   -> /company-screener (price, market cap, exchange)
      - fetch_metrics     -> /ratios-ttm(-bulk), /key-metrics-ttm(-bulk),
                             /financial-growth, /financial-scores, DCF
      - earnings_calendar -> /earnings-calendar?from=&to=
    """

    def __init__(self, settings: FmpSettings) -> None:
        self._settings = settings

    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]:
        raise NotImplementedError("FMP universe screen lands in M1")

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        raise NotImplementedError("FMP fundamentals fetch lands in M1")

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        raise NotImplementedError("FMP earnings calendar lands in M1")
