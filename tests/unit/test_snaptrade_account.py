"""SnapTrade rows into this app's accounts and positions.

Fixtures follow SnapTrade's API spec exactly — decimal STRINGS, negative units for a short, the
option instrument's own strike/expiry/multiplier — since no live account has been read yet. When
one is, the first real payload should replace these.
"""

from __future__ import annotations

from datetime import date

import pytest

from wheel_screener.adapters.snaptrade.account import SnapTradeAccountProvider
from wheel_screener.adapters.snaptrade.client import SnapTradeUser
from wheel_screener.core.errors import ProviderUnavailableError
from wheel_screener.core.models import AccountType, OptionType, PositionKind

TODAY = date(2026, 9, 26)
USER = SnapTradeUser("u", "s")


def _option(symbol, side, strike, expiry, units, price="1.20", cost="2.50", multiplier="100",
            underlying="MRVL"):
    return {
        "instrument": {
            "kind": "option", "id": "11111111-1111-1111-1111-111111111111", "symbol": symbol,
            "option_type": side, "strike_price": strike, "expiration_date": expiry,
            "multiplier": multiplier, "description": f"{underlying} {side}",
            "underlying": {"kind": "stock", "symbol": underlying, "raw_symbol": underlying},
        },
        "units": units, "price": price, "cost_basis": cost, "currency": "USD",
    }


def _stock(symbol, units, price="100.00", cost="90.00", kind="stock", **extra):
    return {"instrument": {"kind": kind, "symbol": symbol, "raw_symbol": symbol,
                           "description": f"{symbol} Inc"},
            "units": units, "price": price, "cost_basis": cost, "currency": "USD", **extra}


ACCOUNT = {
    "id": "acct-1", "institution_name": "Fidelity", "number": "Z12-341234",
    "raw_type": "Individual - Margin", "status": "open",
    "balance": {"total": {"amount": 100_000.0, "currency": "USD"}},
}
BALANCES = [
    {"currency": {"code": "CAD"}, "cash": 5.0, "buying_power": 5.0},
    {"currency": {"code": "USD"}, "cash": 40_000.0, "buying_power": 80_000.0},
]


class _FakeClient:
    def __init__(self, positions, accounts=(ACCOUNT,), balances=BALANCES, activities=(),
                 activities_error=None):
        self._positions, self._accounts = positions, list(accounts)
        self._balances, self._activities = balances, list(activities)
        self._activities_error = activities_error
        self.users = []

    def accounts(self, user):
        self.users.append(user)
        return self._accounts

    def balances(self, user, account_id):
        return self._balances

    def positions(self, user, account_id):
        return self._positions

    def activities(self, user, account_id, start, end):
        if self._activities_error:
            raise self._activities_error
        return self._activities


def _one(rows, **kw):
    (account,) = SnapTradeAccountProvider(_FakeClient(rows, **kw), USER,
                                          today=lambda: TODAY).accounts()
    return account


# --- options --------------------------------------------------------------------------------

def test_a_short_put_is_a_short_put_with_its_cash_collateral() -> None:
    (p,) = _one([_option("MRVL  261030P00210000", "PUT", "210", "2026-10-30", "-2")]).positions
    assert p.kind is PositionKind.SHORT_PUT and p.option_type is OptionType.PUT
    assert p.quantity == 2 and p.strike == 210.0 and p.expiration == date(2026, 10, 30)
    assert p.underlying == "MRVL" and p.dte == 34
    assert p.collateral == 210 * 100 * 2
    assert p.market_value == pytest.approx(-240.0), "a short option is a liability: negative"
    assert p.mark == pytest.approx(1.20), "the per-share price must survive the round trip"
    assert p.average_price == 2.50


def test_calls_and_longs_are_told_apart() -> None:
    rows = [_option("AAPL  261016C00250000", "CALL", "250", "2026-10-16", "-1", underlying="AAPL"),
            _option("AAPL  261016P00200000", "PUT", "200", "2026-10-16", "3", underlying="AAPL")]
    short_call, long_put = _one(rows).positions
    assert short_call.kind is PositionKind.SHORT_CALL
    assert long_put.kind is PositionKind.LONG_OPTION and long_put.collateral is None


def test_a_mini_option_is_listed_but_not_priced_as_a_standard_contract() -> None:
    """Every per-contract figure here assumes 100 shares; a mini priced that way is 10x wrong."""
    (p,) = _one([_option("AAPL7 261016P00200000", "PUT", "200", "2026-10-16", "-1",
                         multiplier="10", underlying="AAPL")]).positions
    assert p.kind is PositionKind.OTHER and p.collateral is None and not p.is_option


