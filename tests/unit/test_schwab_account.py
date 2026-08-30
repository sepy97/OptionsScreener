"""Balance mapping for Schwab accounts.

The fixtures mirror the field names read from a live margin account, not the documentation —
including the two things that surprised us: `cashBalance` is not necessarily the cash, and
`longMarketValue` is not "invested".
"""

from __future__ import annotations

import pytest

from wheel_screener.adapters.schwab.account import SchwabAccountProvider
from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import AuthExpiredError, ProviderUnavailableError
from wheel_screener.core.models import AccountType, Position


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code
        self.request = None
        self.text = str(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, numbers, accounts, status=200):
        self._numbers, self._accounts, self._status = numbers, accounts, status

    def get_account_numbers(self):
        return _Response(self._numbers, self._status)

    def get_accounts(self, *, fields=None):
        self.fields = fields  # positions are opt-in; assert we actually ask for them
        return _Response(self._accounts, self._status)


def _provider(numbers, accounts, status=200) -> SchwabAccountProvider:
    return SchwabAccountProvider(
        SchwabSettings(client_id="k", client_secret="s"),
        client_factory=lambda _s: _Client(numbers, accounts, status),
    )


_NUMBERS = [{"accountNumber": "22881337", "hashValue": "HASH-ABC"}]


def _margin(**balances):
    current = {
        "liquidationValue": 100_000.0, "cashBalance": 40_000.0, "moneyMarketFund": 0.0,
        "longMarketValue": 0.0, "buyingPower": 80_000.0, "equity": 100_000.0,
    }
    current.update(balances)
    return [{"securitiesAccount": {
        "type": "MARGIN", "accountNumber": "22881337",
        "initialBalances": {"liquidationValue": 1.0},   # start-of-day: must NOT be read
        "currentBalances": current,
        "projectedBalances": {"buyingPower": 2.0},
    }}]


def test_maps_a_margin_account() -> None:
    acct = _provider(_NUMBERS, _margin()).accounts()[0]
    assert acct.broker == "schwab" and acct.account_type is AccountType.MARGIN
    assert acct.balances.total_value == 100_000.0
    assert acct.balances.buying_power == 80_000.0


def test_account_number_is_masked_and_the_hash_is_the_handle() -> None:
    """The number is for humans and stays masked; the hash is what the API wants."""
    acct = _provider(_NUMBERS, _margin()).accounts()[0]
    assert acct.display_name == "••••1337"
    assert "22881337" not in acct.display_name
    assert acct.account_id == "HASH-ABC"


def test_invested_is_derived_not_summed_from_buckets() -> None:
    """On the live account longMarketValue was ZERO while bonds and short options were not.
    Summing buckets under-reports; total minus cash does not."""
    acct = _provider(_NUMBERS, _margin(longMarketValue=0.0, bondValue=55_000.0)).accounts()[0]
    assert acct.balances.invested == 60_000.0  # 100k total - 40k cash, despite longMarketValue=0


def test_cash_sums_the_swept_buckets() -> None:
    """Brokers sweep idle cash into a money-market fund, so cashBalance alone understates it."""
    acct = _provider(
        _NUMBERS, _margin(cashBalance=10_000.0, moneyMarketFund=30_000.0)
    ).accounts()[0]
    assert acct.balances.cash == 40_000.0
    assert acct.balances.invested == 60_000.0


def test_over_counted_cash_is_reported_not_hidden(caplog) -> None:
    """If a swept bucket is double-counted, invested goes negative. That must be loud: a silently
    clamped zero would read as 'you hold nothing', which is a different and wrong claim."""
    acct = _provider(
        _NUMBERS, _margin(cashBalance=100_000.0, moneyMarketFund=50_000.0)
    ).accounts()[0]
    assert acct.balances.invested == -50_000.0
    assert any("double-counting" in r.message for r in caplog.records)


