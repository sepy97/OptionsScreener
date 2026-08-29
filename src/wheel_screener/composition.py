"""Composition root — the one place concrete adapters are wired to the service.

Swapping a provider is a one-line change here; tests inject fakes instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wheel_screener.adapters.alpaca.provider import AlpacaChainProvider
from wheel_screener.adapters.fmp.provider import FmpFundamentalsProvider
from wheel_screener.adapters.fundcore.provider import FundcoreReportProvider
from wheel_screener.adapters.local.earnings import LocalEarningsCalendar
from wheel_screener.adapters.local.provider import LocalFundamentalsProvider
from wheel_screener.adapters.schwab.provider import SchwabChainProvider
from wheel_screener.config import Settings
from wheel_screener.core.ports import (
    ChainProvider,
    FundamentalReportProvider,
    FundamentalsProvider,
)
from wheel_screener.core.service import ScreenerService


@dataclass(frozen=True)
class Probe:
    """One credentialed connection, and how to ask whether it works."""

    role: str  # what breaks when it's down, in the user's terms
    name: str  # the vendor
    provider: object  # anything exposing check_auth() -> str | None


def _build_chains(settings: Settings) -> ChainProvider:
    if settings.chain_source == "alpaca":
        return AlpacaChainProvider(settings.alpaca)
    return SchwabChainProvider(settings.schwab)


def _build_fundamentals(settings: Settings) -> FundamentalsProvider:
    if settings.fundamentals_source == "local":
        # Earnings isn't in the bulk store. Prefer LIVE FMP: the calendar is refreshed on every
        # request, and dates get confirmed/moved daily — a nightly CSV snapshot silently goes
        # stale, and every symbol it misses reads downstream as "no earnings" (issue #113).
        # The local CSV remains the offline fallback (no key); it self-checks its own coverage.
        if settings.fmp.api_key.get_secret_value():
            earnings = FmpFundamentalsProvider(settings.fmp)
        elif Path(settings.earnings_path).expanduser().exists():
            earnings = LocalEarningsCalendar(settings.earnings_path)
        else:
            earnings = None
        return LocalFundamentalsProvider(settings.data_dir, earnings_provider=earnings)
    return FmpFundamentalsProvider(settings.fmp)


def _build_reports(settings: Settings) -> FundamentalReportProvider | None:
    """The long-form report provider, or None when this deployment can't serve reports.

    The engine reuses the FMP key, so no key means no reports. Whether the engine PACKAGE is
    installed is decided later, inside the provider: that way "not deployed here" surfaces as
    a typed provider error the UI can explain, rather than silently missing functionality.
    """
    key = settings.fmp.api_key.get_secret_value()
    if not key:
        return None
    return FundcoreReportProvider(key, settings.fundcore)


def build_service(settings: Settings | None = None) -> ScreenerService:
    settings = settings or Settings()
    return ScreenerService(
        fundamentals=_build_fundamentals(settings),
        chains=_build_chains(settings),
        reports=_build_reports(settings),
    )


def build_probes(settings: Settings, service: ScreenerService) -> list[Probe]:
    """Every connection that needs a credential, so health and `doctor` agree on the list.

    Built once and reused: a probe holds an HTTP client, so constructing one per health poll
    would leak connections.
    """
    probes = [Probe("option chains", settings.chain_source, service.chains)]
    if settings.fmp.api_key.get_secret_value():
        probes.append(
            Probe("fundamentals & earnings", "fmp", FmpFundamentalsProvider(settings.fmp))
        )
    if service.reports is not None:
        probes.append(Probe("fundamental reports", "fundcore", service.reports))
    return probes
