from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.fmp.client import FmpClient
from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.config import FmpSettings
from wheel_screener.core.errors import AuthExpiredError, ProviderDataError
from wheel_screener.core.models import ScreenCriteria

BASE = "https://financialmodelingprep.com/stable"


def _settings() -> FmpSettings:
    # cache_enabled=False so tests use a plain injected httpx.Client (respx-intercepted)
    return FmpSettings(api_key=SecretStr("test-key"), cache_enabled=False)


def _provider() -> FmpFundamentalsProvider:
    return FmpFundamentalsProvider(_settings())


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
def test_bulk_metrics_maps_partial_universe() -> None:
    respx.get(f"{BASE}/ratios-ttm-bulk").mock(return_value=httpx.Response(200, json=[
        {"symbol": "AAA", "peRatioTTM": 10.0, "returnOnEquityTTM": 0.2, "currentRatioTTM": 1.4},
        {"symbol": "BBB", "peRatioTTM": 30.0, "returnOnEquityTTM": 0.05, "currentRatioTTM": 0.9},
    ]))
    respx.get(f"{BASE}/key-metrics-ttm-bulk").mock(return_value=httpx.Response(200, json=[
        {"symbol": "AAA", "roicTTM": 0.22, "netDebtToEBITDATTM": 0.5},
        {"symbol": "BBB", "roicTTM": 0.04, "netDebtToEBITDATTM": 3.0},
    ]))
    metrics = _provider().bulk_metrics(["AAA", "BBB", "CCC"])
    assert set(metrics) == {"AAA", "BBB"}  # CCC absent from bulk
    assert metrics["AAA"].pe == 10.0 and metrics["AAA"].roi == 0.22
    assert metrics["AAA"].eps is None  # sign inputs come from the deep fetch only


@respx.mock
def test_bulk_metrics_empty_on_subscription_code() -> None:
    # 402 (not in subscription) -> {} so the caller falls back to the deep fetch
    respx.get(f"{BASE}/ratios-ttm-bulk").mock(return_value=httpx.Response(402))
    assert _provider().bulk_metrics(["AAA"]) == {}


@respx.mock
def test_bulk_metrics_raises_on_auth_failure() -> None:
    # 401 must NOT be masked as an empty ranking
    respx.get(f"{BASE}/ratios-ttm-bulk").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthExpiredError):
        _provider().bulk_metrics(["AAA"])


@respx.mock
def test_fetch_metrics_raises_on_auth_failure() -> None:
    respx.get(f"{BASE}/ratios-ttm").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthExpiredError):
        _provider().fetch_metrics(["AAA"])


@respx.mock
def test_in_run_cache_dedupes_identical_gets() -> None:
    route = respx.get(f"{BASE}/ratios-ttm").mock(return_value=httpx.Response(200, json=[{"x": 1}]))
    client = FmpClient(_settings(), client=httpx.Client())
    first = client.get("ratios-ttm", {"symbol": "AAPL"})
    second = client.get("ratios-ttm", {"symbol": "AAPL"})
    assert first == second
    assert route.call_count == 1  # second call served from the in-run cache


@respx.mock
def test_disk_cache_serves_across_clients(tmp_path) -> None:
    route = respx.get(f"{BASE}/ratios-ttm").mock(return_value=httpx.Response(200, json=[{"x": 1}]))
    c1 = FmpClient(_settings(), client=httpx.Client(), disk=DiskCache(str(tmp_path), 3600))
    c1.get("ratios-ttm", {"symbol": "AAPL"})
    # a fresh client (new run) with an empty in-run cache but the same disk dir hits disk
    c2 = FmpClient(_settings(), client=httpx.Client(), disk=DiskCache(str(tmp_path), 3600))
    c2.get("ratios-ttm", {"symbol": "AAPL"})
    assert route.call_count == 1  # second client served from the on-disk cache


def _dense_rows(frm: date, to: date, extra: list[dict] | None = None) -> list[dict]:
    """A row on every day in [frm, to] — a realistic, fully-covering response."""
    rows: list[dict] = []
    day = frm
    while day <= to:
        rows.append({"symbol": f"D{day.isoformat()}", "date": day.isoformat()})
        day += timedelta(days=1)
    return rows + (extra or [])


@respx.mock
def test_earnings_calendar_parses() -> None:
    def handler(request):
        frm = date.fromisoformat(request.url.params["from"])
        to = date.fromisoformat(request.url.params["to"])
        extra = [
            {"symbol": "AAA", "date": "2026-08-01"},
            {"symbol": "AAA", "date": "2026-07-01"},
            {"symbol": "BBB", "date": "bad"},
        ]
        keep = [r for r in extra if frm.isoformat() <= r["date"] <= to.isoformat()]
        return httpx.Response(200, json=_dense_rows(frm, to, keep))

    respx.get(f"{BASE}/earnings-calendar").mock(side_effect=handler)
    earnings = _provider().earnings_calendar(date(2026, 6, 21), date(2026, 8, 5))
    assert earnings["AAA"] == date(2026, 7, 1)  # earliest upcoming wins
    assert "BBB" not in earnings  # unparseable date dropped


