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
    ProviderCaps,
    ScreenCriteria,
    Underlying,
)


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Universe + fundamentals + earnings. FMP today.

    ``screen_universe`` returns the cheap price/market-cap/exchange universe;
    ``fetch_metrics`` returns fundamentals (adapter decides bulk vs per-symbol);
    ``earnings_calendar`` maps symbol -> next earnings date within [start, end].
    """

    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]: ...

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]: ...

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]: ...


@runtime_checkable
class ChainProvider(Protocol):
    """Option-chain source with greeks + IV. Schwab today; others later."""

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot: ...

    def capabilities(self) -> ProviderCaps: ...
