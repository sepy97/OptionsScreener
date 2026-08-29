from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from wheel_screener.adapters.alpaca.provider import AlpacaChainProvider
from wheel_screener.config import AlpacaSettings
from wheel_screener.core.errors import ProviderDataError, RateLimitedError
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
    assert caps.name == "alpaca" and caps.realtime is False and caps.max_concurrency == 8


def _call_occ() -> str:
    exp = date.today() + timedelta(days=40)
    return f"AAA{exp:%y%m%d}C00110000"


def _call_snap_body(occ: str) -> dict:
    return {
        "snapshots": {
            occ: {
                "latestQuote": {"bp": 1.40, "ap": 1.50},
                "greeks": {"delta": 0.20},  # calls carry positive delta
                "impliedVolatility": 0.345,
            }
        },
        "next_page_token": None,
    }


@respx.mock
def test_get_chain_requests_calls_when_asked() -> None:
    occ = _call_occ()
    snap = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_call_snap_body(occ)))
    oi = respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_oi_body(occ)))
    chain = AlpacaChainProvider(_settings()).get_chain(
        "AAA", ChainFilter(min_dte=30, max_dte=45, option_type=OptionType.CALL)
    )
    # BOTH endpoints must switch sides — a put OI lookup would silently drop every call's OI
    assert "type=call" in str(snap.calls.last.request.url)
    assert "type=call" in str(oi.calls.last.request.url)
    assert chain.contracts[0].option_type is OptionType.CALL
    assert chain.contracts[0].strike == 110.0


@respx.mock
def test_chain_cache_is_keyed_by_option_type(tmp_path) -> None:
    """A shared key would serve the put chain for a call request (and vice versa) for the whole
    TTL — the failure is silent and the numbers look plausible."""
    put_occ, call_occ = _occ(), _call_occ()

    def _route(request):
        side = "call" if "type=call" in str(request.url) else "put"
        occ = call_occ if side == "call" else put_occ
        if "options/contracts" in str(request.url):
            return httpx.Response(200, json=_oi_body(occ))
        body = _call_snap_body(occ) if side == "call" else _snap_body(occ)
        return httpx.Response(200, json=body)

    respx.get(SNAP).mock(side_effect=_route)
    respx.get(CONTRACTS).mock(side_effect=_route)
    prov = AlpacaChainProvider(
        _settings(
            chain_cache_enabled=True, chain_cache_dir=str(tmp_path), chain_cache_ttl_seconds=300
        )
    )
    puts = prov.get_chain("AAA", ChainFilter(min_dte=30, max_dte=45))
    calls = prov.get_chain(
        "AAA", ChainFilter(min_dte=30, max_dte=45, option_type=OptionType.CALL)
    )
    assert puts.contracts[0].option_type is OptionType.PUT
    assert calls.contracts[0].option_type is OptionType.CALL  # not the cached put chain


@respx.mock
def test_spot_reads_the_latest_trade() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAA/snapshot").mock(
        return_value=httpx.Response(200, json={"latestTrade": {"p": 305.94}})
    )
    assert AlpacaChainProvider(_settings()).spot("AAA") == 305.94


@respx.mock
def test_spot_falls_back_to_the_daily_bar_then_gives_up_quietly() -> None:
    """Spot is a yield denominator, not a hard dependency: losing it must blank the yield cell,
    never fail the search."""
    url = "https://data.alpaca.markets/v2/stocks/AAA/snapshot"
    respx.get(url).mock(return_value=httpx.Response(200, json={"dailyBar": {"c": 100.5}}))
    assert AlpacaChainProvider(_settings()).spot("AAA") == 100.5
    respx.get(url).mock(return_value=httpx.Response(500))
    assert AlpacaChainProvider(_settings(max_retries=0)).spot("AAA") is None


@respx.mock
def test_check_auth_reports_a_rejected_key() -> None:
    """Presence of a key proves nothing — a revoked key is still present. Only a call tells."""
    settings = AlpacaSettings(
        api_key="k", api_secret="s", trading_base_url="https://paper-api.alpaca.markets"
    )
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized."})
    )
    detail = AlpacaChainProvider(settings).check_auth()
    assert detail is not None
    assert "Alpaca" in detail and "401" in detail
    assert "ALPACA__API_KEY" in detail, "must say which setting to fix"
    assert "paper-api" in detail, "must mention the paper/live mismatch trap"


