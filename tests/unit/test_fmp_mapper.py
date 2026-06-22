from __future__ import annotations

from datetime import date

from wheel_screener.adapters.fmp.mapper import map_earnings, map_metrics, map_universe_row


def test_map_universe_row() -> None:
    u = map_universe_row(
        {"symbol": "AAA", "companyName": "Alpha", "price": "50.0", "marketCap": 1.0e10,
         "sector": "Technology"}
    )
    assert u.symbol == "AAA"
    assert u.name == "Alpha"
    assert u.price == 50.0  # string coerced
    assert u.sector == "Technology"


def test_map_metrics_picks_fields_and_sign_inputs() -> None:
    fm = map_metrics(
        ratios={
            "peRatioTTM": 12.0, "priceToSalesRatioTTM": 2.0, "priceToBookRatioTTM": 2.5,
            "returnOnEquityTTM": 0.2, "netProfitMarginTTM": 0.12, "currentRatioTTM": 1.5,
            "debtEquityRatioTTM": 0.6,
        },
        key_metrics={"roicTTM": 0.18, "netDebtToEBITDATTM": 1.2},
        income={"eps": 4.5, "ebitda": 1.0e9},
        balance={"totalStockholdersEquity": 5.0e9},
        dcf={"dcf": 60.0, "Stock Price": 50.0},
    )
    assert fm.pe == 12.0 and fm.ps == 2.0 and fm.pb == 2.5
    assert fm.roi == 0.18 and fm.net_debt_to_ebitda == 1.2
    assert fm.eps == 4.5 and fm.total_equity == 5.0e9 and fm.ebitda == 1.0e9
    assert fm.dcf == 60.0 and fm.price == 50.0


def test_map_metrics_alternate_field_spellings() -> None:
    # defensive _pick should accept legacy/alternate keys
    fm = map_metrics(
        ratios={"priceEarningsRatioTTM": 9.0, "pbRatioTTM": 1.1},
        key_metrics={"returnOnInvestedCapitalTTM": 0.25, "netDebtToEbitdaTTM": 0.8},
        income={"epsdiluted": 3.0},
        balance={"totalEquity": 2.0e9},
        dcf={"dcf": 40.0, "stockPrice": 30.0},
    )
    assert fm.pe == 9.0 and fm.pb == 1.1 and fm.roi == 0.25
    assert fm.net_debt_to_ebitda == 0.8 and fm.eps == 3.0
    assert fm.total_equity == 2.0e9 and fm.price == 30.0


def test_map_metrics_real_stable_field_names() -> None:
    # field names verified against live FMP /stable/ responses (June 2026)
    fm = map_metrics(
        ratios={
            "priceToEarningsRatioTTM": 11.0, "priceToSalesRatioTTM": 2.0,
            "priceToBookRatioTTM": 3.0, "priceToEarningsGrowthRatioTTM": 1.5,
            "netProfitMarginTTM": 0.15, "currentRatioTTM": 1.4, "quickRatioTTM": 1.0,
            "cashRatioTTM": 0.4, "debtToEquityRatioTTM": 0.7,
        },
        # ROE/ROA/ROIC live in key-metrics-ttm, not ratios-ttm (verified live)
        key_metrics={
            "returnOnEquityTTM": 0.3, "returnOnAssetsTTM": 0.12,
            "returnOnInvestedCapitalTTM": 0.2, "netDebtToEBITDATTM": 1.1,
        },
        income={"eps": 6.0, "ebitda": 2.0e9},
        balance={"totalStockholdersEquity": 8.0e9},
        dcf={"dcf": 70.0, "Stock Price": 60.0},
    )
    assert fm.pe == 11.0 and fm.peg == 1.5 and fm.debt_to_equity == 0.7
    assert fm.roe == 0.3 and fm.roa == 0.12 and fm.roi == 0.2
    assert fm.net_debt_to_ebitda == 1.1
    assert fm.eps == 6.0 and fm.total_equity == 8.0e9 and fm.price == 60.0


def test_map_earnings_keeps_earliest_and_skips_bad() -> None:
    e = map_earnings([
        {"symbol": "AAA", "date": "2026-08-01"},
        {"symbol": "AAA", "date": "2026-07-01"},  # earlier -> wins
        {"symbol": "BBB", "date": "not-a-date"},  # skipped
        {"symbol": "CCC"},  # no date -> skipped
    ])
    assert e == {"AAA": date(2026, 7, 1)}
