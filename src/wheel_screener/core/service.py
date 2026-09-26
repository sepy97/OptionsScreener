"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

from wheel_screener.core import exits, rollgrid
from wheel_screener.core.assignment import DEFAULT_CARRY_RATE
from wheel_screener.core.assignment import assess as assess_assignment
from wheel_screener.core.dividends import in_life, upcoming
from wheel_screener.core.earnings import EarningsGuard
from wheel_screener.core.errors import ProviderError, ProviderUnavailableError
from wheel_screener.core.fundamentals import (
    gate_reasons,
    rank_by_fundamentals,
    score_strength,
)
from wheel_screener.core.models import (
    BrokerageAccount,
    CandidateResult,
    ChainFilter,
    CompanyProfile,
    Dividend,
    EarningsPolicy,
    EarningsStatus,
    FundamentalMetrics,
    FundamentalReport,
    OptionType,
    Position,
    PositionKind,
    ScreenCriteria,
    SwapSuggestion,
    Underlying,
)
from wheel_screener.core.pipeline.pull_chains import pull_chains
from wheel_screener.core.pipeline.rank import rank
from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank
from wheel_screener.core.pipeline.select_strike import (
    contract_yield,
    credited_premium,
    select_put,
    select_top_contracts,
    signed_target_delta,
)
from wheel_screener.core.pipeline.universe import build_universe
from wheel_screener.core.ports import (  # noqa: F401 - EtfUniverseProvider is a field type
    BrokerageAccountProvider,
    ChainProvider,
    CompanyProfileProvider,
    DividendProvider,
    EtfUniverseProvider,
    FundamentalReportProvider,
    FundamentalsProvider,
)
from wheel_screener.core.swap import (
    OpenCall,
    OpenPut,
    SwapParams,
    list_median_yield,
    review_covered_call,
)
from wheel_screener.core.swap import review as swap_review

logger = logging.getLogger(__name__)

# Concurrent chain pulls for the swap review. A wheel account holds a handful of puts, so this
# is about not waiting on them one at a time rather than about throughput.
_SWAP_WORKERS = 4


def _suggestion(c: CandidateResult) -> SwapSuggestion:
    """A screen candidate (or a freshly picked put) as a swap suggestion."""
    return SwapSuggestion(
        symbol=c.symbol, strike=c.contract.strike, expiration=c.contract.expiration,
        dte=c.contract.dte, delta=c.contract.delta, bid=c.contract.bid,
        annualized_yield=c.annualized_yield, collateral=c.collateral, score=c.score,
    )


@dataclass
class TickerSearch:
    """Single-ticker search: the top-N contracts on one side + fundamentals/earnings context.

    ``side`` says which trade the rows describe — puts to open a cash-secured position, calls to
    sell against shares already held. The two are read differently (the put's base is the strike,
    the call's is the share price), so consumers must not assume puts.
    """

    symbol: str
    contracts: list[CandidateResult] = field(default_factory=list)
    side: OptionType = OptionType.PUT
    underlying_price: float | None = None  # spot used as the covered-call yield base
    passes_fundamentals: bool | None = None  # None if the ticker isn't in the local store
    gate_reasons: list[str] = field(default_factory=list)
    next_earnings: date | None = None
    earnings_known: bool = False  # False = we could not establish a date, NOT "no earnings"
    metrics: FundamentalMetrics | None = None  # the ticker's raw fundamentals (P/E, ROE, ...)
    fundamental_score: float | None = None  # absolute financial strength 0-1 (primary rating)
    peer_percentile: float | None = None  # percentile vs the screened field (None if outside it)
    # the next ex-dividend date whatever the expiries (announced, or estimated from the schedule)
    next_dividend: Dividend | None = None
    dividends_known: bool = False  # False = no lookup ran or it failed, NOT "pays no dividend"


