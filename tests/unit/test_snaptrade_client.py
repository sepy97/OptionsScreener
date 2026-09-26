"""The SnapTrade wrapper over the official SDK.

The SDK's HTTP layer is stubbed and everything above it is real — signing, URL building, response
validation — so these check what actually leaves the process and what comes back, not a mock of
the SDK's methods.
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("snaptrade_client")

import urllib3  # noqa: E402
from snaptrade_client import rest  # noqa: E402

from wheel_screener.adapters.snaptrade.client import SnapTradeClient, SnapTradeUser  # noqa: E402
from wheel_screener.core.errors import (  # noqa: E402
    AuthExpiredError,
    ProviderDataError,
    ProviderUnavailableError,
    RateLimitedError,
)

USER = SnapTradeUser("user-1", "THE-SECRET")
ACCOUNT = {"id": "11111111-2222-3333-4444-555555555555",
           "brokerage_authorization": "22222222-2222-3333-4444-555555555555",
           "name": "Joint", "number": "****1234", "institution_name": "Fidelity",
           "created_date": "2026-01-01T00:00:00Z", "sync_status": {}, "balance": {},
           "meta": {}, "is_paper": False}


@pytest.fixture
def wire(monkeypatch):
    """Record every request the SDK sends, and answer from `wire.replies` by path."""
    class Wire:
        sent: list[dict] = []
        replies: dict[str, tuple[int, object]] = {}
        error: Exception | None = None

    Wire.sent, Wire.replies, Wire.error = [], {}, None

    def request(self, method, url, headers=None, fields=None, body=None, *args, **kwargs):
        parts = urlsplit(url)
        Wire.sent.append({"method": method, "path": parts.path, "query": parse_qs(parts.query),
                          "headers": dict(headers or {}),
                          "body": json.loads(body) if body else None})
        if Wire.error is not None:
            raise Wire.error
        status, payload = Wire.replies.get(parts.path, (200, {}))
        return rest.ResponseWrapper(urllib3.HTTPResponse(
            body=json.dumps(payload).encode(), status=status,
            headers={"content-type": "application/json"}, preload_content=True), 0.0)

    monkeypatch.setattr(rest.RESTClientObject, "request", request)
    return Wire


def test_calls_carry_the_persons_credentials_and_a_signature(wire) -> None:
    wire.replies["/accounts"] = (200, [ACCOUNT])
    got = SnapTradeClient("CLIENT", "KEY").accounts(USER)
    assert [a["id"] for a in got] == [ACCOUNT["id"]]
    (req,) = wire.sent
    assert req["query"]["userId"] == ["user-1"] and req["query"]["userSecret"] == ["THE-SECRET"]
    assert req["query"]["clientId"] == ["CLIENT"] and "timestamp" in req["query"]
    assert req["headers"].get("Signature"), "the SDK signs every request"


def test_the_portal_is_always_asked_for_read_only_access(wire) -> None:
    wire.replies["/snapTrade/login"] = (200, {"redirectURI": "https://app.snaptrade.com/p",
                                              "sessionId": "s"})
    url = SnapTradeClient("C", "K").portal_url(USER, redirect="https://steadybull.net/r",
                                               reconnect="conn-9")
    assert url == "https://app.snaptrade.com/p"
    body = wire.sent[0]["body"]
    assert body["connectionType"] == "read" and body["reconnect"] == "conn-9"
    assert body["customRedirect"] == "https://steadybull.net/r"


def test_a_portal_link_that_is_not_https_is_refused(wire) -> None:
    wire.replies["/snapTrade/login"] = (200, {"redirectURI": "javascript:alert(1)"})
    with pytest.raises(ProviderDataError):
        SnapTradeClient("C", "K").portal_url(USER, redirect="https://steadybull.net/r")


def test_registering_returns_the_secret(wire) -> None:
    wire.replies["/snapTrade/registerUser"] = (200, {"userId": "u", "userSecret": "fresh"})
    assert SnapTradeClient("C", "K").register_user("u") == "fresh"
    assert wire.sent[0]["body"] == {"userId": "u"}


@pytest.mark.parametrize("status, error", [
    (401, AuthExpiredError), (403, AuthExpiredError), (429, RateLimitedError),
    (500, ProviderUnavailableError), (404, ProviderDataError),
])
def test_failures_are_typed_and_carry_nothing_from_the_request(wire, status, error) -> None:
    """The SDK's own exception text includes the response headers and body."""
    wire.replies["/accounts"] = (status, {"detail": "nope", "echo": "THE-SECRET"})
    with pytest.raises(error) as caught:
        SnapTradeClient("C", "K").accounts(USER)
    assert "THE-SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_network_failure_does_not_leak_the_url_it_was_calling(wire) -> None:
    """urllib3's errors name the URL, and every per-person URL carries the secret."""
    wire.error = urllib3.exceptions.MaxRetryError(
        None, "https://api.snaptrade.com/accounts?userSecret=THE-SECRET", "timed out")
    with pytest.raises(ProviderUnavailableError) as caught:
        SnapTradeClient("C", "K").accounts(USER)
    assert "THE-SECRET" not in str(caught.value) and caught.value.__suppress_context__


def test_a_hung_call_has_a_timeout() -> None:
    """The SDK sends no timeout of its own; without one a stalled call stalls the page."""
    client = SnapTradeClient("C", "K", timeout=7.0)
    pool = client._sdk.account_information.api_client.rest_client.pool_manager
    timeout = pool.connection_pool_kw["timeout"]
    assert timeout.read_timeout == 7.0 and timeout.connect_timeout == 5.0


def test_positions_and_activities_come_out_of_their_envelopes(wire) -> None:
    aid = ACCOUNT["id"]
    wire.replies[f"/accounts/{aid}/positions/all"] = (200, {
        "results": [{"units": "1", "instrument": {"kind": "stock", "symbol": "AAPL",
                                                  "id": "33333333-2222-3333-4444-555555555555"}}],
        "data_freshness": {"as_of": "2026-09-26T14:00:00Z"}})
    wire.replies[f"/accounts/{aid}/activities"] = (200, {"data": [], "pagination": {}})
    client = SnapTradeClient("C", "K")
    assert client.positions(USER, aid)[0]["units"] == "1"
    assert client.activities(USER, aid, date(2026, 8, 1), date(2026, 9, 26)) == []
    assert wire.sent[1]["query"]["startDate"] == ["2026-08-01"]
