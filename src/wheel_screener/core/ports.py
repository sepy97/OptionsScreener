"""Provider ports (interfaces). The core depends only on these Protocols, never on
concrete vendors. Concrete implementations live in ``wheel_screener.adapters``.

(An ``IvRankProvider`` port was intentionally dropped for v1 — IV rank is deferred;
see docs/PLAN.md. Schwab's per-contract IV is still surfaced on OptionContract.)
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from wheel_screener.core.models import (
    ChainFilter,
    ChainSnapshot,
    FundamentalMetrics,
    FundamentalReport,
    ProviderCaps,
    ScreenCriteria,
    Underlying,
)


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Universe + fundamentals + earnings. FMP today.

    ``screen_universe`` returns the cheap price/market-cap/exchange universe;
    ``bulk_metrics`` returns cheap partial TTM metrics for many symbols (the pre-rank);
    ``fetch_metrics`` returns the deep per-name metrics incl. sign inputs + DCF;
    ``earnings_calendar`` maps symbol -> next earnings date within [start, end];
    ``next_earnings`` answers the same question for ONE symbol, authoritatively.

    Implementations of ``earnings_calendar`` MUST return a complete window or raise — a
    silently truncated calendar reads as "nobody reports soon" and disables the blackout.
    """

    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]: ...

    def bulk_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]: ...

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]: ...

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]: ...

    def next_earnings(self, symbol: str, on_or_after: date) -> date | None: ...


@runtime_checkable
class ChainProvider(Protocol):
    """Option-chain source with greeks + IV. Schwab today; others later."""

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot: ...

    def capabilities(self) -> ProviderCaps: ...


@runtime_checkable
class FundamentalReportProvider(Protocol):
    """Long-form, multi-period fundamental analysis of ONE company.

    Separate from :class:`FundamentalsProvider`, which serves the bulk metrics the screen
    ranks a whole universe on. This answers "show me this company's numbers over the last
    N periods, graded", and is backed by an external analysis engine.

    Implementations MUST raise the typed ``ProviderError`` hierarchy so the delivery layer
    can tell a missing key from an outage from an unknown ticker.
    """

    def fundamental_report(
        self, symbol: str, period: str = "annual", years: int = 10
    ) -> FundamentalReport: ...
