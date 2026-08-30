"""Read-only account balances from Schwab.

One provider, two calls: ``get_account_numbers()`` maps an account number to the opaque hash the
rest of the API wants, and ``get_accounts()`` returns balances. Positions ride along on that same
call via the opt-in ``fields`` parameter, requested now that the credential and balance mapping
are proven against a live account.

Field names and their meanings were read from a live margin account rather than inferred; the
mapping notes below record what that showed, including two things the documentation does not.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

from wheel_screener.adapters.errors import SCHWAB, map_http_error
from wheel_screener.adapters.schwab.auth import load_client
from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import ProviderDataError, ProviderError, ProviderUnavailableError
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

# Cash-like buckets. Brokers sweep idle cash into a money-market fund, so `cashBalance` alone can
# understate it. Summed rather than picked, and reconciled by the `invested` sanity check below.
_CASH_FIELDS = ("cashBalance", "moneyMarketFund", "savings")


def _num(block: dict, *names: str) -> float | None:
    """First numeric field present under any of ``names``. Defensive because the payload's shape
    differs between cash and margin accounts, and a missing field must read as unknown, not zero."""
    for name in names:
        value = block.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _cash(block: dict) -> float | None:
    present = [
        block[f] for f in _CASH_FIELDS
        if isinstance(block.get(f), (int, float)) and not isinstance(block.get(f), bool)
    ]
    return float(sum(present)) if present else None


class SchwabAccountProvider:
    """A :class:`~wheel_screener.core.ports.BrokerageAccountProvider` over Schwab.

    Read-only: only GET endpoints are ever called. The token Schwab issues is trading-capable, so
    that is an invariant of this class rather than a matter of which methods happen to exist.
    """

    broker = "schwab"

    def __init__(self, settings: SchwabSettings, client_factory=load_client) -> None:
        self._settings = settings
        self._client_factory = client_factory  # injected so tests need no token

    def _client(self):
        return self._client_factory(self._settings)

    @staticmethod
    def _json(response):
        """Response body, or the mapped provider error. schwab-py hands back httpx responses."""
        status = getattr(response, "status_code", None)
        if status is not None and status >= 400:
            request = getattr(response, "request", None) or httpx.Request("GET", "https://schwab")
            raise map_http_error(
                httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response),
                SCHWAB,
            )
        try:
            return response.json()
        except Exception as e:  # noqa: BLE001 - a non-JSON body is a provider problem
            raise ProviderDataError(f"{SCHWAB} returned an unreadable body: {e}") from e

    def accounts(self) -> list[BrokerageAccount]:
        try:
            client = self._client()
            from schwab.client import Client as _SchwabClient

            numbers = self._json(client.get_account_numbers())
            # schwab-py enforces enums, so this must be the enum member rather than "positions"
            payload = self._json(
                client.get_accounts(fields=_SchwabClient.Account.Fields.POSITIONS)
            )
        except ProviderError:
            raise  # already typed (auth/rate/outage) — never mask it
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, SCHWAB) from e
        except Exception as e:  # noqa: BLE001 - any vendor failure is a provider problem
            raise ProviderUnavailableError(f"{SCHWAB} account fetch failed: {e}") from e

        # account number -> opaque hash, so the number itself never has to be displayed
        hashes = {
            row.get("accountNumber"): row.get("hashValue")
            for row in (numbers or [])
            if isinstance(row, dict)
        }
        return [
            self._to_account(entry["securitiesAccount"], hashes)
            for entry in (payload or [])
            if isinstance(entry, dict) and isinstance(entry.get("securitiesAccount"), dict)
        ]

    # Rows that are cash by another name. A sweep fund is already counted in the cash balance,
    # so listing it as a holding would double-count it on screen and make "shares held" nonsense.
    _CASH_EQUIVALENT = ("MONEY_MARKET_FUND", "CASH_EQUIVALENT")

    def _to_positions(self, acct: dict, today: date) -> list[Position]:
        """Normalise the broker's rows into wheel-shaped holdings.

        Every field is read defensively. Schwab populates a different subset for equities,
        options, funds and sweeps, and the failure mode that matters is not a crash — it is a
        confidently wrong number on a page about someone's money. A field we cannot read stays
        None, and the template renders an em dash.
        """
        out: list[Position] = []
        for row in acct.get("positions") or []:
            if not isinstance(row, dict):
                continue
            instrument = row.get("instrument") or {}
            symbol = str(instrument.get("symbol") or "").strip()
            if not symbol:
                continue
            asset = str(instrument.get("assetType") or "").upper()
            if asset in self._CASH_EQUIVALENT:
                continue  # already inside the cash balance; showing it twice is a lie

            long_qty = _num(row, "longQuantity") or 0.0
            short_qty = _num(row, "shortQuantity") or 0.0
            quantity = long_qty or short_qty
            if not quantity:
                continue  # a closed row the broker still returns

            # Schwab sends the CUSIP as its own field. When it EQUALS the symbol, the symbol is
            # a CUSIP — which is how bonds arrive — and the description is the only readable
            # label the row has. Comparing the two beats guessing from the symbol's shape.
            cusip = str(instrument.get("cusip") or "").strip()
            description = str(instrument.get("description") or "").strip() or None
            common = {
                "asset_type": asset or None,
                "description": description,
                "symbol_is_cusip": bool(cusip and cusip == symbol),
            }

            osi = parse_osi(symbol) if asset == "OPTION" else None
            if osi is not None:
                if short_qty:
                    kind = (PositionKind.SHORT_PUT if osi.option_type is OptionType.PUT
                            else PositionKind.SHORT_CALL)
                else:
                    kind = PositionKind.LONG_OPTION
                # A short put commits strike x 100 per contract, whatever the broker's margin
                # requirement says. This is a CASH-secured view on purpose.
                collateral = (osi.strike * 100 * short_qty) if short_qty else None
                out.append(Position(
                    symbol=symbol,
                    underlying=str(instrument.get("underlyingSymbol") or osi.underlying),
                    kind=kind, quantity=quantity, **common,
                    market_value=_num(row, "marketValue"),
                    average_price=_num(row, "averagePrice", "averageShortPrice",
                                       "averageLongPrice"),
                    unrealized_pl=_num(row, "longOpenProfitLoss", "shortOpenProfitLoss"),
                    strike=osi.strike, expiration=osi.expiration, dte=osi.dte(today),
                    collateral=collateral,
                ))
                continue

            # Everything non-option is a HOLDING, whatever its asset class. Bonds and funds were
            # previously swept into an "also held" footnote that printed raw CUSIPs, which is
            # neither an answer to "what do I own" nor recoverable by the reader.
            kind = PositionKind.SHARES if asset in ("EQUITY", "COLLECTIVE_INVESTMENT") \
                else PositionKind.OTHER
            out.append(Position(
                symbol=symbol, underlying=symbol, kind=kind, quantity=quantity, **common,
                market_value=_num(row, "marketValue"),
                average_price=_num(row, "averagePrice", "averageLongPrice"),
                unrealized_pl=_num(row, "longOpenProfitLoss", "shortOpenProfitLoss"),
            ))
        return out

    def _to_account(self, acct: dict, hashes: dict) -> BrokerageAccount:
        number = str(acct.get("accountNumber") or "")
        # `currentBalances` is live; `initialBalances` is start-of-day and `projectedBalances` is
        # buying-power projection, so neither answers "what is it worth now".
        current = acct.get("currentBalances") or {}

        total = _num(current, "liquidationValue")
        cash = _cash(current)
        # DERIVED, not summed from asset buckets: on the account this was written against
        # `longMarketValue` was zero while bonds and short options were not, so summing buckets
        # under-reports whenever a broker uses one we didn't enumerate.
        invested = None if (total is None or cash is None) else total - cash
        if invested is not None and invested < -1.0:
            # cash over-counted (a swept bucket double-counted inside another). Say so loudly
            # rather than rendering a negative "invested", which reads as a real position.
            logger.warning(
                "schwab balances: cash exceeds total value by %.2f — the cash buckets %s are "
                "likely double-counting for this account type",
                -invested, ", ".join(_CASH_FIELDS),
            )

        raw_type = str(acct.get("type") or "").upper()
        return BrokerageAccount(
            broker=self.broker,
            account_id=str(hashes.get(number) or number),
            display_name=f"••••{number[-4:]}" if len(number) >= 4 else (number or "account"),
            account_type=AccountType.MARGIN if raw_type == "MARGIN"
            else AccountType.CASH if raw_type == "CASH" else None,
            positions=self._to_positions(acct, date.today()),
            balances=AccountBalances(
                total_value=total,
                cash=cash,
                invested=invested,
                # margin accounts report buyingPower; cash accounts cashAvailableForTrading
                buying_power=_num(current, "buyingPower", "cashAvailableForTrading"),
                equity=_num(current, "equity"),
            ),
        )
