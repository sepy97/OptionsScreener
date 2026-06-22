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


def test_map_earnings_keeps_earliest_and_skips_bad() -> None:
    e = map_earnings([
        {"symbol": "AAA", "date": "2026-08-01"},
        {"symbol": "AAA", "date": "2026-07-01"},  # earlier -> wins
        {"symbol": "BBB", "date": "not-a-date"},  # skipped
        {"symbol": "CCC"},  # no date -> skipped
    ])
    assert e == {"AAA": date(2026, 7, 1)}