@respx.mock
def test_earnings_calendar_splits_window_when_capped() -> None:
    """A slice dense enough to hit the row cap is halved until it comes back complete."""
    def handler(request):
        frm = date.fromisoformat(request.url.params["from"])
        to = date.fromisoformat(request.url.params["to"])
        if (to - frm).days > 3:  # pretend anything wider than 4 days overflows the cap
            return httpx.Response(200, json=[
                {"symbol": f"S{i}", "date": to.isoformat()} for i in range(4000)
            ])
        return httpx.Response(200, json=_dense_rows(frm, to, [
            {"symbol": "AAA", "date": "2026-07-01"}
        ] if frm <= date(2026, 7, 1) <= to else []))

    respx.get(f"{BASE}/earnings-calendar").mock(side_effect=handler)
    cal = _provider().earnings_calendar(date(2026, 6, 22), date(2026, 8, 6))
    assert cal.get("AAA") == date(2026, 7, 1)  # found only by splitting below the cap
    assert "S0" not in cal  # capped rows are discarded, never treated as the answer


@respx.mock
def test_earnings_calendar_rejects_a_front_clipped_response() -> None:
    """Issue #113: the endpoint answers a wide request with a right-anchored slice, dropping the
    NEAR-TERM rows the blackout needs — and says nothing. A silently short calendar must raise,
    because downstream every missing symbol reads as 'no earnings scheduled'."""
    start = date(2026, 7, 25)

    def handler(request):
        frm = date.fromisoformat(request.url.params["from"])
        to = date.fromisoformat(request.url.params["to"])
        clipped_from = max(frm, start + timedelta(days=30))  # first 30 days silently missing
        return httpx.Response(200, json=_dense_rows(clipped_from, to) if clipped_from <= to else [])

    respx.get(f"{BASE}/earnings-calendar").mock(side_effect=handler)
    with pytest.raises(ProviderDataError, match="truncated"):
        _provider().earnings_calendar(start, start + timedelta(days=45))


@respx.mock
def test_earnings_calendar_never_requests_a_window_past_the_upstream_limit() -> None:
    """Ranges beyond the documented 3-month max get clamped upstream (silently), so we must
    never issue one — the 120-day refresh is exactly how the near term went missing."""
    seen: list[int] = []

    def handler(request):
        frm = date.fromisoformat(request.url.params["from"])
        to = date.fromisoformat(request.url.params["to"])
        seen.append((to - frm).days)
        return httpx.Response(200, json=_dense_rows(frm, to))

    respx.get(f"{BASE}/earnings-calendar").mock(side_effect=handler)
    start = date(2026, 7, 25)
    _provider().earnings_calendar(start, start + timedelta(days=120))
    assert max(seen) <= 7  # every call is a small slice, nowhere near the 90-day clamp


@respx.mock
def test_next_earnings_uses_the_per_symbol_endpoint() -> None:
    respx.get(f"{BASE}/earnings").mock(return_value=httpx.Response(200, json=[
        {"symbol": "RDDT", "date": "2026-07-30", "epsActual": None},
        {"symbol": "RDDT", "date": "2026-04-30", "epsActual": 1.01},  # already reported
        {"symbol": "RDDT", "date": "2026-10-29", "epsActual": None},
    ]))
    assert _provider().next_earnings("RDDT", date(2026, 7, 25)) == date(2026, 7, 30)
    assert _provider().next_earnings("RDDT", date(2026, 8, 1)) == date(2026, 10, 29)


@respx.mock
def test_earnings_calendar_is_never_served_from_cache() -> None:
    """It must be current on every request: a cached calendar ages into a silent blackout hole."""
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.params["from"])
        frm = date.fromisoformat(request.url.params["from"])
        to = date.fromisoformat(request.url.params["to"])
        return httpx.Response(200, json=_dense_rows(frm, to))

    respx.get(f"{BASE}/earnings-calendar").mock(side_effect=handler)
    provider = _provider()  # one provider = one client = one in-memory cache
    provider.earnings_calendar(date(2026, 7, 25), date(2026, 8, 5))
    first = len(calls)
    provider.earnings_calendar(date(2026, 7, 25), date(2026, 8, 5))
    assert len(calls) == first * 2, "second fetch was served from cache"
