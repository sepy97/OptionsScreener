"""Schwab OAuth via schwab-py: the interactive login (auth-login) and token-file load.

schwab-py handles the authorization-code flow, the local-loopback redirect capture, token
persistence, and 30-min access-token auto-refresh. The refresh token still expires after
7 days, so ``login`` must be re-run ~weekly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import AuthExpiredError

logger = logging.getLogger(__name__)


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


def repair_token_file(path: Path) -> bool:
    """Unwrap a token this app wrote twice. Returns True if the file was repaired.

    An earlier writer added its own ``{creation_timestamp, token}`` envelope on top of the one
    schwab-py had already applied. The result loads without complaint and then fails every API
    call with ``unsupported_token_type``. Repairing on load rather than demanding a fresh login
    matters because the credential underneath is still valid — the envelope is wrong, not the
    grant — and a redeploy should not silently cost someone their broker link.
    """
    try:
        payload = json.loads(path.read_text())
        inner = payload.get("token")
        if not (isinstance(inner, dict) and "creation_timestamp" in inner and "token" in inner):
            return False
        path.write_text(json.dumps(inner))  # the inner envelope is schwab-py's own, and correct
        path.chmod(0o600)
    except (OSError, ValueError, AttributeError) as e:
        logger.warning("schwab token could not be inspected for repair (%s)", e)
        return False
    logger.info("schwab token was wrapped twice by an older build; unwrapped it in place")
    return True


def load_client(settings: SchwabSettings):
    """Load a Schwab client from the token file (auto-refreshes the access token)."""
    from schwab.auth import client_from_token_file

    client_id, secret, _callback, token_path = _creds(settings)
    path = Path(token_path).expanduser()
    if not path.exists():
        raise AuthExpiredError(f"no Schwab token at {token_path}; run `wheel-screener auth-login`")
    repair_token_file(path)
    return client_from_token_file(token_path, client_id, secret)
