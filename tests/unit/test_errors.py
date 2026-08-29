from __future__ import annotations

import httpx

from wheel_screener.adapters.errors import map_http_error
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderUnavailableError,
    RateLimitedError,
)


def _status(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


def test_map_http_error_status_codes() -> None:
    assert isinstance(map_http_error(_status(401)), AuthExpiredError)  # systemic: re-auth
    assert isinstance(map_http_error(_status(403)), AuthExpiredError)
    assert isinstance(map_http_error(_status(429)), RateLimitedError)  # systemic: back off
    assert isinstance(map_http_error(_status(503)), ProviderUnavailableError)  # systemic: outage
    assert isinstance(map_http_error(_status(400)), ProviderDataError)  # per-symbol: skippable
    assert isinstance(map_http_error(_status(404)), ProviderDataError)


def test_map_http_error_transport_is_unavailable() -> None:
    assert isinstance(map_http_error(httpx.ConnectError("boom")), ProviderUnavailableError)


def test_auth_errors_name_the_provider_and_the_fix() -> None:
    """Three adapters share this mapper, so a bare "provider auth failed" left the reader
    guessing which of three credentials expired — and each has a different fix."""
    from wheel_screener.adapters.errors import ALPACA, FMP, SCHWAB

    alpaca = str(map_http_error(_status(401), ALPACA))
    assert "Alpaca" in alpaca and "401" in alpaca and "ALPACA__API_KEY" in alpaca

    schwab = str(map_http_error(_status(401), SCHWAB))
    assert "Schwab" in schwab and "auth-login" in schwab, "OAuth needs a re-auth instruction"

    fmp = str(map_http_error(_status(403), FMP))
    assert "FMP" in fmp and "FMP__API_KEY" in fmp

    # the three fixes are genuinely different, which is the whole point
    assert len({alpaca, schwab, fmp}) == 3


def test_non_auth_errors_also_name_the_provider() -> None:
    from wheel_screener.adapters.errors import ALPACA

    assert "Alpaca" in str(map_http_error(_status(429), ALPACA))
    assert "Alpaca" in str(map_http_error(_status(503), ALPACA))
    assert "Alpaca" in str(map_http_error(httpx.ConnectError("no route"), ALPACA))


def test_unattributed_errors_still_map() -> None:
    # an untagged call site keeps working; it just can't name the vendor
    assert isinstance(map_http_error(_status(401)), AuthExpiredError)
