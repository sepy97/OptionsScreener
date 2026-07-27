"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from wheel_screener.core.earnings import EarningsGuard
from wheel_screener.core.errors import ProviderError
from wheel_screener.core.fundamentals import (
    gate_reasons,
    rank_by_fundamentals,
    score_strength,
)
from wheel_screener.core.models import (
    CandidateResult,
    ChainFilter,
    EarningsPolicy,
    EarningsStatus,
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
    earnings_known: bool = False  # False = we could not establish a date, NOT "no earnings"
    metrics: FundamentalMetrics | None = None  # the ticker's raw fundamentals (P/E, ROE, ...)
    fundamental_score: float | None = None  # absolute financial strength 0-1 (primary rating)
    peer_percentile: float | None = None  # percentile vs the screened field (None if outside it)


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
        """The 0-1 cross-sectional *peer percentile* for every gate-passing name in the universe.
        Computed once and cached (stable between fundamentals refreshes) so a single ticker search
        doesn't re-rank the market on every call. (The absolute strength rating is per-name and
        computed directly from the ticker's metrics, so it doesn't need this.)"""
        if self._scores is None:
            universe = build_universe(self.fundamentals, criteria)
            metrics = self.fundamentals.fetch_metrics([u.symbol for u in universe])
            for u in universe:
                u.metrics = metrics.get(u.symbol)
            gated = [u for u in universe if not gate_reasons(u.metrics, criteria)]
            rank_by_fundamentals(gated, criteria.factor_weights, criteria.stock_profile)
            self._scores = {
                u.symbol: u.peer_percentile for u in gated if u.peer_percentile is not None
            }
            logger.info("peer percentiles computed for %d names (cached)", len(self._scores))
        return self._scores

    def _earnings_window_end(self, criteria: ScreenCriteria, today: date) -> date:
        """The last date that can possibly matter: the furthest expiry we'd sell, plus the drift
        buffer. Nothing past it can affect a verdict, so nothing past it is worth fetching."""
        furthest_expiry = today + timedelta(days=criteria.max_dte + criteria.dte_tolerance)
        return furthest_expiry + timedelta(days=criteria.earnings_buffer_days)

    def _build_guard(
        self, criteria: ScreenCriteria, today: date, *, policy: EarningsPolicy | None = None
    ) -> EarningsGuard:
        """Load a FRESH calendar covering exactly the contracts' window, and wrap it in the guard.

        Two properties make this narrow sweep sufficient, and both are load-bearing:

        * it is **verified complete** over the range (the adapter asserts business-day coverage
          and raises otherwise), so a symbol's absence positively means "does not report before
          your expiry" — no per-symbol follow-up needed, and no guessing;
        * it is **re-fetched per request**, not read from a nightly snapshot: dates get confirmed
          and moved daily, and a stale calendar fails silently, since every symbol it lost reads
          downstream as "no earnings scheduled". The adapter bypasses its HTTP cache here.
        """
        policy = policy or criteria.earnings_policy
        if policy is EarningsPolicy.OFF:
            return EarningsGuard({}, today, policy=policy, exclude_unknown=False)
        end = self._earnings_window_end(criteria, today)
        dates = self.fundamentals.earnings_calendar(today, end)
        logger.info(
            "earnings calendar refreshed: %d reporters between %s and %s (verified complete)",
            len(dates), today, end,
        )
        return EarningsGuard(
            dates,
            today,
            buffer_days=criteria.earnings_buffer_days,
            policy=policy,
            exclude_unknown=criteria.exclude_unknown_earnings,
            covers_through=end,
        )

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

    def screen_fundamentals(
        self, criteria: ScreenCriteria, today: date, guard: EarningsGuard | None = None
    ) -> list[Underlying]:
        """Universe -> fundamental gate + cross-sectional rank -> ranked names."""
        universe = build_universe(self.fundamentals, criteria)
        guard = guard or self._build_guard(criteria, today)
        return rate_and_rank(self.fundamentals, universe, criteria, today, guard)

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
        guard = self._build_guard(criteria, today)
        survivors = self.screen_fundamentals(criteria, today, guard)
        filt = self._put_filter(criteria)
        deadline = (
            time.monotonic() + criteria.max_runtime_seconds
            if criteria.max_runtime_seconds is not None
            else None
        )
        chains, complete = pull_chains(
            self.chains, survivors, filt, deadline=deadline, cancel=cancel
        )
        if not complete:
            logger.warning(
                "screen returned PARTIAL results — the chain pull was cut short (timeout/cancel); "
                "some qualifying names may be missing"
            )

        candidates: list[CandidateResult] = []
        for u in survivors:
            snapshot = chains.get(u.symbol)
            if snapshot is None:
                continue
            put = select_put(snapshot, criteria, guard)
            if put is None:
                continue
            candidates.append(
                self._candidate(
                    u.symbol, put, fundamental_score=u.fundamental_score,
                    peer_percentile=u.peer_percentile,
                    next_earnings=u.next_earnings, has_weeklys=u.has_weeklys,
                    earnings_status=guard.status(u.symbol, put.expiration),
                )
            )

        if criteria.min_annualized_yield is not None:
            floor = criteria.min_annualized_yield
            candidates = [c for c in candidates if (c.annualized_yield or 0.0) >= floor]
        # last line of defense: nothing whose life spans a report may reach the results table,
        # whatever happened upstream. Under EXCLUDE this should already be empty — if it ever
        # isn't, that is a bug worth shouting about rather than shipping to the user.
        if criteria.earnings_policy is EarningsPolicy.EXCLUDE:
            leaked = [c for c in candidates if c.earnings_status is EarningsStatus.SPANS]
            if leaked:
                logger.error(
                    "earnings filter leaked %d candidate(s) — dropping: %s",
                    len(leaked), ", ".join(f"{c.symbol}@{c.contract.expiration}" for c in leaked),
                )
                candidates = [
                    c for c in candidates if c.earnings_status is not EarningsStatus.SPANS
                ]
        logger.info(
            "candidates: %d with a tradeable put (%d earnings-clean, %d unknown) · "
            "ranked by fundamental_weight=%.2f",
            len(candidates),
            sum(1 for c in candidates if c.earnings_status is EarningsStatus.CLEAN),
            sum(1 for c in candidates if c.earnings_status is EarningsStatus.UNKNOWN),
            criteria.fundamental_weight,
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
        # One authoritative per-symbol call, refreshed on every search — far cheaper and more
        # accurate than sweeping the whole market's calendar to look up a single ticker (which is
        # also what exposed this path to the calendar's near-term clipping). No coverage range is
        # claimed: this endpoint answers for one symbol, so silence means unknown, not clean.
        earnings = self._symbol_earnings(symbol, criteria, today)
        guard = EarningsGuard(
            {symbol: earnings} if earnings else {},
            today,
            buffer_days=criteria.earnings_buffer_days,
            # FLAG, not EXCLUDE: someone who typed a ticker should see its full term structure
            # with the risky expiries marked — silently returning fewer rows would read as
            # "no contracts available" and hide the very thing they need to see.
            policy=(
                EarningsPolicy.OFF
                if criteria.earnings_policy is EarningsPolicy.OFF
                else EarningsPolicy.FLAG
            ),
            exclude_unknown=False,
        )
        snapshot = self.chains.get_chain(symbol, self._put_filter(criteria))
        puts = [
            self._candidate(symbol, p, earnings_status=guard.status(symbol, p.expiration))
            for p in select_top_puts(snapshot, criteria, n, guard)
        ]
        # fundamentals context (the ticker may sit outside the screener's $20-200 universe)
        metrics = self.fundamentals.fetch_metrics([symbol]).get(symbol)
        if metrics is None:
            passes, reasons = None, []
        else:
            reasons = gate_reasons(metrics, criteria)
            passes = not reasons
        # absolute strength from the ticker's own metrics (works even for out-of-universe names);
        # the peer percentile needs the ranked universe, so it's None outside the screened field.
        strength, _ = score_strength(metrics, criteria.factor_weights, criteria.stock_profile)
        percentile = self._universe_scores(criteria, today).get(symbol)
        for c in puts:
            c.next_earnings = earnings
            c.fundamental_score = strength
            c.peer_percentile = percentile
        logger.info(
            "search %s: %d puts near Δ=%.2f (DTE %d-%d) · strength=%s · pct=%s · earnings=%s "
            "(%d of %d expiries span it)",
            symbol, len(puts), criteria.target_delta, criteria.min_dte, criteria.max_dte,
            "n/a" if strength is None else f"{strength:.2f}",
            "n/a" if percentile is None else f"{percentile:.2f}",
            earnings or "unknown",
            sum(1 for c in puts if c.earnings_status is EarningsStatus.SPANS), len(puts),
        )
        return TickerSearch(
            symbol=symbol, puts=puts, passes_fundamentals=passes, gate_reasons=reasons,
            next_earnings=earnings, earnings_known=earnings is not None, metrics=metrics,
            fundamental_score=strength, peer_percentile=percentile,
        )

    def _symbol_earnings(
        self, symbol: str, criteria: ScreenCriteria, today: date
    ) -> date | None:
        """Next report for one ticker. Prefers the per-symbol endpoint; falls back to a calendar
        window so a provider without one (or an offline local store) still answers."""
        if criteria.earnings_policy is EarningsPolicy.OFF:
            return None
        lookup = getattr(self.fundamentals, "next_earnings", None)
        if lookup is not None:
            try:
                return lookup(symbol, today)
            except ProviderError:
                logger.warning("earnings: per-symbol lookup failed for %s", symbol, exc_info=True)
        try:
            return self.fundamentals.earnings_calendar(
                today, self._earnings_window_end(criteria, today)
            ).get(symbol)
        except ProviderError:
            logger.warning("earnings: calendar unavailable for %s", symbol, exc_info=True)
            return None
