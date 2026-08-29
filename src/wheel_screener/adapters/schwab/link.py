"""Establishing and ending the Schwab link — the server side of "sign in with Schwab".

The flow is three moves: build an authorize URL, send the browser to Schwab, and exchange the code
Schwab redirects back with. schwab-py supplies all three, and its ``AuthContext`` turns out to hold
only ``(callback_url, authorization_url, state)`` — ``client_from_received_url`` reads just the
first and last. **So only the `state` string has to survive the redirect**: the context is rebuilt
on the far side. There is no PKCE verifier to stash, no server affinity, and an app restart
mid-flow costs the user one more click rather than wedging the handshake.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import ProviderDataError, ProviderError, ProviderUnavailableError
from wheel_screener.core.models import BrokerLinkStatus

logger = logging.getLogger(__name__)

# Schwab refresh tokens last 7 days. Not configurable — it is the broker's rule, and the session
# lifetime is capped to it so one clock governs both the credential and the login.
REFRESH_TOKEN_DAYS = 7


class SchwabOAuthLink:
    """A :class:`~wheel_screener.core.ports.OAuthBrokerLink` for Schwab."""

    broker = "schwab"

    def __init__(self, settings: SchwabSettings) -> None:
        self._settings = settings

    @property
    def _token_path(self) -> Path:
        return Path(self._settings.token_path).expanduser()

    def _context(self, state: str):
        from schwab.auth import get_auth_context

        return get_auth_context(self._settings.client_id, self._settings.callback_url, state=state)

    def authorize_url(self, state: str) -> str:
        """Where to send the browser. ``state`` is ours, issued and tracked server-side."""
        if not self._settings.client_id or not self._settings.client_secret.get_secret_value():
            raise ProviderUnavailableError(
                "Schwab is not configured — set SCHWAB__CLIENT_ID and SCHWAB__CLIENT_SECRET"
            )
        return self._context(state).authorization_url

    def complete(self, received_url: str, state: str) -> BrokerLinkStatus:
        """Exchange the code Schwab redirected back with, and persist the token."""
        from schwab.auth import client_from_received_url

        self._token_path.parent.mkdir(parents=True, exist_ok=True)

        def write_token(token, *_args):
            # schwab-py hands us the token dict; it is written here so the file lands where this
            # deployment wants it, with permissions we control rather than the library's default.
            payload = {"creation_timestamp": int(datetime.now(tz=UTC).timestamp()), "token": token}
            self._token_path.write_text(json.dumps(payload))
            self._token_path.chmod(0o600)

        try:
            client_from_received_url(
                self._settings.client_id,
                self._settings.client_secret.get_secret_value(),
                self._context(state),
                received_url,
                write_token,
            )
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - the exchange failing is a provider problem
            # never echo the URL: it carries the authorization code
            raise ProviderDataError(f"Schwab rejected the authorization exchange: {e}") from e
        return self.status()

    def status(self) -> BrokerLinkStatus:
        """Whether a usable token is on disk, and when its refresh dies."""
        path = self._token_path
        if not path.exists():
            return BrokerLinkStatus(broker=self.broker, connected=False)
        try:
            created = json.loads(path.read_text()).get("creation_timestamp")
            minted = datetime.fromtimestamp(float(created), tz=UTC)
        except Exception as e:  # noqa: BLE001 - an unreadable token is a disconnected link
            logger.warning("schwab token unreadable (%s); treating the link as disconnected", e)
            return BrokerLinkStatus(broker=self.broker, connected=False)
        expires = minted + timedelta(days=REFRESH_TOKEN_DAYS)
        return BrokerLinkStatus(
            broker=self.broker,
            connected=expires > datetime.now(tz=UTC),
            expires_at=expires,
        )

    def revoke(self) -> None:
        """Delete the stored token. Disconnect must remove the credential, not just the session."""
        self._token_path.unlink(missing_ok=True)
