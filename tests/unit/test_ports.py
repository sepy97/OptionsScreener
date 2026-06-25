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
    service = build_service(Settings())  # default fundamentals_source == "local"
    assert isinstance(service, ScreenerService)
    assert isinstance(service.fundamentals, LocalFundamentalsProvider)
    assert isinstance(service.chains, SchwabChainProvider)


def test_build_service_live_source() -> None:
    service = build_service(Settings(fundamentals_source="live"))
    assert isinstance(service.fundamentals, FmpFundamentalsProvider)


def test_chain_source_selects_alpaca() -> None:
    assert isinstance(build_service(Settings(chain_source="alpaca")).chains, AlpacaChainProvider)
    assert isinstance(build_service(Settings()).chains, SchwabChainProvider)  # default schwab
