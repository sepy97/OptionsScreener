from __future__ import annotations

from datetime import date

from wheel_screener.adapters.schwab.mapper import parse_chain
from wheel_screener.core.models import OptionType, ScreenCriteria
from wheel_screener.core.pipeline.select_strike import select_put

# Schwab chains shape: putExpDateMap["YYYY-MM-DD:DTE"][strike] = [contract, ...]
_PAYLOAD = {
    "symbol": "AAA",
    "underlyingPrice": 100.0,
    "putExpDateMap": {
        "2026-07-25:33": {
            "90.0": [{
                "putCall": "PUT", "symbol": "AAA  260725P00090000", "strikePrice": 90.0,
                "daysToExpiration": 33, "bid": 1.40, "ask": 1.50, "last": 1.45, "mark": 1.49,
                "bidSize": 10, "askSize": 12, "totalVolume": 200, "openInterest": 800,
                "delta": -0.20, "gamma": 0.03, "theta": -0.04, "vega": 0.10,
                "volatility": 34.5, "rho": -0.01, "timeValue": 1.45,
            }],
            "85.0": [{
                "putCall": "PUT", "symbol": "AAA  260725P00085000", "strikePrice": 85.0,
                "daysToExpiration": 33, "bid": 0.80, "ask": 0.90, "mark": 0.85,
                "totalVolume": 50, "openInterest": 300,
                "delta": -0.10, "gamma": 0.02, "theta": -999.0, "vega": -999.0,  # sentinels
                "volatility": -999.0,
            }],
        }
    },
}


def test_parse_chain_maps_fields_and_sentinels():
    snap = parse_chain(_PAYLOAD)
    assert snap.underlying_symbol == "AAA" and snap.underlying_price == 100.0
    assert len(snap.contracts) == 2
    by_strike = {c.strike: c for c in snap.contracts}

    p90 = by_strike[90.0]
    assert p90.option_type == OptionType.PUT
    assert p90.expiration == date(2026, 7, 25) and p90.dte == 33
    assert p90.delta == -0.20 and p90.open_interest == 800 and p90.bid == 1.40
    assert abs(p90.implied_volatility - 0.345) < 1e-9  # 34.5% -> 0.345 fraction
    # mid is the true midpoint (bid+ask)/2 == 1.45, NOT Schwab's "mark" (1.49, kept in raw)
    assert p90.mid == 1.45 and p90.raw["mark"] == 1.49

    p85 = by_strike[85.0]
    assert p85.theta is None and p85.vega is None and p85.implied_volatility is None  # -999 -> None


def test_parsed_chain_feeds_select_put():
    snap = parse_chain(_PAYLOAD)
    put = select_put(snap, ScreenCriteria())  # min_oi 100, dte 30-45, ~-0.20 delta
    assert put is not None and put.strike == 90.0  # the -0.20Δ put (85 is -0.10 and lower yield)