@respx.mock
def test_check_auth_returns_none_when_the_key_works() -> None:
    settings = AlpacaSettings(
        api_key="k", api_secret="s", trading_base_url="https://paper-api.alpaca.markets"
    )
    route = respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"id": "acct"})
    )
    assert AlpacaChainProvider(settings).check_auth() is None
    assert route.call_count == 1, "one cheap call, not a chain pull"


@respx.mock
def test_check_auth_reports_an_outage_without_raising() -> None:
    settings = AlpacaSettings(api_key="k", api_secret="s")
    respx.get(url__startswith="https://api.alpaca.markets").mock(
        side_effect=httpx.ConnectError("no route")
    )
    detail = AlpacaChainProvider(settings).check_auth()
    assert detail is not None and "Alpaca" in detail


# --- bulk prefetch -------------------------------------------------------------------------
# Most of a screen's chain requests return nothing (measured: 61% of names have no contract in
# the window), and `underlying_symbols` is plural — so one batched call can say which names are
# worth a snapshot request at all.

def _filt() -> ChainFilter:
    return ChainFilter(option_type=OptionType.PUT, min_dte=30, max_dte=45)


def _bulk_body(rows):
    return {"option_contracts": [
        {"symbol": occ, "underlying_symbol": und, "open_interest": str(oi)}
        for und, occ, oi in rows
    ], "next_page_token": None}


@respx.mock
def test_prefetch_reports_which_names_have_nothing() -> None:
    occ = _occ()
    route = respx.get(CONTRACTS).mock(
        return_value=httpx.Response(200, json=_bulk_body([("AAA", occ, 800)]))
    )
    provider = AlpacaChainProvider(_settings())
    empty = provider.prefetch(["AAA", "BBB", "CCC"], _filt())
    assert empty == {"BBB", "CCC"}, "names absent from the batch have no contracts"
    assert route.call_count == 1, "one call for all three names"


@respx.mock
def test_a_prefetched_name_does_not_re_request_open_interest() -> None:
    occ = _occ()
    bulk = respx.get(CONTRACTS).mock(
        return_value=httpx.Response(200, json=_bulk_body([("AAA", occ, 800)]))
    )
    snap = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))
    provider = AlpacaChainProvider(_settings())
    provider.prefetch(["AAA"], _filt())
    chain = provider.get_chain("AAA", _filt())
    assert chain.contracts[0].open_interest == 800  # came from the prefetch
    assert snap.call_count == 1
    assert bulk.call_count == 1, "the per-symbol contracts call must not fire again"


@respx.mock
def test_a_different_window_falls_back_to_a_per_symbol_call() -> None:
    """The prefetch is keyed by window, so a search over another DTE range can't reuse it."""
    occ = _occ()
    respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))
    bulk = respx.get(CONTRACTS).mock(
        return_value=httpx.Response(200, json=_bulk_body([("AAA", occ, 800)]))
    )
    provider = AlpacaChainProvider(_settings())
    provider.prefetch(["AAA"], _filt())
    provider.get_chain("AAA", ChainFilter(option_type=OptionType.PUT, min_dte=7, max_dte=20))
    assert bulk.call_count == 2, "second call is the per-symbol fallback for the new window"


@respx.mock
def test_one_unlisted_symbol_does_not_fail_the_whole_batch() -> None:
    """The API 422s the entire batch for one bad name — but it names the offenders."""
    occ = _occ()
    responses = [
        httpx.Response(422, json={"code": 42210000,
                                  "message": "invalid underlying symbols: EQR,SATS"}),
        httpx.Response(200, json=_bulk_body([("AAA", occ, 800)])),
    ]
    route = respx.get(CONTRACTS).mock(side_effect=responses)
    provider = AlpacaChainProvider(_settings())
    empty = provider.prefetch(["AAA", "EQR", "SATS"], _filt())
    assert route.call_count == 2, "drops the named symbols and retries once"
    assert "EQR" in empty and "SATS" in empty  # unlisted == no contracts, for our purposes


@respx.mock
def test_an_unrecoverable_422_still_raises() -> None:
    respx.get(CONTRACTS).mock(return_value=httpx.Response(422, json={"message": "nope"}))
    provider = AlpacaChainProvider(_settings())
    with pytest.raises(ProviderDataError):  # a 422 we can't attribute is a real failure
        provider.prefetch(["AAA"], _filt())


def test_alpaca_advertises_batch_support() -> None:
    assert AlpacaChainProvider(_settings()).capabilities().supports_batch_underlyings is True