def test_cash_account_uses_its_own_buying_power_field() -> None:
    accounts = [{"securitiesAccount": {
        "type": "CASH", "accountNumber": "99991234",
        "currentBalances": {
            "liquidationValue": 5_000.0, "cashBalance": 5_000.0,
            "cashAvailableForTrading": 4_500.0,   # cash accounts have no buyingPower
        },
    }}]
    acct = _provider([{"accountNumber": "99991234", "hashValue": "H2"}], accounts).accounts()[0]
    assert acct.account_type is AccountType.CASH
    assert acct.balances.buying_power == 4_500.0
    assert acct.balances.invested == 0.0


def test_missing_fields_read_as_unknown_not_zero() -> None:
    accounts = [{"securitiesAccount": {
        "type": "MARGIN", "accountNumber": "1234", "currentBalances": {},
    }}]
    b = _provider(_NUMBERS, accounts).accounts()[0].balances
    assert b.total_value is None and b.cash is None and b.invested is None
    assert b.buying_power is None, "absent is not the same as zero"


def test_rejected_credentials_say_how_to_fix_them() -> None:
    with pytest.raises(AuthExpiredError) as e:
        _provider(_NUMBERS, _margin(), status=401).accounts()
    assert "Schwab" in str(e.value) and "auth-login" in str(e.value)


def test_a_vendor_explosion_becomes_a_provider_error() -> None:
    def boom(_s):
        raise RuntimeError("authlib exploded")

    provider = SchwabAccountProvider(SchwabSettings(client_id="k", client_secret="s"),
                                     client_factory=boom)
    with pytest.raises(ProviderUnavailableError):
        provider.accounts()


def test_service_distinguishes_no_broker_from_no_holdings() -> None:
    from wheel_screener.core.service import ScreenerService

    svc = ScreenerService(fundamentals=object(), chains=object())
    with pytest.raises(ProviderUnavailableError, match="no brokerage account is linked"):
        svc.brokerage_accounts()


# ── positions ──────────────────────────────────────────────────────────────────────────────

def _pos(symbol, asset, *, long=0.0, short=0.0, **kw) -> dict:
    row = {"instrument": {"symbol": symbol, "assetType": asset, **kw.pop("instrument", {})},
           "longQuantity": long, "shortQuantity": short}
    row.update(kw)
    return row


def _acct_with(*positions, cash=100_000.0) -> list:
    return [{"securitiesAccount": {
        "accountNumber": "12345678", "type": "MARGIN",
        "currentBalances": {"liquidationValue": 150_000.0, "cashBalance": cash},
        "positions": list(positions)}}]


def test_positions_are_requested_not_assumed() -> None:
    """Balances come back by default; positions only if asked for. A silent omission here would
    render an empty, entirely believable "no open positions"."""
    from schwab.client import Client

    client = _Client([], _acct_with())
    SchwabAccountProvider(
        SchwabSettings(client_id="k", client_secret="s"),
        client_factory=lambda _s: client,
    ).accounts()
    assert client.fields is Client.Account.Fields.POSITIONS


def test_a_short_put_is_recognised_and_its_collateral_derived() -> None:
    from wheel_screener.core.models import PositionKind

    acct = _provider([], _acct_with(_pos(
        "AAPL  260918P00190000", "OPTION", short=2.0, marketValue=-420.0,
        averagePrice=3.10, instrument={"underlyingSymbol": "AAPL"},
    ))).accounts()[0]
    p = acct.positions[0]
    assert p.kind is PositionKind.SHORT_PUT and p.underlying == "AAPL"
    assert p.strike == 190.0 and p.quantity == 2.0
    assert p.collateral == 38_000.0, "strike x 100 x contracts — a CASH-secured view"
    assert acct.committed_collateral == 38_000.0
    assert acct.capacity == 100_000.0 - 38_000.0


def test_capacity_uses_cash_not_margin_buying_power() -> None:
    """A cash-secured put is secured by cash. Showing margin buying power here would invite
    selling puts the account cannot actually cover."""
    acct = _provider([], _acct_with(
        _pos("AAPL  260918P00190000", "OPTION", short=1.0), cash=25_000.0)).accounts()[0]
    assert acct.capacity == 25_000.0 - 19_000.0


