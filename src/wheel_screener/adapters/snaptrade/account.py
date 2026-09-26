"""One person's brokerage accounts, through SnapTrade, in this app's terms.

A :class:`~wheel_screener.core.ports.BrokerageAccountProvider`, bound to ONE person's SnapTrade
identity — built per request by ``api.deps.get_portfolio`` and never shared.

Mapping notes, from SnapTrade's API spec (``api.yaml``), since no live account has been read yet:

* **Numbers are strings.** ``units``, ``price``, ``cost_basis``, ``strike_price`` and
  ``multiplier`` arrive as decimal strings. A value that will not parse stays None — an empty cell
  on a page about money beats a confident wrong number.
* **A short position has negative ``units``.** Quantity is kept positive and the direction goes in
  the position's kind, the same convention as the Schwab adapter.
* **Options carry a multiplier.** Anything but the standard 100 shares a contract (minis were 10)
  is listed as "other" rather than as an option: every per-contract figure in this app assumes 100,
  and a mini priced as a standard contract would be wrong by a factor of ten.
* **``cost_basis`` is per share for options, and its sign for a short position is not specified.**
  So the premium collected is taken as its magnitude, and no profit figure is derived from it.
* **Cash-equivalent rows are skipped** — a sweep fund is already inside the cash balance.
* **Opening dates come from activities** (``SELL_TO_OPEN`` / ``BUY_TO_OPEN``), matched to positions
  by contract rather than by string, because two sources may space an OCC symbol differently.
  Best-effort: activities failing costs a date, never the page.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from wheel_screener.adapters.snaptrade.client import SnapTradeClient, SnapTradeUser
from wheel_screener.core.errors import ProviderError
from wheel_screener.core.models import (
    AccountBalances,
    AccountType,
    BrokerageAccount,
    OptionType,
    Position,
    PositionKind,
)
from wheel_screener.core.osi import parse_osi

logger = logging.getLogger(__name__)

STANDARD_MULTIPLIER = 100.0
LOOKBACK_DAYS = 60  # a wheel position lives 20-45 days; older ones keep an unknown open date
# SnapTrade instrument kinds that are shares for this app's purposes: things a covered call can
# be written against, and that read as "shares" in the holdings table.
_SHARE_KINDS = {"stock": "EQUITY", "etf": "COLLECTIVE_INVESTMENT", "adr": "EQUITY",
                "cef": "COLLECTIVE_INVESTMENT"}
_OPENING = {"SELL_TO_OPEN", "BUY_TO_OPEN"}


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(number) if number.is_finite() else None


def _day(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _moment(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _contract_key(symbol: str | None):
    """(underlying, expiry, side, strike) — the same contract however its symbol is spaced."""
    osi = parse_osi(symbol or "")
    return None if osi is None else (osi.underlying, osi.expiration, osi.option_type, osi.strike)


class SnapTradeAccountProvider:
    """Read-only by construction: the client it wraps has no call that places an order."""

    broker = "snaptrade"

    def __init__(self, client: SnapTradeClient, user: SnapTradeUser, today=date.today) -> None:
        self._client = client
        self._user = user
        self._today = today

    def accounts(self) -> list[BrokerageAccount]:
        today = self._today()
        out: list[BrokerageAccount] = []
        for acct in self._client.accounts(self._user):
            account_id = str(acct.get("id") or "")
            if not account_id or str(acct.get("status") or "").lower() in ("closed", "archived"):
                continue
            balances = self._client.balances(self._user, account_id)
            rows, as_of = self._client.positions(self._user, account_id)
            account = self._to_account(acct, balances, rows, today)
            account.as_of = _moment(as_of)
            if any(p.is_option for p in account.positions):
                opened = self._opening_trades(account_id, today)
                for p in account.positions:
                    match = opened.get(_contract_key(p.symbol))
                    if match is not None:
                        p.opened_on, p.opening_price = match
            out.append(account)
        return out

    # --- balances -------------------------------------------------------------------------

    @staticmethod
    def _account_type(raw: str) -> AccountType | None:
        text = raw.lower()
        if "margin" in text:
            return AccountType.MARGIN
        if "cash" in text:
            return AccountType.CASH
        return None  # an IRA, a TFSA… — not guessed

    def _to_account(self, acct: dict, balances: list[dict], rows: list[dict],
                    today: date) -> BrokerageAccount:
        institution = str(acct.get("institution_name") or "").strip()
        number = "".join(ch for ch in str(acct.get("number") or "") if ch.isalnum())
        name = " ".join(x for x in (institution, f"••••{number[-4:]}" if number else "") if x)
        account_type = self._account_type(str(acct.get("raw_type") or ""))

        total = _num(((acct.get("balance") or {}).get("total") or {}).get("amount"))
        usd = next((b for b in balances
                    if str((b.get("currency") or {}).get("code") or "").upper() == "USD"), None)
        row = usd or (balances[0] if len(balances) == 1 else None)  # one currency: use it
        cash = _num(row.get("cash")) if row else None
        power = _num(row.get("buying_power")) if row else None
        if account_type is None and cash is not None and power is not None and power > cash + 1:
            # SnapTrade's `raw_type` is the broker's own wording, and a Schwab margin account's did
            # not say "margin" (seen on a real account). Its spec is explicit that a non-margin
            # account reports buying power EQUAL to cash — so power above cash is borrowing, and
            # borrowing is what makes an account a margin account.
            account_type = AccountType.MARGIN
        # SnapTrade reports buying power as cash for a non-margin account; this app shows buying
        # power only where it means borrowing, so it is left out otherwise.
        buying_power = power if account_type is AccountType.MARGIN else None
        return BrokerageAccount(
            broker=self.broker,
            account_id=str(acct.get("id")),
            display_name=name or "account",
            account_type=account_type,
            positions=self._to_positions(rows, today),
            balances=AccountBalances(
                total_value=total, cash=cash, buying_power=buying_power,
                invested=None if total is None or cash is None else total - cash,
            ),
        )

    # --- positions ------------------------------------------------------------------------

    def _to_positions(self, rows: list[dict], today: date) -> list[Position]:
        out: list[Position] = []
        for row in rows:
            instrument = row.get("instrument") or {}
            units = _num(row.get("units"))
            symbol = str(instrument.get("symbol") or "").strip()
            if not units or not symbol or row.get("cash_equivalent"):
                continue
            kind = str(instrument.get("kind") or "").lower()
            description = str(instrument.get("description") or "").strip() or None
            price = _num(row.get("price"))
            cost = _num(row.get("cost_basis"))
            if kind == "option":
                option = self._option(instrument, units, price, cost, description, today)
                if option is not None:
                    out.append(option)
                    continue
            asset = _SHARE_KINDS.get(kind)
            share = asset is not None and units > 0
            underlying = str(instrument.get("raw_symbol") or symbol).strip()
            out.append(Position(
                symbol=symbol, underlying=underlying if share else symbol,
                kind=PositionKind.SHARES if share else PositionKind.OTHER,
                quantity=abs(units), asset_type=asset or (kind.upper() or None),
                description=description,
                market_value=None if price is None else price * units,
                average_price=None if cost is None else abs(cost),
            ))
        return out

    @staticmethod
    def _option(instrument: dict, units: float, price: float | None, cost: float | None,
                description: str | None, today: date) -> Position | None:
        """A standard, fully described option — or None, and the row is listed as "other"."""
        symbol = str(instrument.get("symbol") or "").strip()
        osi = parse_osi(symbol)
        multiplier = _num(instrument.get("multiplier"))
        if multiplier is not None and multiplier != STANDARD_MULTIPLIER:
            return None
        side = str(instrument.get("option_type") or "").upper()
        option_type = (OptionType.PUT if side == "PUT" else OptionType.CALL if side == "CALL"
                       else osi.option_type if osi else None)
        strike = _num(instrument.get("strike_price")) or (osi.strike if osi else None)
        expiration = _day(instrument.get("expiration_date")) or (osi.expiration if osi else None)
        underlying = str(((instrument.get("underlying") or {}).get("raw_symbol")
                          or (instrument.get("underlying") or {}).get("symbol")
                          or (osi.underlying if osi else "")) or "").strip()
        if option_type is None or strike is None or expiration is None or not underlying:
            return None
        quantity = abs(units)
        short = units < 0
        if short:
            kind = PositionKind.SHORT_PUT if option_type is OptionType.PUT \
                else PositionKind.SHORT_CALL
        else:
            kind = PositionKind.LONG_OPTION
        return Position(
            symbol=symbol, underlying=underlying, kind=kind, quantity=quantity,
            asset_type="OPTION", description=description, option_type=option_type,
            # Signed like a broker's: a short option is a liability, so negative.
            market_value=None if price is None else price * units * STANDARD_MULTIPLIER,
            average_price=None if cost is None else abs(cost),
            strike=strike, expiration=expiration, dte=(expiration - today).days,
            # A short put commits strike x 100 per contract — a CASH-secured view on purpose,
            # whatever the broker's margin requirement says. Same rule as the Schwab adapter.
            collateral=strike * STANDARD_MULTIPLIER * quantity if short else None,
        )

    # --- when each option was opened -----------------------------------------------------

    def _opening_trades(self, account_id: str, today: date) -> dict:
        """``{contract: (opened_on, price per share)}``; the earliest opening fill wins."""
        try:
            rows = self._client.activities(
                self._user, account_id, today - timedelta(days=LOOKBACK_DAYS), today)
        except ProviderError as e:
            logger.info("snaptrade activities unavailable (%s); open dates will be blank", e)
            return {}
        out: dict = {}
        for row in rows:
            if str(row.get("option_type") or "").upper() not in _OPENING:
                continue
            key = _contract_key((row.get("option_symbol") or {}).get("ticker"))
            stamp = _day(row.get("trade_date")) or _day(row.get("settlement_date"))
            price = _num(row.get("price"))
            if key is None or stamp is None or price is None:
                continue
            if key not in out or stamp < out[key][0]:
                out[key] = (stamp, abs(price))
        return out