def test_the_contract_is_read_from_its_symbol_when_the_fields_are_missing() -> None:
    row = _option("MRVL  261030P00210000", "PUT", None, None, "-1")
    row["instrument"].pop("option_type")
    (p,) = _one([row]).positions
    assert (p.strike, p.expiration, p.option_type) == (210.0, date(2026, 10, 30), OptionType.PUT)


def test_a_number_that_will_not_parse_is_blank_not_zero() -> None:
    row = _option("MRVL  261030P00210000", "PUT", "210", "2026-10-30", "-1", price="n/a",
                  cost="")
    (p,) = _one([row]).positions
    assert p.market_value is None and p.average_price is None and p.mark is None
    assert _one([_stock("AAPL", "lots")]).positions == []  # no quantity, no row


# --- everything else -------------------------------------------------------------------------

def test_shares_and_funds_are_holdings_a_call_can_be_written_against() -> None:
    stock, etf = _one([_stock("AAPL", "300"), _stock("SPY", "100", kind="etf")]).positions
    assert stock.kind is PositionKind.SHARES and stock.covered_call_lots == 3
    assert etf.kind is PositionKind.SHARES and etf.covered_call_lots == 1
    assert stock.market_value == pytest.approx(30_000.0)


def test_a_sweep_fund_is_not_listed_twice() -> None:
    """It is already inside the cash balance."""
    assert _one([_stock("SPAXX", "5000", kind="mutualfund", cash_equivalent=True)]).positions == []


def test_a_bond_is_a_holding_but_not_shares() -> None:
    (p,) = _one([_stock("912828XX", "10", kind="bond")]).positions
    assert p.kind is PositionKind.OTHER and p.covered_call_lots is None


# --- the account -----------------------------------------------------------------------------

def test_the_account_reads_in_dollars_and_names_its_brokerage() -> None:
    a = _one([])
    assert a.display_name == "Fidelity ••••1234"
    assert a.account_type is AccountType.MARGIN
    assert (a.balances.total_value, a.balances.cash, a.balances.buying_power) == (
        100_000.0, 40_000.0, 80_000.0)


def test_buying_power_is_shown_only_where_it_means_borrowing() -> None:
    cash_account = {**ACCOUNT, "raw_type": "Cash"}
    a = _one([], accounts=(cash_account,))
    assert a.account_type is AccountType.CASH and a.balances.buying_power is None


def test_an_ira_is_not_guessed_to_be_cash_or_margin() -> None:
    assert _one([], accounts=({**ACCOUNT, "raw_type": "Roth IRA"},)).account_type is None


def test_closed_accounts_are_left_out() -> None:
    provider = SnapTradeAccountProvider(
        _FakeClient([], accounts=({**ACCOUNT, "status": "closed"},)), USER, today=lambda: TODAY)
    assert provider.accounts() == []


def test_every_call_is_made_as_the_person_the_provider_was_built_for() -> None:
    client = _FakeClient([])
    SnapTradeAccountProvider(client, SnapTradeUser("alice", "a-secret"),
                             today=lambda: TODAY).accounts()
    assert client.users == [SnapTradeUser("alice", "a-secret")]


# --- when a position was opened ----------------------------------------------------------------

def _activity(ticker, option_type, day, price):
    return {"option_symbol": {"ticker": ticker}, "option_type": option_type,
            "trade_date": f"{day}T14:31:00Z", "price": price, "type": "SELL"}


def test_the_earliest_opening_trade_dates_the_position_however_the_symbol_is_spaced() -> None:
    rows = [_option("MRVL  261030P00210000", "PUT", "210", "2026-10-30", "-2")]
    activities = [
        _activity("MRVL261030P00210000", "SELL_TO_OPEN", "2026-09-22", 3.10),  # unpadded
        _activity("MRVL  261030P00210000", "SELL_TO_OPEN", "2026-09-15", 2.90),  # first lot
        _activity("MRVL  261030P00210000", "BUY_TO_CLOSE", "2026-09-10", 9.99),  # not an open
    ]
    (p,) = _one(rows, activities=activities).positions
    assert p.opened_on == date(2026, 9, 15) and p.opening_price == 2.90


def test_a_failed_history_call_costs_a_date_not_the_page() -> None:
    rows = [_option("MRVL  261030P00210000", "PUT", "210", "2026-10-30", "-2")]
    (p,) = _one(rows, activities_error=ProviderUnavailableError("down")).positions
    assert p.kind is PositionKind.SHORT_PUT and p.opened_on is None
