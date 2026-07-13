from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.adapters.alpaca.mapper import build_chain, parse_occ_symbol
from wheel_screener.core.models import OptionType, ScreenCriteria
from wheel_screener.core.pipeline.select_strike import select_put


def test_parse_occ_symbol() -> None:
    assert parse_occ_symbol("AAPL260815P00190000") == (
        "AAPL", date(2026, 8, 15), OptionType.PUT, 190.0,
    )
    assert parse_occ_symbol("AAPL260815C00190500")[2] == OptionType.CALL
    # multi-char roots and fractional strikes
    root, exp, _t, strike = parse_occ_symbol("BRKB261218P00250500")
    assert root == "BRKB" and exp == date(2026, 12, 18) and strike == 250.5
    # unparseable -> None (never raises)
    assert parse_occ_symbol("") is None
    assert parse_occ_symbol("GARBAGE") is None
    assert parse_occ_symbol("AAPL260815X00190000") is None  # bad option type


def test_build_chain_merges_oi_and_maps_fields() -> None:
    today = date(2026, 6, 25)
    snapshots = {
        "AAA260725P00090000": {
            "latestQuote": {"bp": 1.40, "ap": 1.50, "bs": 10, "as": 12},
            "latestTrade": {"p": 1.45},
            "greeks": {"delta": -0.20, "gamma": 0.03, "theta": -0.04, "vega": 0.10, "rho": -0.01},
            "impliedVolatility": 0.345,
        },
    }
    snap = build_chain("AAA", snapshots, {"AAA260725P00090000": 800}, today)
    assert snap.underlying_symbol == "AAA" and len(snap.contracts) == 1
    c = snap.contracts[0]
    assert c.option_type == OptionType.PUT and c.strike == 90.0
    assert c.expiration == date(2026, 7, 25) and c.dte == 30
    assert c.bid == 1.40 and c.ask == 1.50 and c.mid == 1.45  # true midpoint
    assert c.delta == -0.20 and c.open_interest == 800
    assert c.implied_volatility == 0.345  # Alpaca IV is already a fraction (no /100)
    assert c.raw["rho"] == -0.01


def test_build_chain_oi_missing_is_none() -> None:
    today = date(2026, 6, 25)
    snapshots = {"AAA260725P00090000": {"latestQuote": {"bp": 1.0, "ap": 1.1}}}
    snap = build_chain("AAA", snapshots, {}, today)  # no OI provided for this contract
    assert snap.contracts[0].open_interest is None


def test_alpaca_chain_feeds_select_put() -> None:
    today = date(2026, 6, 25)
    snapshots = {
        "AAA260725P00090000": {  # ~-0.20Δ, liquid -> the pick
            "latestQuote": {"bp": 1.40, "ap": 1.50}, "greeks": {"delta": -0.20},
            "impliedVolatility": 0.34,
        },
        "AAA260725P00085000": {  # -0.10Δ, lower yield -> not chosen
            "latestQuote": {"bp": 0.80, "ap": 0.90}, "greeks": {"delta": -0.10},
            "impliedVolatility": 0.30,
        },
    }
    oi = {"AAA260725P00090000": 800, "AAA260725P00085000": 300}
    put = select_put(build_chain("AAA", snapshots, oi, today), ScreenCriteria())
    assert put is not None and put.strike == 90.0 and put.open_interest == 800


def test_select_put_honors_strict_dte_window() -> None:
    # only an out-of-window expiry: 40 DTE, while the default window is [21, 35] (issue #26)
    today = date(2026, 6, 25)
    far = f"AAA{today + timedelta(days=40):%y%m%d}P00090000"
    snaps = {far: {"latestQuote": {"bp": 1.4, "ap": 1.5}, "greeks": {"delta": -0.20},
                   "impliedVolatility": 0.34}}
    chain = build_chain("AAA", snaps, {far: 800}, today)
    assert select_put(chain, ScreenCriteria()) is None  # strict by default: 40 DTE is out of band
    picked = select_put(chain, ScreenCriteria(dte_tolerance=10))  # opt-in tolerance admits it
    assert picked is not None and picked.dte == 40
