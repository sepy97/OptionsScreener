from __future__ import annotations

from wheel_screener.adapters.alpaca.provider import AlpacaChainProvider
from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.adapters.local.provider import LocalFundamentalsProvider
from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.composition import build_service
from wheel_screener.config import AlpacaSettings, FmpSettings, SchwabSettings, Settings
from wheel_screener.core.ports import ChainProvider, FundamentalsProvider
from wheel_screener.core.service import ScreenerService


def test_adapters_satisfy_ports() -> None:
    assert isinstance(FmpFundamentalsProvider(FmpSettings()), FundamentalsProvider)
    assert isinstance(LocalFundamentalsProvider("data/fundamentals"), FundamentalsProvider)
    assert isinstance(SchwabChainProvider(SchwabSettings()), ChainProvider)
    assert isinstance(AlpacaChainProvider(AlpacaSettings()), ChainProvider)


def test_schwab_capabilities() -> None:
    caps = SchwabChainProvider(SchwabSettings()).capabilities()
    assert caps.name == "schwab"
    assert caps.supports_batch_underlyings is False


def test_schwab_concurrency_is_configurable() -> None:
    caps = SchwabChainProvider(SchwabSettings(max_concurrency=5)).capabilities()
    assert caps.max_concurrency == 5  # pull_chains uses this to size its thread pool


def test_build_service_local_is_default() -> None:
    # explicit chain_source so an ambient .env (e.g. CHAIN_SOURCE=alpaca) can't sway the test
    service = build_service(Settings(chain_source="schwab"))  # default fundamentals_source=local
    assert isinstance(service, ScreenerService)
    assert isinstance(service.fundamentals, LocalFundamentalsProvider)
    assert isinstance(service.chains, SchwabChainProvider)


def test_build_service_live_source() -> None:
    service = build_service(Settings(fundamentals_source="live"))
    assert isinstance(service.fundamentals, FmpFundamentalsProvider)


def test_chain_source_selects_alpaca() -> None:
    assert isinstance(build_service(Settings(chain_source="alpaca")).chains, AlpacaChainProvider)
    assert isinstance(build_service(Settings(chain_source="schwab")).chains, SchwabChainProvider)


def test_dividend_source_is_fmp_when_keyed_and_absent_otherwise() -> None:
    """The bulk store holds no dividend dates, so the local source borrows live FMP for them —
    and with no key the flag is simply off rather than pretending there are no dividends."""
    from pydantic import SecretStr

    from wheel_screener.core.ports import DividendProvider

    assert isinstance(FmpFundamentalsProvider(FmpSettings()), DividendProvider)
    keyed = build_service(Settings(chain_source="schwab", fmp=FmpSettings(api_key=SecretStr("k"))))
    assert isinstance(keyed.dividends, FmpFundamentalsProvider)
    keyless = build_service(Settings(chain_source="schwab", fmp=FmpSettings()))
    assert keyless.dividends is None
    live = build_service(Settings(fundamentals_source="live", chain_source="schwab"))
    assert live.dividends is live.fundamentals  # the live source answers for itself


def test_live_prices_come_from_alpaca_for_the_local_store_only() -> None:
    from pydantic import SecretStr

    from wheel_screener.core.ports import QuoteProvider

    keys = {"fmp": FmpSettings(api_key=SecretStr("k")),
            "alpaca": AlpacaSettings(api_key=SecretStr("a"), api_secret=SecretStr("s"))}
    local = build_service(Settings(chain_source="schwab", **keys))
    assert isinstance(local.quotes, QuoteProvider) and local.quotes is local.etfs
    live = build_service(Settings(chain_source="schwab", fundamentals_source="live", **keys))
    assert live.quotes is None  # FMP's screener prices its own rows
    assert build_service(Settings(chain_source="schwab", fmp=FmpSettings())).quotes is None


def test_live_prices_are_read_from_the_stock_snapshots() -> None:
    import httpx

    from wheel_screener.adapters.etf.universe import EtfUniverseProvider

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/stocks/snapshots"
        asked = request.url.params["symbols"].split(",")
        assert asked == ["FCX", "ZZZ"]  # each name once
        return httpx.Response(200, json={
            "FCX": {"latestTrade": {"p": 46.78}, "dailyBar": {"c": 46.5, "v": 1000}},
            "ZZZ": {"latestTrade": {}, "dailyBar": {}},  # nothing to price it by
        })

    quotes = EtfUniverseProvider(FmpSettings(), AlpacaSettings(chain_cache_enabled=False),
                                 client=httpx.Client(transport=httpx.MockTransport(answer)))
    assert quotes.last_prices(["ZZZ", "FCX", "FCX"]) == {"FCX": 46.78}
