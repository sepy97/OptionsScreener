from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from wheel_screener.adapters.alpaca.provider import AlpacaChainProvider
from wheel_screener.config import AlpacaSettings
from wheel_screener.core.errors import RateLimitedError
from wheel_screener.core.models import ChainFilter, OptionType

SNAP = "https://data.alpaca.markets/v1beta1/options/snapshots/AAA"
CONTRACTS = "https://api.alpaca.markets/v2/options/contracts"  # live host is the default


def _occ() -> str:
    # a put ~40 days out so it lands inside the default 30-45 DTE window
    exp = date.today() + timedelta(days=40)
    return f"AAA{exp:%y%m%d}P00090000"


def _settings(**kw) -> AlpacaSettings:
    base = dict(api_key=SecretStr("k"), api_secret=SecretStr("s"), chain_cache_enabled=False)
    return AlpacaSettings(**{**base, **kw})


def _snap_body(occ: str) -> dict:
    return {
        "snapshots": {
            occ: {
                "latestQuote": {"bp": 1.40, "ap": 1.50},
                "greeks": {"delta": -0.20},
                "impliedVolatility": 0.345,
            }
        },
        "next_page_token": None,
    }


def _oi_body(occ: str) -> dict:
    return {"option_contracts": [{"symbol": occ, "open_interest": "800"}], "next_page_token": None}


@respx.mock
def test_get_chain_merges_snapshot_and_open_interest() -> None:
    occ = _occ()
    snap = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))
    oi = respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_oi_body(occ)))
    chain = AlpacaChainProvider(_settings()).get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    assert snap.called and oi.called
    assert len(chain.contracts) == 1
    c = chain.contracts[0]
    assert c.option_type == OptionType.PUT and c.strike == 90.0
    assert c.bid == 1.40 and c.mid == 1.45 and c.delta == -0.20
    assert c.implied_volatility == 0.345 and c.open_interest == 800  # merged from contracts


@respx.mock
def test_get_chain_sends_auth_headers_and_feed() -> None:
    occ = _occ()
    snap = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))
    respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_oi_body(occ)))
    prov = AlpacaChainProvider(_settings(feed="opra"))
    prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    req = snap.calls.last.request
    assert req.headers["APCA-API-KEY-ID"] == "k" and req.headers["APCA-API-SECRET-KEY"] == "s"
    assert "feed=opra" in str(req.url) and "type=put" in str(req.url)


@respx.mock
def test_chain_cache_skips_second_fetch(tmp_path) -> None:
    occ = _occ()
    snap = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))
    oi = respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_oi_body(occ)))
    prov = AlpacaChainProvider(
        _settings(
            chain_cache_enabled=True, chain_cache_dir=str(tmp_path), chain_cache_ttl_seconds=300
        )
    )
    filt = ChainFilter(min_dte=30, max_dte=45)
    prov.get_chain("AAA", filt)
    prov.get_chain("AAA", filt)
    assert snap.call_count == 1 and oi.call_count == 1  # second served from the disk cache


@respx.mock
def test_get_chain_retries_transient_then_succeeds() -> None:
    occ = _occ()
    respx.get(SNAP).mock(side_effect=[
        httpx.Response(429), httpx.Response(429), httpx.Response(200, json=_snap_body(occ)),
    ])
    respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_oi_body(occ)))
    prov = AlpacaChainProvider(_settings(max_retries=3, retry_backoff_multiplier=0.0))
    chain = prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    assert len(chain.contracts) == 1  # 2 transient 429s retried, 3rd succeeds


@respx.mock
def test_get_chain_raises_after_exhausting_retries() -> None:
    respx.get(SNAP).mock(return_value=httpx.Response(429))
    prov = AlpacaChainProvider(_settings(max_retries=1, retry_backoff_multiplier=0.0))
    with pytest.raises(RateLimitedError):  # systemic -> surfaced, not masked
        prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))


@respx.mock
def test_snapshot_pagination_merges_pages() -> None:
    exp = date.today() + timedelta(days=40)
    occ1, occ2 = f"AAA{exp:%y%m%d}P00090000", f"AAA{exp:%y%m%d}P00085000"
    page1 = {"snapshots": {occ1: {"latestQuote": {"bp": 1.4, "ap": 1.5}}},
             "next_page_token": "tok2"}
    page2 = {"snapshots": {occ2: {"latestQuote": {"bp": 0.8, "ap": 0.9}}},
             "next_page_token": None}
    snap = respx.get(SNAP).mock(side_effect=[httpx.Response(200, json=page1),
                                             httpx.Response(200, json=page2)])
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json={"option_contracts": [{"symbol": occ1, "open_interest": "800"},
                                         {"symbol": occ2, "open_interest": "300"}],
                   "next_page_token": None}))
    chain = AlpacaChainProvider(_settings()).get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    assert {c.strike for c in chain.contracts} == {90.0, 85.0}  # both pages merged
    assert "page_token=tok2" in str(snap.calls[1].request.url)  # continuation token sent


def test_capabilities_reflects_feed() -> None:
    assert AlpacaChainProvider(_settings(feed="opra")).capabilities().realtime is True
    caps = AlpacaChainProvider(_settings(feed="indicative")).capabilities()
    assert caps.name == "alpaca" and caps.realtime is False and caps.max_concurrency == 16