# ── batched fetch ──────────────────────────────────────────────────────────────────────────
# One snapshot request per underlying made a screen entirely rate-limit-bound: 817 names cost
# 832 requests and 246s, of which 94% was sleeping on the limiter. Asking for many option
# symbols per request cut the same screen to 117 requests and 18s, picking the identical
# contract for all 314 candidates.

BULK_SNAP = "https://data.alpaca.markets/v1beta1/options/snapshots"
STOCKS = "https://data.alpaca.markets/v2/stocks/snapshots"
FILT = ChainFilter(min_dte=30, max_dte=45, min_open_interest=100)


def _occ_for(under: str, strike: int, root: str | None = None) -> str:
    exp = date.today() + timedelta(days=40)
    return f"{root or under}{exp:%y%m%d}P{strike * 1000:08d}"


def _contracts_body(*rows) -> dict:
    """rows: (underlying, occ, strike, open_interest)"""
    return {"option_contracts": [
        {"symbol": occ, "underlying_symbol": u, "strike_price": str(k), "open_interest": str(oi)}
        for u, occ, k, oi in rows], "next_page_token": None}


def _bulk_snap_body(*occs) -> dict:
    return {"snapshots": {o: {"latestQuote": {"bp": 1.40, "ap": 1.50},
                              "greeks": {"delta": -0.20},
                              "impliedVolatility": 0.345} for o in occs},
            "next_page_token": None}


@respx.mock
def test_get_chains_serves_many_names_in_three_requests() -> None:
    a, b = _occ_for("AAA", 90), _occ_for("BBB", 45)
    respx.get(STOCKS).mock(return_value=httpx.Response(200, json={
        "AAA": {"latestTrade": {"p": 100.0}}, "BBB": {"latestTrade": {"p": 50.0}}}))
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json=_contracts_body(("AAA", a, 90, 800), ("BBB", b, 45, 800))))
    snaps = respx.get(BULK_SNAP).mock(
        return_value=httpx.Response(200, json=_bulk_snap_body(a, b)))

    chains, complete = AlpacaChainProvider(_settings()).get_chains(["AAA", "BBB"], FILT)
    assert complete and set(chains) == {"AAA", "BBB"}
    assert chains["AAA"].contracts[0].strike == 90.0
    assert chains["BBB"].contracts[0].open_interest == 800
    assert len(respx.calls) == 3, "three bulk requests, not one per name"
    assert "symbols=" in str(snaps.calls.last.request.url)


@respx.mock
def test_a_name_with_no_strike_near_spot_falls_back_to_the_per_name_fetch() -> None:
    """The band is a speed heuristic, so it must never be the reason a name is dropped. A name
    whose liquid strikes all sit outside it is fetched the old way instead."""
    far = _occ_for("AAA", 30)  # 0.30 x spot — far below the 0.70 floor
    respx.get(STOCKS).mock(return_value=httpx.Response(
        200, json={"AAA": {"latestTrade": {"p": 100.0}}}))
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json=_contracts_body(("AAA", far, 30, 800))))
    bulk = respx.get(BULK_SNAP).mock(return_value=httpx.Response(200, json=_bulk_snap_body()))
    per_name = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(far)))

    chains, complete = AlpacaChainProvider(_settings()).get_chains(["AAA"], FILT)
    assert complete and per_name.called, "the clipped name must still be fetched"
    assert chains["AAA"].contracts[0].strike == 30.0
    assert not bulk.calls or "symbols=&" not in str(bulk.calls.last.request.url)


@respx.mock
def test_a_name_with_unknown_spot_falls_back_rather_than_guessing_moneyness() -> None:
    occ = _occ_for("AAA", 90)
    respx.get(STOCKS).mock(return_value=httpx.Response(200, json={}))  # no price for AAA
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json=_contracts_body(("AAA", occ, 90, 800))))
    respx.get(BULK_SNAP).mock(return_value=httpx.Response(200, json=_bulk_snap_body()))
    per_name = respx.get(SNAP).mock(return_value=httpx.Response(200, json=_snap_body(occ)))

    chains, _ = AlpacaChainProvider(_settings()).get_chains(["AAA"], FILT)
    assert per_name.called and chains["AAA"].contracts[0].strike == 90.0


