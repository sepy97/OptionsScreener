"""FundamentalsProvider backed by Financial Modeling Prep (https://financialmodelingprep.com/stable/).

The same provider pythonBot uses. Rating thresholds live in ``core.fundamentals``;
this adapter only fetches + maps FMP JSON into the core models.
"""

from __future__ import annotations

from datetime import date

import httpx

from wheel_screener.adapters.fmp.client import FmpClient
from wheel_screener.adapters.fmp.mapper import map_earnings, map_metrics, map_universe_row
from wheel_screener.config import FmpSettings
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying


def _first(payload: object) -> dict:
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


class FmpFundamentalsProvider:
    def __init__(self, settings: FmpSettings, client: FmpClient | None = None) -> None:
        self._settings = settings
        self._client = client or FmpClient(settings)

    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]:
        params = {
            "priceMoreThan": criteria.min_price,
            "priceLowerThan": criteria.max_price,
            "marketCapMoreThan": int(criteria.min_market_cap),
            "exchange": ",".join(criteria.exchanges),
            "isFund": "false",
            "isActivelyTrading": "true",
            "limit": 3000,
        }
        rows = self._client.get("company-screener", params)
        if not isinstance(rows, list):
            return []
        return [map_universe_row(r) for r in rows if isinstance(r, dict) and r.get("symbol")]

    def _bulk(self, path: str) -> dict[str, dict]:
        payload = self._client.get(path, {})
        rows = payload if isinstance(payload, list) else []
        return {r["symbol"]: r for r in rows if isinstance(r, dict) and r.get("symbol")}

    def bulk_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        """Cheap pre-rank metrics for the whole universe via the *-ttm-bulk endpoints
        (no sign inputs / DCF — those come from the deep ``fetch_metrics``).

        NOTE: the exact bulk response shape/tier (JSON vs CSV, `part` pagination) is
        unverified against live FMP; this assumes a JSON array keyed by `symbol`.
        """
        ratios = self._bulk("ratios-ttm-bulk")
        key_metrics = self._bulk("key-metrics-ttm-bulk")
        out: dict[str, FundamentalMetrics] = {}
        for sym in symbols:
            if sym in ratios or sym in key_metrics:
                out[sym] = map_metrics(ratios.get(sym, {}), key_metrics.get(sym, {}), {}, {}, {})
        return out

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        """Per-symbol deep fetch (incl. EPS / equity / EBITDA sign inputs + DCF)."""
        out: dict[str, FundamentalMetrics] = {}
        for sym in symbols:
            try:
                ratios = _first(self._client.get("ratios-ttm", {"symbol": sym}))
                key_metrics = _first(self._client.get("key-metrics-ttm", {"symbol": sym}))
                income = _first(self._client.get("income-statement", {"symbol": sym, "limit": 1}))
                balance = _first(
                    self._client.get("balance-sheet-statement", {"symbol": sym, "limit": 1})
                )
                dcf = _first(self._client.get("discounted-cash-flow", {"symbol": sym}))
            except httpx.HTTPError:
                continue  # skip a name we couldn't fetch; it just won't be ranked
            out[sym] = map_metrics(ratios, key_metrics, income, balance, dcf)
        return out

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        rows = self._client.get(
            "earnings-calendar", {"from": start.isoformat(), "to": end.isoformat()}
        )
        return map_earnings(rows if isinstance(rows, list) else [])
