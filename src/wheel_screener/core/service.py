"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from wheel_screener.core.fundamentals import gate_reasons, rank_by_fundamentals
from wheel_screener.core.models import (
    CandidateResult,
    ChainFilter,
    FundamentalMetrics,
    OptionType,
    ScreenCriteria,
    Underlying,
)
from wheel_screener.core.pipeline.pull_chains import pull_chains
from wheel_screener.core.pipeline.rank import rank
from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank
from wheel_screener.core.pipeline.select_strike import (
    credited_premium,
    put_yield,
    select_put,
    select_top_puts,
)
from wheel_screener.core.pipeline.universe import build_universe
from wheel_screener.core.ports import ChainProvider, FundamentalsProvider

logger = logging.getLogger(__name__)


@dataclass
class TickerSearch:
    """Single-ticker CSP search: the top-N puts + fundamentals/earnings context."""

    symbol: str
    puts: list[CandidateResult] = field(default_factory=list)
    passes_fundamentals: bool | None = None  # None if the ticker isn't in the local store
    gate_reasons: list[str] = field(default_factory=list)
    next_earnings: date | None = None
    metrics: FundamentalMetrics | None = None  # the ticker's raw fundamentals (P/E, ROE, ...)
    fundamental_score: float | None = None  # the screener's 0-1 cross-sectional score


@dataclass
class ScreenerService:
    """Use-case entry point. Wires the pipeline over injected ports.

    Both delivery layers (CLI now, FastAPI later) call these methods — no pipeline
    logic is duplicated anywhere else.
    """

    fundamentals: FundamentalsProvider
    chains: ChainProvider
    _scores: dict[str, float] | None = field(default=None, init=False, repr=False, compare=False)

    def _universe_scores(self, criteria: ScreenCriteria, today: date) -> dict[str, float]:
        """The screener's 0-1 cross-sectional fundamental score for every gate-passing name in the
        universe. Computed once and cached (stable between fundamentals refreshes) so a single
        ticker search doesn't re-rank the market on every call."""
        if self._scores is None:
            universe = build_universe(self.fundamentals, criteria)
            metrics = self.fundamentals.fetch_metrics([u.symbol for u in universe])
            for u in universe:
                u.metrics = metrics.get(u.symbol)
            gated = [u for u in universe if not gate_reasons(u.metrics, criteria)]
            rank_by_fundamentals(gated, criteria.factor_weights, criteria.stock_profile)
            self._scores = {
                u.symbol: u.fundamental_score for u in gated if u.fundamental_score is not None
            }
            logger.info("fundamental scores computed for %d names (cached)", len(self._scores))
        return self._scores

    def _put_filter(self, criteria: ScreenCriteria) -> ChainFilter:
        # pull a padded window so monthly-only names still surface their nearest monthly
        return ChainFilter(
            option_type=OptionType.PUT,
            min_dte=max(criteria.min_dte - criteria.dte_tolerance, 1),
            max_dte=criteria.max_dte + criteria.dte_tolerance,
            min_open_interest=criteria.min_open_interest,
            target_delta=criteria.target_delta,
        )

    def _candidate(self, symbol, put, **ctx) -> CandidateResult:
        return CandidateResult(
            symbol=symbol, contract=put,
            annualized_yield=put_yield(put),
            premium=credited_premium(put),  # conservative: the bid
            collateral=put.strike * 100,
            **ctx,
        )

    def screen_fundamentals(self, criteria: ScreenCriteria, today: date) -> list[Underlying]:
        """Universe -> fundamental gate + cross-sectional rank -> ranked names."""
        universe = build_universe(self.fundamentals, criteria)
        return rate_and_rank(self.fundamentals, universe, criteria, today)

    def run_screen(
        self,
        criteria: ScreenCriteria,
        today: date,
        *,
        cancel: threading.Event | None = None,
    ) -> list[CandidateResult]:
        """Full pipeline: fundamentals -> chain pull -> ~target-delta put -> yield rank.

        Bounded by ``criteria.max_runtime_seconds`` and an optional ``cancel`` event (for a
        web layer to abort on client disconnect); both yield partial, ranked results.
        """
        survivors = self.screen_fundamentals(criteria, today)
        filt = self._put_filter(criteria)
        deadline = (
            time.monotonic() + criteria.max_runtime_seconds
            if criteria.max_runtime_seconds is not None
            else None
        )
        chains = pull_chains(self.chains, survivors, filt, deadline=deadline, cancel=cancel)

        candidates: list[CandidateResult] = []
        for u in survivors:
            snapshot = chains.get(u.symbol)
            if snapshot is None:
                continue
            put = select_put(snapshot, criteria)
            if put is None:
                continue
            candidates.append(
                self._candidate(
                    u.symbol, put, fundamental_score=u.fundamental_score,
                    next_earnings=u.next_earnings, has_weeklys=u.has_weeklys,
                )
            )

        if criteria.min_annualized_yield is not None:
            floor = criteria.min_annualized_yield
            candidates = [c for c in candidates if (c.annualized_yield or 0.0) >= floor]
        logger.info(
            "candidates: %d with a tradeable put · ranked by fundamental_weight=%.2f",
            len(candidates), criteria.fundamental_weight,
        )
        return rank(candidates, criteria.fundamental_weight)

    def search_ticker(
        self, symbol: str, criteria: ScreenCriteria, today: date, *, n: int = 5
    ) -> TickerSearch:
        """Top-N ~target-delta cash-secured puts on ONE ticker — bypasses the universe/funnel.

        One chain pull (works for any optionable symbol, even outside the screen's universe),
        the N puts nearest ``target_delta`` (one per expiry), plus fundamentals + next-earnings
        context so a put seller can judge assignment/event risk.
        """
        symbol = symbol.strip().upper()
        snapshot = self.chains.get_chain(symbol, self._put_filter(criteria))
        puts = [
            self._candidate(symbol, p) for p in select_top_puts(snapshot, criteria, n)
        ]
        # fundamentals context (the ticker may sit outside the screener's $20-200 universe)
        metrics = self.fundamentals.fetch_metrics([symbol]).get(symbol)
        if metrics is None:
            passes, reasons = None, []
        else:
            reasons = gate_reasons(metrics, criteria)
            passes = not reasons
        earnings = self.fundamentals.earnings_calendar(
            today, today + timedelta(days=criteria.max_dte)
        ).get(symbol)
        score = self._universe_scores(criteria, today).get(symbol)  # same score the screener shows
        for c in puts:
            c.next_earnings = earnings
            c.fundamental_score = score
        logger.info(
            "search %s: %d puts near Δ=%.2f (DTE %d-%d) · score=%s",
            symbol, len(puts), criteria.target_delta, criteria.min_dte, criteria.max_dte,
            "n/a" if score is None else f"{score:.2f}",
        )
        return TickerSearch(
            symbol=symbol, puts=puts, passes_fundamentals=passes, gate_reasons=reasons,
            next_earnings=earnings, metrics=metrics, fundamental_score=score,
        )
