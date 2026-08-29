"""Map httpx/vendor exceptions to the core's typed ProviderError hierarchy.

Lives in the adapter layer so the core stays framework-free (no httpx import there).

Errors NAME THEIR PROVIDER and, for auth failures, say what to do about it. Three adapters
share this mapper, so a bare "provider auth failed" left the reader guessing which of three
credentials had expired and which of three unrelated fixes applied.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)


@dataclass(frozen=True)
class Provider:
    """Who we were talking to, and how a human fixes its credentials."""

    name: str
    auth_remedy: str = ""

    def __str__(self) -> str:  # used directly in messages
        return self.name


# The three credentialed providers. Each auth failure has a DIFFERENT fix, which is the whole
# reason these carry a remedy: Schwab re-authorizes over OAuth, the other two are static keys.
ALPACA = Provider(
    "Alpaca",
    "check ALPACA__API_KEY / ALPACA__API_SECRET, and that ALPACA__TRADING_BASE_URL matches the "
    "key's environment (paper keys need paper-api.alpaca.markets)",
)
SCHWAB = Provider("Schwab", "re-run `wheel-screener auth-login` to refresh the OAuth token")
FMP = Provider("FMP", "check FMP__API_KEY")
UNKNOWN = Provider("The data provider")


def map_http_error(exc: Exception, provider: Provider = UNKNOWN) -> ProviderError:
    """Classify a vendor error as systemic (auth/rate/outage — should halt the scan) vs.
    per-item (a 4xx for one symbol/request — skippable). Only the systemic kinds propagate."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            message = f"{provider} rejected our credentials (HTTP {code})"
            if provider.auth_remedy:
                message = f"{message} — {provider.auth_remedy}"
            return AuthExpiredError(message)
        if code == 429:
            return RateLimitedError(f"{provider} rate limit hit (HTTP 429) — back off and retry")
        if code >= 500:
            return ProviderUnavailableError(f"{provider} server error (HTTP {code})")
        # other 4xx (400 bad symbol, 404 not found, 422 …) — a per-request/per-symbol problem
        return ProviderDataError(f"{provider} returned HTTP {code}")
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailableError(f"{provider} unreachable: {exc}")
    return ProviderError(f"{provider} failed: {exc}")