@respx.mock
def test_snapshot_requests_never_exceed_the_hard_100_symbol_cap() -> None:
    """101 symbols returns 400 'symbol limit is 100'. This is an API limit, not a tuning knob."""
    # the band is 0.70-1.02 x spot, so the strikes have to be packed inside that range to
    # exercise the chunking at all -- spread them wider and the band, not the cap, does the work
    rows = [("AAA", _occ_for("AAA", k), k, 800) for k in range(7000, 7251)]  # 251, all in band
    respx.get(STOCKS).mock(return_value=httpx.Response(
        200, json={"AAA": {"latestTrade": {"p": 10000.0}}}))
    respx.get(CONTRACTS).mock(return_value=httpx.Response(200, json=_contracts_body(*rows)))
    bulk = respx.get(BULK_SNAP).mock(
        return_value=httpx.Response(200, json=_bulk_snap_body(rows[0][1])))

    AlpacaChainProvider(_settings()).get_chains(["AAA"], FILT)
    assert len(bulk.calls) == 3  # 251 symbols -> 100 + 100 + 51
    for call in bulk.calls:
        n = len(dict(httpx.URL(str(call.request.url)).params)["symbols"].split(","))
        assert n <= 100, f"sent {n} symbols in one request"


@respx.mock
def test_one_unlisted_ticker_does_not_sink_a_batch_of_spot_lookups() -> None:
    """The stock endpoint names ONE bad ticker at a time, and phrases it differently from the
    contracts endpoint ('invalid symbol' vs 'invalid underlying symbols'). Without handling
    both, a single unlisted name fails the whole 100-symbol batch."""
    occ = _occ_for("AAA", 90)
    responses = [
        httpx.Response(400, json={"message": "code=400, message=invalid symbol: BRK-B"}),
        httpx.Response(200, json={"AAA": {"latestTrade": {"p": 100.0}}}),
    ]
    respx.get(STOCKS).mock(side_effect=responses)
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json=_contracts_body(("AAA", occ, 90, 800))))
    respx.get(BULK_SNAP).mock(return_value=httpx.Response(200, json=_bulk_snap_body(occ)))

    chains, complete = AlpacaChainProvider(_settings()).get_chains(["AAA", "BRK-B"], FILT)
    assert complete and chains["AAA"].contracts[0].strike == 90.0


@respx.mock
def test_the_underlying_comes_from_the_vendor_not_from_parsing_the_occ_symbol() -> None:
    """An ADJUSTED contract (root AAA1, issued after a corporate action) has a symbol no naive
    OCC pattern matches. Parsing dropped it silently and changed which strike a real screen
    picked for one name in 817; the contracts payload already says who owns it."""
    adjusted = _occ_for("AAA", 90, root="AAA1")
    respx.get(STOCKS).mock(return_value=httpx.Response(
        200, json={"AAA": {"latestTrade": {"p": 100.0}}}))
    respx.get(CONTRACTS).mock(return_value=httpx.Response(
        200, json=_contracts_body(("AAA", adjusted, 90, 800))))
    respx.get(BULK_SNAP).mock(return_value=httpx.Response(
        200, json=_bulk_snap_body(adjusted)))

    chains, _ = AlpacaChainProvider(_settings()).get_chains(["AAA"], FILT)
    assert "AAA" in chains and chains["AAA"].contracts[0].option_symbol == adjusted


@respx.mock
def test_get_chains_stops_when_cancelled_and_reports_an_incomplete_scan() -> None:
    import threading
    occ = _occ_for("AAA", 90)
    cancel = threading.Event()
    respx.get(STOCKS).mock(return_value=httpx.Response(
        200, json={"AAA": {"latestTrade": {"p": 100.0}}}))
    respx.get(CONTRACTS).mock(side_effect=lambda req: (
        cancel.set(), httpx.Response(200, json=_contracts_body(("AAA", occ, 90, 800))))[1])

    chains, complete = AlpacaChainProvider(_settings()).get_chains(
        ["AAA"], FILT, cancel=cancel)
    assert complete is False, "a cancelled fetch must not look like a finished one"
    assert chains == {}


@respx.mock
def test_a_batched_chain_is_cached_under_its_own_key() -> None:
    """A batched chain holds only the strikes near spot. Sharing get_chain's key would hand a
    ticker search a silently truncated chain."""
    prov = AlpacaChainProvider(_settings())
    single = prov._batch_cache_key("AAA", date(2026, 9, 1), date(2026, 9, 30), OptionType.PUT)
    assert "batch" in single and "0.7-1.02" in single
