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
