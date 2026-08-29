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
from wheel_screener.core.models import AccountType


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

    def get_accounts(self):
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
