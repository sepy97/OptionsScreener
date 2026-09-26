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
    assert c.min_dte == 14  # wide enough that a monthly always lands inside it
    assert c.max_dte == 45
    assert c.top_n is None  # no cap: pull a chain for every fundamental survivor
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


# --- an expired contract still on the books --------------------------------------------------

def _expired_put(price, dte=-1, strike=270.0):
    from wheel_screener.core.models import OptionType, Position, PositionKind

    return Position(symbol="LRCX  260925P00270000", underlying="LRCX",
                    kind=PositionKind.SHORT_PUT, quantity=1, option_type=OptionType.PUT,
                    strike=strike, dte=dte, collateral=strike * 100, underlying_price=price)


def _cash_account(*positions):
    from wheel_screener.core.models import AccountBalances, BrokerageAccount

    return BrokerageAccount(broker="x", account_id="a", display_name="a",
                            balances=AccountBalances(cash=204_535.04), positions=list(positions))


def test_a_put_that_expired_worthless_no_longer_commits_cash() -> None:
    """Seen on a real account: LRCX $270, expired the day before with the stock at $315, still
    listed because the broker had not processed the weekend's expirations — and its $27,000 made
    capacity read $5,235 instead of $32,235."""
    lrcx = _expired_put(315.19)
    assert lrcx.expired and lrcx.expired_worthless
    account = _cash_account(lrcx, _expired_put(200.0, dte=20, strike=165.0))
    assert account.committed_collateral == 16_500.0  # only the live put
    assert account.capacity == 204_535.04 - 16_500.0


def test_a_put_that_expired_in_the_money_stays_committed() -> None:
    """Assignment is coming, and the cash will go to the shares."""
    assert _cash_account(_expired_put(250.0)).committed_collateral == 27_000.0


def test_an_expired_put_with_no_price_stays_committed() -> None:
    """Without a price, assignment cannot be ruled out."""
    put = _expired_put(None)
    assert put.expired and not put.expired_worthless
    assert _cash_account(put).committed_collateral == 27_000.0


def test_expiry_day_itself_is_not_expired() -> None:
    assert not _expired_put(315.0, dte=0).expired