@dataclass
class ScreenerService:
    """Use-case entry point. Wires the pipeline over injected ports.

    Both delivery layers (CLI now, FastAPI later) call these methods — no pipeline
    logic is duplicated anywhere else.
    """

    fundamentals: FundamentalsProvider
    chains: ChainProvider
    # optional: the long-form report engine ships separately and may not be installed
    reports: FundamentalReportProvider | None = None
    # optional: company identity/description. Context only — absence shows a bare ticker.
    profiles: CompanyProfileProvider | None = None
    # optional: the linked brokerage. None when no broker is connected.
    accounts: BrokerageAccountProvider | None = None
    # optional: optionable ETFs, which join the SAME screen rather than a separate one.
    # Without it the screen is stocks only, which is the pre-existing behaviour.
    etfs: EtfUniverseProvider | None = None
    # optional: dividend history for the ex-dividend flag. Without it nothing is flagged.
    dividends: DividendProvider | None = None
    # the short-term rate a put holder earns on the strike — the early-exercise test for puts
    carry_rate: float = DEFAULT_CARRY_RATE
    # limits for the put swap rule (keep or redeploy the cash behind an open put)
    swap_params: SwapParams = field(default_factory=SwapParams)
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

    def _chain_filter(
        self, criteria: ScreenCriteria, option_type: OptionType = OptionType.PUT
    ) -> ChainFilter:
        # pull a padded window so monthly-only names still surface their nearest monthly
        return ChainFilter(
            option_type=option_type,
            min_dte=max(criteria.min_dte - criteria.dte_tolerance, 1),
            max_dte=criteria.max_dte + criteria.dte_tolerance,
            min_open_interest=criteria.min_open_interest,
            target_delta=signed_target_delta(criteria.target_delta, option_type),
        )

    def _candidate(self, symbol, contract, **ctx) -> CandidateResult:
        # Capital tied up, by side: a put sets aside the strike in cash; a call pledges 100 shares
        # you already own, so it's their market value (None when spot is unknown — see _spot).
        if contract.option_type is OptionType.PUT:
            collateral = contract.strike * 100
        else:
            spot = contract.underlying_price
            collateral = spot * 100 if spot and spot > 0 else None
        return CandidateResult(
            symbol=symbol, contract=contract,
            annualized_yield=contract_yield(contract),
            premium=credited_premium(contract),  # conservative: the bid
            collateral=collateral,
            **ctx,
        )

    def _spot(self, symbol: str, snapshot, metrics: FundamentalMetrics | None) -> float | None:
        """Current share price — the covered-call yield base, in descending order of freshness.

        1. the chain snapshot, when the provider returns spot in-band (Schwab does);
        2. the provider's own quote endpoint, if it exposes one (Alpaca's chains are option-only,
           so this is one extra call — worth it on a single-ticker search, which is the only place
           calls are offered; the screener is CSP-only and needs no spot);
        3. the fundamentals store's profile price — end-of-day, so a fallback rather than a peer.

        None when all three fail: ``contract_yield`` then reports no yield instead of a wrong one.
        """
        if snapshot.underlying_price and snapshot.underlying_price > 0:
            return snapshot.underlying_price
        quote = getattr(self.chains, "spot", None)
        if quote is not None:
            try:
                live = quote(symbol)
                if live and live > 0:
                    return live
            except ProviderError:
                logger.warning("spot: quote lookup failed for %s", symbol, exc_info=True)
        eod = metrics.price if metrics is not None else None
        if eod and eod > 0:
            logger.info("spot: falling back to the EOD profile price for %s (%.2f)", symbol, eod)
            return eod
        return None

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
        survivors = survivors + self._etf_survivors(criteria)
        filt = self._chain_filter(criteria, OptionType.PUT)  # the screen is CSP-only
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
                    u.symbol, put, is_etf=u.is_etf,
                    underlying_price=snapshot.underlying_price,
                    fundamental_score=u.fundamental_score,
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
        ranked = rank(
            candidates,
            criteria.fundamental_weight,
            yield_good=criteria.yield_good,
            yield_satisfactory=criteria.yield_satisfactory,
            min_score=criteria.min_score,
        )
        # After the last filter (the score floor lives inside rank), so only the names actually
        # shown cost a lookup. A flag, not a filter: it removes nothing and reorders nothing.
        if ranked:
            histories = self._dividend_histories(sorted({c.symbol for c in ranked}))
            self._stamp_dividends(ranked, today, histories)
        return ranked

    def _tenor_matched_put(self, snapshot, criteria, guard, held_days: int):
        """The put the entry rules would open at roughly the OPEN PUT'S OWN CLOCK.

        Not the best-paying expiry in the entry window, which is what the screen picks and which
        is almost always the shortest one on offer: premium grows with the square root of time,
        so an identical delta shows a far higher ANNUAL rate over fewer days (MRVL on 21 Sep
        2026: 44%/yr at 18 days against 28%/yr at 39). Comparing across tenors therefore
        measures the calendar rather than the position, and flagged a put sold days earlier.

        So the expiry nearest the open put's remaining life is used, clamped into the entry
        window — the rules would not sell a 5-day put, so nothing is compared against one — and
        the nearest in-window expiry that actually yields a pick wins, since the held expiry may
        have nothing liquid at the target delta.
        """
        target = min(max(held_days, criteria.min_dte), criteria.max_dte)
        lo, hi = criteria.min_dte, criteria.max_dte + criteria.dte_tolerance
        options = sorted(
            {c.dte for c in snapshot.contracts if lo <= c.dte <= hi},
            key=lambda d: (abs(d - target), d),
        )
        for dte in options:
            narrowed = criteria.model_copy(
                update={"min_dte": dte, "max_dte": dte, "dte_tolerance": 0}
            )
            pick = select_put(snapshot, narrowed, guard)
            if pick is not None:
                return pick
        return None

    def _dividend_histories(self, symbols: list[str]) -> dict[str, list[Dividend]] | None:
        """Dividend histories for the names being shown, or None when there is no source or it
        failed. Never raises: a missing flag costs a line of context, and must not take a screen
        or a search down with it."""
        if self.dividends is None or not symbols:
            return None
        try:
            return self.dividends.dividend_history(symbols)
        except Exception as e:  # noqa: BLE001 - optional context, never fatal
            logger.warning("dividends: lookup failed (%s); no ex-dividend flags this time", e)
            return None

    def _stamp_dividends(
        self,
        candidates: list[CandidateResult],
        today: date,
        histories: dict[str, list[Dividend]] | None,
    ) -> None:
        """Attach the ex-dividends each candidate's contract lives through."""
        if histories is None or not candidates:
            return
        for c in candidates:
            history = histories.get(c.symbol)
            if history is None:
                continue  # couldn't be looked up: say nothing rather than "no dividend"
            c.dividends = in_life(history, today, c.contract.expiration)
            c.dividends_checked = True
        logger.info(
            "dividends: %d of %d candidate(s) live through an ex-dividend date (%d estimated)",
            sum(1 for c in candidates if c.dividends), len(candidates),
            sum(1 for c in candidates if any(d.estimated for d in c.dividends)),
        )

    def exit_options(
        self,
        symbol: str,
        strike: float,
        expiration: date,
        contracts: float,
        today: date,
        *,
        min_dte: int = 1,
        max_dte: int = 120,
        option_type: OptionType = OptionType.PUT,
        is_short: bool = True,
        collected: float | None = None,
        opened_on: date | None = None,
        roll_strike: float | None = None,
        call_strike: float | None = None,
    ):
        """Every way out of one open short put, priced and ranked.

        Returns ``(alternatives, after_assignment, roll_grid, spot, early_assignment)``. The
        requested DTE window is always widened to contain the position's own expiry — without
        that contract there is no cost to close, so the baseline "keep" row cannot be formed and
        the whole table becomes a list of alternatives to nothing.

        ``early_assignment`` is judged against the held contract's BID — what its holder could
        sell it for instead of exercising — so it is the precise version of the portfolio row's
        mark-based estimate. None for a long position, which cannot be assigned.

        Calls are only fetched when the put is in the money. Out of the money, assignment is not
        the live outcome, so offering an assign-and-write row would compare against a position
        the holder would not end up in.
        """
        symbol = symbol.strip().upper()
        held_dte = (expiration - today).days
        lo = max(1, min(min_dte, held_dte))
        hi = max(max_dte, held_dte)

        own_chain = self.chains.get_chain(symbol, ChainFilter(
            option_type=option_type, min_dte=lo, max_dte=hi, min_open_interest=0
        ))
        spot = own_chain.underlying_price
        if not spot or spot <= 0:
            quote = getattr(self.chains, "spot", None)
            spot = quote(symbol) if callable(quote) else None

        # The opposite side is only worth a request when there is an assignment to plan past:
        # a SHORT position, in the money. Long options are exercised by choice and out-of-the-
        # money ones simply expire, so a second chain pull would buy nothing.
        opposite: list = []
        if is_short and exits.is_in_the_money(strike, spot, option_type):
            other = (OptionType.CALL if option_type is OptionType.PUT else OptionType.PUT)
            opposite = self.chains.get_chain(symbol, ChainFilter(
                option_type=other, min_dte=lo, max_dte=hi, min_open_interest=0
            )).contracts

        grid = rollgrid.build(
            own_chain.contracts, strike=strike, expiration=expiration, contracts=contracts,
            spot=spot, today=today, option_type=option_type, collected=collected,
            opened_on=opened_on,
        ) if is_short else None

        rows, after = exits.compare(
            own_chain.contracts, opposite, strike=strike, expiration=expiration,
            contracts=contracts, spot=spot, today=today, option_type=option_type,
            is_short=is_short, roll_strike=roll_strike, call_strike=call_strike,
        )
        early = None
        if is_short:
            mine = next(
                (c for c in own_chain.contracts
                 if c.strike == strike and c.expiration == expiration), None,
            )
            # the holder's alternative to exercising is selling at the bid; mid if there is none
            price = None if mine is None else (mine.bid if mine.bid else mine.mid)
            histories = self._dividend_histories([symbol]) or {}
            early = assess_assignment(
                option_type, strike, spot, price, expiration, today,
                in_life(histories.get(symbol) or [], today, expiration), self.carry_rate,
            )
        logger.info(
            "exits: %s $%g %s -> %d alternative(s), %d post-assignment (spot %s) · early "
            "assignment %s",
            symbol, strike, expiration, len(rows), len(after),
            f"{spot:.2f}" if spot else "unknown", early.risk if early else "n/a",
        )
        return rows, after, grid, spot, early

    def search_ticker(
        self,
        symbol: str,
        criteria: ScreenCriteria,
        today: date,
        *,
        n: int = 5,
        side: OptionType = OptionType.PUT,
    ) -> TickerSearch:
        """Top-N ~target-delta contracts on ONE ticker — bypasses the universe/funnel.

        One chain pull (works for any optionable symbol, even outside the screen's universe), the
        N contracts nearest ``target_delta`` (one per expiry), plus fundamentals + next-earnings
        context so a seller can judge assignment/event risk.

        ``side`` picks the trade: PUT sells a cash-secured put to *enter* a position; CALL sells a
        covered call against shares already held. Search is the right (and only) home for calls —
        a covered call presupposes a specific holding, so the underlying is given, not screened.
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
            # For calls the flag reads differently and is genuinely a preference, not a veto: the
            # shares are already held, so the report's gap is taken either way. Selling through it
            # cushions a drop with premium and caps the upside on a pop — a trade-off to see, not
            # one to make for the user.
            policy=(
                EarningsPolicy.OFF
                if criteria.earnings_policy is EarningsPolicy.OFF
                else EarningsPolicy.FLAG
            ),
            exclude_unknown=False,
        )
        snapshot = self.chains.get_chain(symbol, self._chain_filter(criteria, side))
        # fundamentals context (the ticker may sit outside the screener's $20-200 universe).
        # Fetched BEFORE the contracts are built: its profile price is the last-resort spot, and a
        # covered call's yield needs a share price at construction time.
        metrics = self.fundamentals.fetch_metrics([symbol]).get(symbol)
        if metrics is None:
            passes, reasons = None, []
        else:
            reasons = gate_reasons(metrics, criteria)
            passes = not reasons
        spot = self._spot(symbol, snapshot, metrics)
        selected = select_top_contracts(snapshot, criteria, n, side, guard)
        for k in selected:
            # stamp the resolved spot so the yield/collateral math (and the CSV) has a base even
            # when the chain provider returns option-only data
            if k.underlying_price is None:
                k.underlying_price = spot
        contracts = [
            self._candidate(symbol, k, earnings_status=guard.status(symbol, k.expiration))
            for k in selected
        ]
        # absolute strength from the ticker's own metrics (works even for out-of-universe names);
        # the peer percentile needs the ranked universe, so it's None outside the screened field.
        strength, _ = score_strength(metrics, criteria.factor_weights, criteria.stock_profile)
        percentile = self._universe_scores(criteria, today).get(symbol)
        for c in contracts:
            c.next_earnings = earnings
            c.fundamental_score = strength
            c.peer_percentile = percentile
        histories = self._dividend_histories([symbol])
        self._stamp_dividends(contracts, today, histories)
        history = histories.get(symbol) if histories is not None else None
        # The next ex-date whatever the listed expiries, so the header can say "after all of
        # these" rather than nothing. A year out reaches even an annual payer's next date.
        ahead = upcoming(history or [], today, today + timedelta(days=366))
        logger.info(
            "search %s: %d %ss near Δ=%.2f (DTE %d-%d) · spot=%s · strength=%s · pct=%s · "
            "earnings=%s (%d of %d expiries span it)",
            symbol, len(contracts), side.value,
            signed_target_delta(criteria.target_delta, side),
            criteria.min_dte, criteria.max_dte,
            "unknown" if spot is None else f"{spot:.2f}",
            "n/a" if strength is None else f"{strength:.2f}",
            "n/a" if percentile is None else f"{percentile:.2f}",
            earnings or "unknown",
            sum(1 for c in contracts if c.earnings_status is EarningsStatus.SPANS), len(contracts),
        )
        return TickerSearch(
            symbol=symbol, contracts=contracts, side=side, underlying_price=spot,
            passes_fundamentals=passes, gate_reasons=reasons,
            next_earnings=earnings, earnings_known=earnings is not None, metrics=metrics,
            fundamental_score=strength, peer_percentile=percentile,
            next_dividend=ahead[0] if ahead else None, dividends_known=history is not None,
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

    def fundamental_report(
        self, symbol: str, period: str = "annual", years: int = 10
    ) -> FundamentalReport:
        """A multi-period graded fundamental analysis of one company.

        Independent of the screen: it needs no chain data, no universe and no earnings
        calendar, and works for any symbol the data provider knows.
        """
        if self.reports is None:
            raise ProviderUnavailableError(
                "fundamental reports are not configured for this deployment"
            )
        return self.reports.fundamental_report(symbol, period=period, years=years)

    def company_profile(self, symbol: str) -> CompanyProfile | None:
        """Who this ticker is, or None when the deployment can't say.

        Never raises: a missing profile costs a line of context, and must not take a page with it.
        """
        if self.profiles is None:
            return None
        try:
            return self.profiles.company_profile(symbol)
        except Exception as e:  # noqa: BLE001 - optional context, never fatal
            logger.warning("company profile unavailable for %s: %s", symbol, e)
            return None

    def brokerage_accounts(self) -> list[BrokerageAccount]:
        """Balances for every linked brokerage account.

        Raises ``ProviderUnavailableError`` when no broker is linked, rather than returning an
        empty list: "nothing connected" and "connected but you hold nothing" are different
        answers and the caller must be able to tell them apart.
        """
        if self.accounts is None:
            raise ProviderUnavailableError("no brokerage account is linked to this deployment")
        accounts = self.accounts.accounts()
        self._price_positions(accounts)
        return accounts

    def _etf_survivors(self, criteria: ScreenCriteria) -> list[Underlying]:
        """ETFs joining the same list, having skipped every fundamental stage.

        They bypass the gates and the cross-sectional rank because neither has an answer for a
        fund: there is no leverage to cap, no coverage to require and no peer set to sit in.
        They still face every CONTRACT-level test — delta, DTE, liquidity, earnings — because
        those judge the option rather than the issuer, and an ETF's chain can be as untradeable
        as a stock's.
        """
        if not criteria.include_etfs or self.etfs is None:
            return []
        try:
            return self.etfs.etf_universe(criteria)
        except ProviderError as e:
            # A screen that returns stocks is worth more than one that returns an error page.
            logger.warning("etf universe unavailable (%s); screening stocks only", e)
            return []

    def swap_reviews(
        self,
        positions: list[Position],
        candidates: Sequence[CandidateResult] | None,
        today: date,
        criteria: ScreenCriteria | None = None,
    ) -> None:
        """Stamp each open short option: puts with a keep-or-swap verdict, calls with whether
        the shares behind them are still earning (see ``core.swap``).

        One chain pull per held put does double duty: it carries the ASK that says what buying
        the put back costs, and the board the fresh put is chosen from — the one put the entry
        rules would open on that ticker today, picked by the very same ``select_put`` the screen
        uses, so the comparison is against a put this project would really sell.

        ``candidates`` is the latest screen. It supplies only the fallback median (for a ticker
        with no valid pick of its own) and the other-ticker suggestions; it never decides whether
        to swap. Nothing here raises: a verdict is worth less than the page it sits on.
        """
        criteria = criteria or ScreenCriteria()
        picks = [_suggestion(c) for c in candidates or []]
        median_yield = list_median_yield([p.annualized_yield for p in picks
                                          if p.annualized_yield is not None])
        open_puts = [
            p for p in positions
            if p.kind is PositionKind.SHORT_PUT and p.strike and p.expiration
        ]
        # Covered calls get the cheaper question — are the shares still earning? — which needs
        # no chain at all: the broker's own mark says what the call is worth. See
        # ``review_covered_call`` for why it is not the put rule with the sides swapped.
        for c in positions:
            if c.kind is PositionKind.SHORT_CALL and c.strike and c.expiration:
                c.swap = review_covered_call(
                    OpenCall(
                        symbol=c.underlying, strike=c.strike,
                        days=(c.expiration - today).days, contracts=c.quantity,
                        spot=c.underlying_price, price=c.mark,
                    ),
                    self.swap_params,
                )
        if not open_puts:
            return

        def one(p: Position) -> None:
            days = (p.expiration - today).days
            out_of_scope = days < 1 or (
                p.underlying_price is not None and p.underlying_price <= p.strike
            )
            if out_of_scope:
                # Nothing a chain could say changes an out-of-scope verdict, so it is settled
                # before spending the call. (Spot unknown is NOT out of scope: the chain pull
                # is where that price comes from.)
                p.swap = swap_review(
                    OpenPut(symbol=p.underlying, strike=p.strike, days=days,
                            contracts=p.quantity, spot=p.underlying_price, ask=None),
                    None, (), median_yield, self.swap_params,
                )
                return
            same, ask = self._fresh_put_and_ask(p, criteria, today)
            others = [
                s for s in sorted(
                    picks, key=lambda s: s.annualized_yield or 0.0, reverse=True
                ) if s.symbol != p.underlying
            ]
            p.swap = swap_review(
                OpenPut(
                    symbol=p.underlying, strike=p.strike, days=(p.expiration - today).days,
                    contracts=p.quantity, spot=p.underlying_price, ask=ask,
                ),
                same, others, median_yield, self.swap_params,
            )

        workers = min(len(open_puts), _SWAP_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, open_puts))
        logger.info(
            "swap review: %d open put(s) — %s",
            len(open_puts),
            ", ".join(f"{p.underlying} {p.swap.action.value}" for p in open_puts if p.swap),
        )

    def _fresh_put_and_ask(
        self, position: Position, criteria: ScreenCriteria, today: date
    ) -> tuple[SwapSuggestion | None, float | None]:
        """``(the fresh same-ticker pick, the open put's ask)`` from ONE chain pull.

        The window has to cover both the held expiry and the entry window, because they are
        different questions asked of the same board: what this put costs to close, and what the
        rules would open instead. The comparison is TENOR-MATCHED (see ``_tenor_matched_put``).

        Returns ``(None, ask)`` when the ticker has no valid pick today
        — it fails the fundamental gate, reports before every candidate expiry, or has nothing
        liquid enough — which is exactly when the list median stands in.
        """
        held_days = (position.expiration - today).days
        try:
            snapshot = self.chains.get_chain(position.underlying, ChainFilter(
                option_type=OptionType.PUT,
                min_dte=max(1, min(criteria.min_dte, held_days)),
                max_dte=max(criteria.max_dte + criteria.dte_tolerance, held_days),
                min_open_interest=0,  # the HELD contract must come back whatever its liquidity
                target_delta=signed_target_delta(criteria.target_delta, OptionType.PUT),
            ))
        except Exception as e:  # noqa: BLE001 - one unpriceable name must not sink the page
            logger.warning("swap review: no chain for %s (%s)", position.underlying, e)
            return None, None

        mine = next(
            (c for c in snapshot.contracts
             if c.strike == position.strike and c.expiration == position.expiration), None,
        )
        ask = mine.ask if mine is not None else None
        if position.underlying_price is None:
            position.underlying_price = snapshot.underlying_price

        # The entry rules, in the order that costs least: the fundamental gate is a local lookup,
        # the earnings date is one call, and only then is a strike chosen.
        metrics = self.fundamentals.fetch_metrics([position.underlying]).get(position.underlying)
        if metrics is None or gate_reasons(metrics, criteria):
            return None, ask
        earnings = self._symbol_earnings(position.underlying, criteria, today)
        guard = EarningsGuard(
            {position.underlying: earnings} if earnings else {}, today,
            buffer_days=criteria.earnings_buffer_days, policy=EarningsPolicy.EXCLUDE,
            # A per-symbol lookup vouches for no range, so an absent date is unknown rather than
            # clean. Excluding on that would delete the comparison for every name FMP is quiet
            # about, and the fallback median is the safer answer than no comparison at all.
            exclude_unknown=False,
        )
        pick = self._tenor_matched_put(snapshot, criteria, guard, held_days)
        if pick is None:
            return None, ask
        fresh = _suggestion(self._candidate(position.underlying, pick))
        return fresh.model_copy(update={"same_ticker": True}), ask

    def _price_positions(self, accounts: list[BrokerageAccount]) -> None:
        """Stamp each short option with its underlying's price, its ex-dividend dates and its
        early-assignment verdict — the assignment watch.

        The broker prices the CONTRACT, never the stock behind it, so "is this in the money"
        needs a quote from somewhere else. Best-effort by design: a portfolio that renders is
        worth more than one that 500s because a quote endpoint is briefly unhappy, and an unknown
        price shows an em dash rather than implying the position is safe.

        The verdict here is judged against the broker's MARK, since the page lists every position
        and a chain pull per row would be one request each. The exits panel, which pulls the
        chain anyway, judges the same question against the bid.
        """
        short = (PositionKind.SHORT_PUT, PositionKind.SHORT_CALL)
        held = [p for a in accounts for p in a.positions if p.kind in short]
        if not held:
            return
        wanted = sorted({p.underlying for p in held})
        prices: dict[str, float | None] = {}
        spot_of = getattr(self.chains, "spot", None)
        if callable(spot_of):
            for symbol in wanted:
                try:
                    prices[symbol] = spot_of(symbol)
                except Exception:  # noqa: BLE001 - a quote is never worth failing the whole page
                    logger.debug("no spot for %s; assignment watch will show unknown", symbol)
                    prices[symbol] = None
        histories = self._dividend_histories(wanted) or {}
        today = date.today()
        for p in held:
            p.underlying_price = prices.get(p.underlying)
            if p.expiration is None or p.strike is None or p.option_type is None:
                continue
            p.dividends = in_life(histories.get(p.underlying) or [], today, p.expiration)
            p.early_assignment = assess_assignment(
                p.option_type, p.strike, p.underlying_price, p.mark, p.expiration, today,
                p.dividends, self.carry_rate,
            )
