from __future__ import annotations

from datetime import date

import httpx
import respx
from pydantic import SecretStr

from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.config import FmpSettings
from wheel_screener.core.models import ScreenCriteria

BASE = "https://financialmodelingprep.com/stable"


def _provider() -> FmpFundamentalsProvider:
    return FmpFundamentalsProvider(FmpSettings(api_key=SecretStr("test-key")))


@respx.mock
def test_screen_universe_maps_rows() -> None:
    respx.get(f"{BASE}/company-screener").mock(
        return_value=httpx.Response(200, json=[
            {"symbol": "AAA", "price": 50.0, "marketCap": 1.0e10, "sector": "Technology"},
            {"symbol": "BBB", "price": 80.0, "marketCap": 5.0e9, "sector": "Energy"},
            {"companyName": "NoSymbol"},  # dropped (no symbol)
        ])
    )
    universe = _provider().screen_universe(ScreenCriteria())
    assert [u.symbol for u in universe] == ["AAA", "BBB"]
    assert universe[0].sector == "Technology"


@respx.mock
def test_screen_universe_handles_error_payload() -> None:
    respx.get(f"{BASE}/company-screener").mock(
        return_value=httpx.Response(200, json={"Error Message": "Invalid API KEY"})
    )
    assert _provider().screen_universe(ScreenCriteria()) == []


@respx.mock
def test_fetch_metrics_maps_all_endpoints() -> None:
    respx.get(f"{BASE}/ratios-ttm").mock(return_value=httpx.Response(200, json=[{
        "peRatioTTM": 12.0, "priceToSalesRatioTTM": 2.0, "returnOnEquityTTM": 0.2,
        "netProfitMarginTTM": 0.12, "currentRatioTTM": 1.5, "debtEquityRatioTTM": 0.6,
    }]))
    respx.get(f"{BASE}/key-metrics-ttm").mock(
        return_value=httpx.Response(200, json=[{"roicTTM": 0.18, "netDebtToEBITDATTM": 1.2}])
    )
    respx.get(f"{BASE}/income-statement").mock(
        return_value=httpx.Response(200, json=[{"eps": 4.5, "ebitda": 1.0e9}])
    )
    respx.get(f"{BASE}/balance-sheet-statement").mock(
        return_value=httpx.Response(200, json=[{"totalStockholdersEquity": 5.0e9}])
    )
    respx.get(f"{BASE}/discounted-cash-flow").mock(
        return_value=httpx.Response(200, json=[{"dcf": 60.0, "Stock Price": 50.0}])
    )
    metrics = _provider().fetch_metrics(["AAA"])
    fm = metrics["AAA"]
    assert fm.pe == 12.0 and fm.roi == 0.18 and fm.net_debt_to_ebitda == 1.2
    assert fm.eps == 4.5 and fm.total_equity == 5.0e9 and fm.ebitda == 1.0e9


@respx.mock
def test_fetch_metrics_skips_unfetchable_symbol() -> None:
    respx.get(f"{BASE}/ratios-ttm").mock(return_value=httpx.Response(404))
    assert _provider().fetch_metrics(["AAA"]) == {}


@respx.mock
def test_earnings_calendar_parses() -> None:
    respx.get(f"{BASE}/earnings-calendar").mock(return_value=httpx.Response(200, json=[
        {"symbol": "AAA", "date": "2026-08-01"},
        {"symbol": "AAA", "date": "2026-07-01"},
        {"symbol": "BBB", "date": "bad"},
    ]))
    earnings = _provider().earnings_calendar(date(2026, 6, 21), date(2026, 8, 5))
    assert earnings == {"AAA": date(2026, 7, 1)}
