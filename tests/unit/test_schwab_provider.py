from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import RateLimitedError
from wheel_screener.core.models import ChainFilter


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def get_option_chain(self, symbol: str, **kwargs: object) -> _FakeResp:
        self.calls += 1
        return _FakeResp(self.payload)


def _http_429() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://schwab")
    return httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))


class _FlakyClient:
    """Raises a transient 429 for the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int, payload: dict) -> None:
        self.fail_times = fail_times
        self.payload = payload
        self.calls = 0

    def get_option_chain(self, symbol: str, **kwargs: object) -> _FakeResp:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _http_429()
        return _FakeResp(self.payload)


def test_chain_cache_skips_second_fetch(tmp_path: Path) -> None:
    settings = SchwabSettings(chain_cache_dir=str(tmp_path), chain_cache_ttl_seconds=300)
    prov = SchwabChainProvider(settings)
    prov._client = _FakeClient({"symbol": "AAA", "putExpDateMap": {}, "underlyingPrice": 10.0})
    filt = ChainFilter(min_dte=30, max_dte=45)

    first = prov.get_chain("AAA", filt)
    second = prov.get_chain("AAA", filt)

    assert first.underlying_symbol == "AAA" and second.underlying_symbol == "AAA"
    assert prov._client.calls == 1  # second request served from the on-disk cache


def test_get_chain_retries_transient_then_succeeds() -> None:
    settings = SchwabSettings(
        chain_cache_enabled=False, max_retries=3, retry_backoff_multiplier=0.0
    )
    prov = SchwabChainProvider(settings)
    payload = {"symbol": "AAA", "putExpDateMap": {}, "underlyingPrice": 10.0}
    prov._client = _FlakyClient(fail_times=2, payload=payload)
    snap = prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    assert snap.underlying_symbol == "AAA"
    assert prov._client.calls == 3  # 2 transient 429s retried, 3rd succeeds


def test_get_chain_raises_after_exhausting_retries() -> None:
    settings = SchwabSettings(
        chain_cache_enabled=False, max_retries=2, retry_backoff_multiplier=0.0
    )
    prov = SchwabChainProvider(settings)
    prov._client = _FlakyClient(fail_times=99, payload={})
    with pytest.raises(RateLimitedError):  # systemic -> surfaced, not masked
        prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    assert prov._client.calls == 3  # max_retries=2 -> 3 attempts