def test_short_calls_and_shares_are_told_apart() -> None:
    from wheel_screener.core.models import PositionKind

    acct = _provider([], _acct_with(
        _pos("AAPL  260918C00250000", "OPTION", short=1.0,
             instrument={"underlyingSymbol": "AAPL"}),
        _pos("AAPL", "EQUITY", long=300.0, marketValue=60_000.0, averagePrice=180.0),
    )).accounts()[0]
    kinds = {p.kind for p in acct.positions}
    assert kinds == {PositionKind.SHORT_CALL, PositionKind.SHARES}
    # a short CALL commits shares, not cash — it must not eat the collateral pool
    assert acct.committed_collateral == 0.0
    shares = next(p for p in acct.positions if p.kind is PositionKind.SHARES)
    assert shares.quantity == 300.0 and shares.average_price == 180.0


def test_a_long_option_is_not_mistaken_for_an_obligation() -> None:
    from wheel_screener.core.models import PositionKind

    acct = _provider([], _acct_with(_pos(
        "AAPL  260918P00190000", "OPTION", long=1.0))).accounts()[0]
    assert acct.positions[0].kind is PositionKind.LONG_OPTION
    assert acct.positions[0].collateral is None and acct.committed_collateral == 0.0


def test_a_swept_cash_fund_is_not_listed_as_a_holding() -> None:
    """The sweep is already inside the cash balance. Listing it again double-counts the account
    on screen and makes 'shares held' nonsense."""
    acct = _provider([], _acct_with(
        _pos("MMDA1", "CASH_EQUIVALENT", long=50_000.0),
        _pos("SWVXX", "MONEY_MARKET_FUND", long=10_000.0),
        _pos("AAPL", "EQUITY", long=100.0),
    )).accounts()[0]
    assert [p.symbol for p in acct.positions] == ["AAPL"]


def test_closed_and_unreadable_rows_are_skipped_not_rendered() -> None:
    acct = _provider([], _acct_with(
        _pos("AAPL", "EQUITY", long=0.0, short=0.0),   # closed, still returned
        {"instrument": {"symbol": "", "assetType": "EQUITY"}, "longQuantity": 5},
        "not a dict",
        _pos("MSFT", "EQUITY", long=10.0),
    )).accounts()[0]
    assert [p.symbol for p in acct.positions] == ["MSFT"]


def test_an_option_row_whose_symbol_will_not_parse_is_still_shown() -> None:
    """Better a row with unknown strike than a silently dropped obligation."""
    from wheel_screener.core.models import PositionKind

    acct = _provider([], _acct_with(
        _pos("WEIRD-SYMBOL", "OPTION", short=1.0, marketValue=-100.0))).accounts()[0]
    p = acct.positions[0]
    assert p.kind is PositionKind.OTHER and p.strike is None and p.market_value == -100.0


def test_missing_position_fields_read_as_unknown_not_zero() -> None:
    acct = _provider([], _acct_with(_pos("AAPL", "EQUITY", long=100.0))).accounts()[0]
    p = acct.positions[0]
    assert p.market_value is None and p.average_price is None and p.unrealized_pl is None


def test_assignment_watch_needs_a_price_and_says_nothing_without_one() -> None:
    from wheel_screener.core.models import PositionKind

    acct = _provider([], _acct_with(_pos(
        "AAPL  260918P00190000", "OPTION", short=1.0))).accounts()[0]
    p = acct.positions[0]
    assert p.in_the_money is None, "no quote yet — must not claim safe or assigned"
    p.underlying_price = 185.0
    assert p.in_the_money is True
    p.underlying_price = 195.0
    assert p.in_the_money is False
    shares = Position(symbol="AAPL", underlying="AAPL", kind=PositionKind.SHARES, quantity=1,
                      underlying_price=10.0)
    assert shares.in_the_money is None, "only meaningful for a short put"
