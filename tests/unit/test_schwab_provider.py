from __future__ import annotations

from pathlib import Path

from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.config import SchwabSettings
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


def test_chain_cache_skips_second_fetch(tmp_path: Path) -> None:
    settings = SchwabSettings(chain_cache_dir=str(tmp_path), chain_cache_ttl_seconds=300)
    prov = SchwabChainProvider(settings)
    prov._client = _FakeClient({"symbol": "AAA", "putExpDateMap": {}, "underlyingPrice": 10.0})
    filt = ChainFilter(min_dte=30, max_dte=45)

    first = prov.get_chain("AAA", filt)
    second = prov.get_chain("AAA", filt)

    assert first.underlying_symbol == "AAA" and second.underlying_symbol == "AAA"
    assert prov._client.calls == 1  # second request served from the on-disk cache
