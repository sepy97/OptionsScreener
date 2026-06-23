from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from wheel_screener.adapters.local.provider import LocalFundamentalsProvider
from wheel_screener.core.models import ScreenCriteria

_FILES = {
    "profile-bulk_part0.csv": (
        "symbol,price,marketCap,beta,companyName,exchange,sector,industry,"
        "fullTimeEmployees,isEtf,isFund,isAdr,isActivelyTrading\n"
        "GOOD,100,5000000000,1.1,Good Inc,NASDAQ,Technology,Software,5000,false,false,false,true\n"
        "ETFX,50,1000000000,1.0,ETF X,NASDAQ,,Asset Management,0,true,false,false,true\n"
        "FRGN,80,9000000000,1.0,Foreign Co,LSE,Technology,Software,100,false,false,false,true\n"
        "SMALL,5,3000000000,1.0,Small Co,NYSE,Industrials,Manufacturing,50,false,false,false,true\n"
        "NOTEX,25,3000000000,1.0,Acme 6.00% Notes due 2026,NASDAQ,Financial Services,"
        "Asset Management,11,false,false,false,true\n"
        "CEFX,50,3000000000,1.0,Some Closed Fund Limited,NYSE,Financial Services,"
        "Asset Management,0,false,false,false,true\n"
        # IMPP (common) + IMPPP (preferred, same name): dedup keeps the shorter ticker
        "IMPP,30,3000000000,1.0,Imperial Petroleum Inc.,NASDAQ,Energy,Oil & Gas E&P,74,"
        "false,false,false,true\n"
        "IMPPP,26,3000000000,1.0,Imperial Petroleum Inc.,NASDAQ,Energy,Oil & Gas E&P,74,"
        "false,false,false,true\n"
    ),
    "ratios-ttm-bulk.csv": (
        "symbol,priceToEarningsRatioTTM,priceToSalesRatioTTM,priceToBookRatioTTM,"
        "priceToEarningsGrowthRatioTTM,netProfitMarginTTM,currentRatioTTM,quickRatioTTM,"
        "cashRatioTTM,debtToEquityRatioTTM\n"
        "GOOD,10,2,1.5,1,0.15,1.5,1.0,0.5,0.4\n"
    ),
    "key-metrics-ttm-bulk.csv": (
        "symbol,returnOnEquityTTM,returnOnAssetsTTM,returnOnInvestedCapitalTTM,netDebtToEBITDATTM\n"
        "GOOD,0.25,0.12,0.2,0.8\n"
    ),
    "dcf-bulk.csv": "symbol,date,dcf,Stock Price\nGOOD,2026-01-01,120,100\n",
    "income-statement-bulk_2024_annual.csv": (
        "symbol,fiscalYear,eps,ebitda\nGOOD,2024,4.0,900000000\n"
    ),
    "income-statement-bulk_2025_annual.csv": (
        "symbol,fiscalYear,eps,ebitda\nGOOD,2025,5.0,1000000000\n"
    ),
    "balance-sheet-statement-bulk_2025_annual.csv": (
        "symbol,fiscalYear,totalStockholdersEquity\nGOOD,2025,2000000000\n"
    ),
}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    for name, content in _FILES.items():
        (tmp_path / name).write_text(content)
    return tmp_path


def test_screen_universe_filters(store: Path) -> None:
    p = LocalFundamentalsProvider(str(store))
    universe = {u.symbol for u in p.screen_universe(
        ScreenCriteria(min_price=20, max_price=200, min_market_cap=2e9)
    )}
    # kept: GOOD (common) and IMPP (common). Excluded: ETFX (isEtf), FRGN (LSE),
    # SMALL (price<20), NOTEX (baby-bond name), CEFX (closed-end fund),
    # IMPPP (preferred — same name as IMPP, longer ticker -> dedup drops it).
    assert universe == {"GOOD", "IMPP"}
    assert "IMPPP" not in universe and "NOTEX" not in universe and "CEFX" not in universe


def test_fetch_metrics_maps_and_coalesces_latest_year(store: Path) -> None:
    fm = LocalFundamentalsProvider(str(store)).fetch_metrics(["GOOD"])["GOOD"]
    assert fm.pe == 10.0 and fm.ps == 2.0 and fm.pb == 1.5 and fm.peg == 1.0
    assert fm.ros == 0.15 and fm.current_ratio == 1.5 and fm.debt_to_equity == 0.4
    assert fm.roe == 0.25 and fm.roi == 0.2 and fm.net_debt_to_ebitda == 0.8
    assert fm.dcf == 120.0 and fm.price == 100.0
    assert fm.eps == 5.0 and fm.ebitda == 1.0e9  # FY2025 (latest), not FY2024
    assert fm.total_equity == 2.0e9


def test_overlay_overrides_bulk_metrics(store: Path) -> None:
    from wheel_screener.adapters.local.overlay import write_overlay
    from wheel_screener.core.models import FundamentalMetrics

    # bulk says GOOD.pe == 10; a fresh post-earnings refresh writes pe == 7.5
    write_overlay(str(store), {"GOOD": FundamentalMetrics(pe=7.5, roe=0.30, fcf_yield=0.08)})
    fm = LocalFundamentalsProvider(str(store)).fetch_metrics(["GOOD"])["GOOD"]
    assert fm.pe == 7.5 and fm.roe == 0.30 and fm.fcf_yield == 0.08  # overlay wins over bulk


def test_known_symbols(store: Path) -> None:
    assert "GOOD" in LocalFundamentalsProvider(str(store)).known_symbols()


def test_earnings_calendar_empty_without_provider(store: Path) -> None:
    p = LocalFundamentalsProvider(str(store))
    assert p.earnings_calendar(date(2026, 6, 22), date(2026, 8, 6)) == {}


def test_missing_store_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LocalFundamentalsProvider(str(tmp_path)).screen_universe(ScreenCriteria())
