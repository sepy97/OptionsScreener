"""FundamentalsProvider backed by Financial Modeling Prep (https://financialmodelingprep.com/stable/).

The same provider pythonBot uses. Rating thresholds live in ``core.fundamentals``;
this adapter only fetches + maps FMP JSON into the core models.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from wheel_screener.adapters.errors import map_http_error
from wheel_screener.adapters.fmp.client import FmpClient
from wheel_screener.adapters.fmp.mapper import map_earnings, map_metrics, map_universe_row
from wheel_screener.config import FmpSettings
from wheel_screener.core.errors import ProviderDataError
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying

_EARNINGS_ROW_CAP = 4000  # FMP earnings-calendar returns at most this many rows (then clips)


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
            "isEtf": "false",
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

        Returns {} ONLY when the bulk endpoints aren't in the account's subscription
        (verified: lower tiers return HTTP 402/404) so the caller can fall back to a
        capped per-name deep fetch. Any other failure (auth, rate limit, outage) is
        raised as a ProviderError rather than masked as a degraded/empty ranking.
        """
        try:
            ratios = self._bulk("ratios-ttm-bulk")
            key_metrics = self._bulk("key-metrics-ttm-bulk")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (402, 404):
                return {}  # not in subscription -> caller falls back to deep fetch
            raise map_http_error(e) from e
        except httpx.TransportError as e:
            raise map_http_error(e) from e
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
            except httpx.HTTPStatusError as e:
                mapped = map_http_error(e)
                if isinstance(mapped, ProviderDataError):
                    continue  # 4xx for this symbol (e.g. 404) -> skip just this name
                raise mapped from e  # auth/rate/outage is systemic -> surface it
            except httpx.TransportError as e:
                raise map_http_error(e) from e
            out[sym] = map_metrics(ratios, key_metrics, income, balance, dcf)
        return out

    def _earnings_rows(self, start: date, end: date) -> list[dict]:
        """Fetch raw earnings rows, splitting the window when FMP's 4000-row cap is hit
        (a wide window returns only the latest 4000, dropping near-term earnings)."""
        payload = self._client.get(
            "earnings-calendar", {"from": start.isoformat(), "to": end.isoformat()}
        )
        rows = payload if isinstance(payload, list) else []
        if len(rows) >= _EARNINGS_ROW_CAP and end > start:
            mid = start + timedelta(days=(end - start).days // 2)
            return self._earnings_rows(start, mid) + self._earnings_rows(
                mid + timedelta(days=1), end
            )
        return rows

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        return map_earnings(self._earnings_rows(start, end))
