"""Map httpx/vendor exceptions to the core's typed ProviderError hierarchy.

Lives in the adapter layer so the core stays framework-free (no httpx import there).
"""

from __future__ import annotations

import httpx

from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)


def map_http_error(exc: Exception) -> ProviderError:
    """Classify a vendor error as systemic (auth/rate/outage — should halt the scan) vs.
    per-item (a 4xx for one symbol/request — skippable). Only the systemic kinds propagate."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return AuthExpiredError(f"provider auth failed (HTTP {code})")
        if code == 429:
            return RateLimitedError("provider rate limit hit (HTTP 429)")
        if code >= 500:
            return ProviderUnavailableError(f"provider server error (HTTP {code})")
        # other 4xx (400 bad symbol, 404 not found, 422 …) — a per-request/per-symbol problem
        return ProviderDataError(f"provider HTTP {code}")
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailableError(f"provider unreachable: {exc}")
    return ProviderError(str(exc))
