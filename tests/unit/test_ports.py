from __future__ import annotations

from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.composition import build_service
from wheel_screener.config import FmpSettings, SchwabSettings, Settings
from wheel_screener.core.ports import ChainProvider, FundamentalsProvider
from wheel_screener.core.service import ScreenerService


def test_adapters_satisfy_ports() -> None:
    assert isinstance(FmpFundamentalsProvider(FmpSettings()), FundamentalsProvider)
    assert isinstance(SchwabChainProvider(SchwabSettings()), ChainProvider)


def test_schwab_capabilities() -> None:
    caps = SchwabChainProvider(SchwabSettings()).capabilities()
    assert caps.name == "schwab"
    assert caps.supports_batch_underlyings is False


def test_build_service_default_wiring() -> None:
    service = build_service(Settings())
    assert isinstance(service, ScreenerService)
    assert isinstance(service.fundamentals, FmpFundamentalsProvider)
    assert isinstance(service.chains, SchwabChainProvider)
