from __future__ import annotations

from datetime import date

from wheel_screener.core.models import (
    ChainSnapshot,
    OptionContract,
    OptionType,
    ScreenCriteria,
    StockProfile,
)


def test_screen_criteria_defaults() -> None:
    c = ScreenCriteria()
    assert c.stock_profile == StockProfile.STALWART
    assert c.target_delta == -0.20
    assert c.min_dte == 30
    assert c.max_dte == 45
    assert c.top_n == 50
    assert c.exchanges == ["nasdaq", "nyse"]


def test_option_contract_spread_pct() -> None:
    oc = OptionContract(
        underlying_symbol="AAA",
        option_symbol="AAA80P",
        option_type=OptionType.PUT,
        expiration=date(2026, 8, 15),
        strike=80.0,
        dte=40,
        bid=1.00,
        ask=1.10,
    )
    assert oc.spread_pct == (1.10 - 1.00) / 1.05


def test_option_contract_spread_pct_none_when_unpriced() -> None:
    oc = OptionContract(
        underlying_symbol="AAA",
        option_symbol="AAA80P",
        option_type=OptionType.PUT,
        expiration=date(2026, 8, 15),
        strike=80.0,
        dte=40,
    )
    assert oc.spread_pct is None


def test_option_contract_serialized_shape() -> None:
    """The JSON contract a UI/API consumes: spread_pct in, raw out, no dead timestamp."""
    oc = OptionContract(
        underlying_symbol="AAA",
        option_symbol="AAA80P",
        option_type=OptionType.PUT,
        expiration=date(2026, 8, 15),
        strike=80.0,
        dte=40,
        bid=1.00,
        ask=1.10,
        raw={"mark": 1.05},
    )
    dumped = oc.model_dump()
    assert dumped["spread_pct"] == (1.10 - 1.00) / 1.05  # computed field is exposed
    assert "raw" not in dumped  # internal vendor blob excluded from the wire
    assert "quote_ts" not in dumped  # dead timestamp field removed
    assert oc.raw == {"mark": 1.05}  # still kept in memory for internal use


def test_chain_snapshot_drops_dead_fetched_at() -> None:
    assert "fetched_at" not in ChainSnapshot(underlying_symbol="AAA").model_dump()
