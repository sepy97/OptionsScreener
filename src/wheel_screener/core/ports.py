"""Provider ports (interfaces). The core depends only on these Protocols, never on
concrete vendors. Concrete implementations live in ``wheel_screener.adapters``.

(An ``IvRankProvider`` port was intentionally dropped for v1 — IV rank is deferred;
see docs/PLAN.md. Schwab's per-contract IV is still surfaced on OptionContract.)
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Protocol, runtime_checkable

from wheel_screener.core.models import (
    BrokerageAccount,
    BrokerLinkStatus,
    ChainFilter,
    ChainSnapshot,
    CompanyProfile,
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
class BatchChainProvider(Protocol):
    """A chain source that can serve MANY underlyings in a few requests.

    Optional, like the profile and report ports: a provider without it is fetched one name at a
    time and nothing else changes. It exists because the per-name shape is what makes a screen
    slow — the cost is requests, not bytes, and a vendor that accepts a list of symbols can
    answer for hundreds of names in the request budget one name used to spend.

    Returns ``(chains, complete)`` with the same meaning as ``pull_chains``: ``complete`` is
    False when ``cancel`` or ``deadline`` cut the fetch short, so partial results are never
    mistaken for a finished scan.
    """

    def get_chains(
        self,
        symbols: list[str],
        filt: ChainFilter,
        *,
        cancel: threading.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[dict[str, ChainSnapshot], bool]: ...


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


@runtime_checkable
class CompanyProfileProvider(Protocol):
    """Company identity and description for one symbol.

    Separate from :class:`FundamentalsProvider` because it is optional context, not screening
    input: a deployment without it simply shows a bare ticker, and nothing else degrades.
    """

    def company_profile(self, symbol: str) -> CompanyProfile | None: ...


@runtime_checkable
class BrokerageAccountProvider(Protocol):
    """Read-only view of the linked brokerage accounts.

    Deliberately separate from the *linking* port (establishing credentials): a broker whose
    credentials are configured server-side can implement this and never authenticate a human,
    while an OAuth broker implements both. Read-only by construction — no order ever crosses it.
    """

    def accounts(self) -> list[BrokerageAccount]: ...


@runtime_checkable
class OAuthBrokerLink(Protocol):
    """Establishing and ending a link with a broker that authenticates a human.

    Only brokers with a user-facing authorization flow implement this. A broker whose credentials
    are configured server-side (an API key in the environment) provides
    :class:`BrokerageAccountProvider` and nothing here — nobody authenticates, so it can neither
    prove who is asking nor mint a session.
    """

    broker: str

    def authorize_url(self, state: str) -> str: ...

    def complete(self, received_url: str, state: str) -> BrokerLinkStatus: ...

    def status(self) -> BrokerLinkStatus: ...

    def revoke(self) -> None: ...
