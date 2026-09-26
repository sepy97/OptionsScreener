"""SnapTrade through its official Python SDK: the handful of calls this app makes, typed errors.

The SDK signs every request and knows every endpoint; this wrapper exists for three things the SDK
does not do for us:

* **Plain JSON back.** Each call returns SnapTrade's response body exactly as sent (the SDK's raw
  ``response.data``), so the mapping code reads the shapes documented in SnapTrade's API spec
  rather than generated wrapper types.
* **Failures that are safe to show.** The SDK's exception text includes response headers and body,
  and its network errors can include the request URL — which carries the person's secret in its
  query string. Every failure is rebuilt as this app's typed error, from our own words only.
* **A timeout.** The SDK passes none to its HTTP layer, so a hung call would hang the page it
  serves. A default is set on its connection pool.

**Read-only by construction.** No method here places, previews or cancels an order, and the
connection portal is always asked for ``connectionType: read``.

**Users.** Each person is registered with SnapTrade under this app's own internal user id — never
an email, which can change — and SnapTrade returns a secret that every per-person call must carry.
It is encrypted before it is stored (see ``api.secretbox``) and lives in memory only for the length
of a request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from wheel_screener.adapters.errors import SNAPTRADE
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)


@dataclass(frozen=True)
class SnapTradeUser:
    """One person's SnapTrade identity. ``secret`` is decrypted, and short-lived by design."""

    user_id: str
    secret: str


def _fail(status: int | None) -> ProviderError:
    if status in (401, 403):
        return AuthExpiredError(f"{SNAPTRADE} refused the request (HTTP {status})")
    if status == 429:
        return RateLimitedError(f"{SNAPTRADE} rate limit hit — wait a minute and try again")
    if status is not None and status >= 500:
        return ProviderUnavailableError(f"{SNAPTRADE} is having trouble (HTTP {status})")
    if status is not None:
        return ProviderDataError(f"{SNAPTRADE} returned HTTP {status}")
    return ProviderUnavailableError(f"{SNAPTRADE} could not be reached")


class SnapTradeClient:
    """Built once and shared — it owns the SDK's connection pool; the per-person part is the
    ``SnapTradeUser`` passed to each call."""

    def __init__(self, client_id: str, consumer_key: str, *, timeout: float = 20.0) -> None:
        import urllib3
        from snaptrade_client import SnapTrade
        from snaptrade_client.auth import SnapTradeAuth

        self._sdk = SnapTrade(
            auth=SnapTradeAuth.commercial_api_key(consumer_key=consumer_key, client_id=client_id)
        )
        pool = self._sdk.account_information.api_client.rest_client.pool_manager
        pool.connection_pool_kw["timeout"] = urllib3.Timeout(connect=5.0, read=timeout)

    def _call(self, fn: Callable[..., Any], **kwargs) -> Any:
        from snaptrade_client.exceptions import ApiException

        try:
            response = fn(**kwargs)
        except ApiException as e:
            raise _fail(e.status) from None  # its text carries headers and body — never shown
        except ProviderError:
            raise
        except Exception:  # noqa: BLE001 - network errors can name the URL, i.e. the secret
            raise _fail(None) from None
        raw = getattr(getattr(response, "response", None), "data", b"")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ProviderDataError(f"{SNAPTRADE} returned an unreadable body") from None

    # --- people ---------------------------------------------------------------------------

    def register_user(self, user_id: str) -> str:
        """Register a person under our internal id; returns their secret, to be encrypted."""
        data = self._call(self._sdk.authentication.register_snap_trade_user, user_id=user_id)
        secret = (data or {}).get("userSecret") if isinstance(data, dict) else None
        if not isinstance(secret, str) or not secret:
            raise ProviderDataError(f"{SNAPTRADE} registered the user but returned no secret")
        return secret

    def delete_user(self, user_id: str) -> None:
        """Remove the person and every connection they made, at SnapTrade."""
        self._call(self._sdk.authentication.delete_snap_trade_user, user_id=user_id)

    def portal_url(
        self, user: SnapTradeUser, *, redirect: str, reconnect: str | None = None,
    ) -> str:
        """A one-time link into SnapTrade's connection portal, where the person picks their broker
        and signs in AT the broker. ``reconnect`` repairs a broken connection in place."""
        data = self._call(
            self._sdk.authentication.login_snap_trade_user,
            user_id=user.user_id, user_secret=user.secret,
            custom_redirect=redirect, connection_type="read", reconnect=reconnect,
        )
        url = data.get("redirectURI") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ProviderDataError(f"{SNAPTRADE} did not return a connection link")
        return url

    # --- connections ----------------------------------------------------------------------

    def connections(self, user: SnapTradeUser) -> list[dict]:
        """The person's brokerage connections, each with a ``disabled`` flag to act on."""
        return _list(self._call(
            self._sdk.connections.list_brokerage_authorizations,
            user_id=user.user_id, user_secret=user.secret,
        ))

    def remove_connection(self, user: SnapTradeUser, connection_id: str) -> None:
        self._call(
            self._sdk.connections.delete_connection,
            connection_id=connection_id, user_id=user.user_id, user_secret=user.secret,
        )

    # --- accounts -------------------------------------------------------------------------

    def accounts(self, user: SnapTradeUser) -> list[dict]:
        return _list(self._call(
            self._sdk.account_information.list_user_accounts,
            user_id=user.user_id, user_secret=user.secret,
        ))

    def balances(self, user: SnapTradeUser, account_id: str) -> list[dict]:
        return _list(self._call(
            self._sdk.account_information.get_user_account_balance,
            account_id=account_id, user_id=user.user_id, user_secret=user.secret,
        ))

    def positions(self, user: SnapTradeUser, account_id: str) -> tuple[list[dict], str | None]:
        """Every position — stocks, funds and options together (``/positions/all``) — and when
        SnapTrade last fetched them from the broker (``data_freshness.as_of``), if it says."""
        data = self._call(
            self._sdk.account_information.get_all_account_positions,
            account_id=account_id, user_id=user.user_id, user_secret=user.secret,
        )
        if not isinstance(data, dict):
            return _list(data), None
        as_of = (data.get("data_freshness") or {}).get("as_of")
        return _list(data.get("results")), as_of if isinstance(as_of, str) else None

    def activities(
        self, user: SnapTradeUser, account_id: str, start: date, end: date
    ) -> list[dict]:
        data = self._call(
            self._sdk.account_information.get_account_activities,
            account_id=account_id, start_date=start, end_date=end,
            user_id=user.user_id, user_secret=user.secret,
        )
        return _list(data.get("data") if isinstance(data, dict) else data)


def _list(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
