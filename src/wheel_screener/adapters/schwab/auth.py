"""Schwab OAuth via schwab-py: the interactive login (auth-login) and token-file load.

schwab-py handles the authorization-code flow, the local-loopback redirect capture, token
persistence, and 30-min access-token auto-refresh. The refresh token still expires after
7 days, so ``login`` must be re-run ~weekly.
"""

from __future__ import annotations

from pathlib import Path

from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import AuthExpiredError


def _creds(s: SchwabSettings) -> tuple[str, str, str, str]:
    return s.client_id, s.client_secret.get_secret_value(), s.callback_url, s.token_path


def login(settings: SchwabSettings):
    """Run the interactive browser login and persist the token file."""
    from schwab.auth import client_from_login_flow

    client_id, secret, callback, token_path = _creds(settings)
    if not client_id or not secret:
        raise RuntimeError("set SCHWAB__CLIENT_ID and SCHWAB__CLIENT_SECRET in .env first")
    Path(token_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return client_from_login_flow(client_id, secret, callback, token_path)


def load_client(settings: SchwabSettings):
    """Load a Schwab client from the token file (auto-refreshes the access token)."""
    from schwab.auth import client_from_token_file

    client_id, secret, _callback, token_path = _creds(settings)
    if not Path(token_path).expanduser().exists():
        raise AuthExpiredError(f"no Schwab token at {token_path}; run `wheel-screener auth-login`")
    return client_from_token_file(token_path, client_id, secret)
