from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.core.models import (
    CandidateResult,
    ChainSnapshot,
    EarningsStatus,
    FundamentalMetrics,
    OptionContract,
    OptionType,
    ProviderCaps,
    ScreenCriteria,
    Underlying,
)
from wheel_screener.core.pipeline.rank import rank
from wheel_screener.core.pipeline.select_strike import select_put, select_top_contracts
from wheel_screener.core.service import ScreenerService

_BASE = date(2026, 6, 22)


def _put(strike, delta, dte, bid, oi=500, spread=0.02, iv=None, vol=50, bid_size=100):
    return OptionContract(
        underlying_symbol="AAA",
        option_symbol=f"AAA-{dte}-{int(strike)}",
        option_type=OptionType.PUT,
        expiration=_BASE + timedelta(days=dte),  # distinct expiry per DTE
        strike=strike,
        dte=dte,
        delta=delta,
        bid=bid,
        ask=round(bid * (1 + spread), 4),
        open_interest=oi,
        volume=vol,
        bid_size=bid_size,
        implied_volatility=iv,
    )


def _call(strike, delta, dte, bid, oi=500, spread=0.02, iv=None, vol=50, bid_size=100):
    return OptionContract(
        underlying_symbol="AAA",
        option_symbol=f"AAA-C-{dte}-{int(strike)}",
        option_type=OptionType.CALL,
        expiration=_BASE + timedelta(days=dte),
        strike=strike,
        dte=dte,
        delta=delta,  # calls carry POSITIVE delta
        bid=bid,
        ask=round(bid * (1 + spread), 4),
        open_interest=oi,
        volume=vol,
        bid_size=bid_size,
        implied_volatility=iv,
    )


def _chain(contracts, underlying_price=None):
    return ChainSnapshot(
        underlying_symbol="AAA", underlying_price=underlying_price, contracts=contracts
    )


def test_select_put_picks_best_yield_near_target_delta():
    chain = _chain([
        _put(95, -0.10, 35, 1.0), _put(90, -0.20, 35, 1.5), _put(85, -0.30, 35, 2.2),
        _put(90, -0.20, 40, 1.9),  # same delta, higher annualized yield than the 35-DTE
    ])
    put = select_put(chain, ScreenCriteria(min_dte=30, max_dte=45))  # explicit window (35 & 40 in)
    assert put is not None
    assert put.strike == 90 and put.dte == 40


def test_select_put_applies_gates():
    crit = ScreenCriteria(min_dte=30, max_dte=45)  # defaults: OI 100, vol 1, bid size 10
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, oi=99)]), crit) is None      # low OI
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, oi=100)]), crit) is not None  # on it
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, vol=0)]), crit) is None      # never traded
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, bid_size=9)]), crit) is None  # no depth
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, bid_size=10)]), crit) is not None
    assert select_put(_chain([_put(90, -0.40, 40, 1.5)]), crit) is None  # |delta|>0.30
    assert select_put(_chain([_put(90, -0.20, 10, 1.5)]), crit) is None  # DTE below window
    assert select_put(_chain([_put(90, -0.20, 40, 0.0)]), crit) is None  # bid 0 = unsellable


def test_a_spread_you_could_not_trade_inside_is_rejected_again() -> None:
    """The cap was removed once on the evidence that switching it off changed a screen by ONE
    name — which measured candidate count, not fill quality, and those are different questions.
    Measured properly, half an unfiltered screen carries a spread no seller could work inside:
    one had a $1.96 bid against an $8.18 ask."""
    crit = ScreenCriteria(min_dte=30, max_dte=45)
    ok = _put(90, -0.20, 40, 1.50, spread=0.20)          # 20% of mid
    assert select_put(_chain([ok]), crit) is not None
    wide = _put(90, -0.20, 40, 1.50, spread=3.0)         # bid 1.50 against ask 6.00
    assert select_put(_chain([wide]), crit) is None


def test_a_penny_wide_market_on_a_cheap_contract_is_not_punished() -> None:
    """Percentage spread scales inversely with premium, and this strategy sells cheap far-OTM
    puts. A 1c spread on a 20c contract reads as 5% of mid... but a 5c one reads as 22%, which
    is tradeable and would fail the percentage test. Rejecting those is exactly what made the
    first version of this cap look useless."""
    crit = ScreenCriteria(min_dte=30, max_dte=45)
    cheap = _put(90, -0.20, 40, 0.20, spread=0.25)       # 20c bid, 25c ask -> 22% of mid
    assert (cheap.ask - cheap.bid) <= crit.spread_abs_exempt
    assert select_put(_chain([cheap]), crit) is not None, "5c wide is tradeable at any price"
    # ...and the exemption is absolute, so a wide gap on a cheap contract still fails
    gappy = _put(90, -0.20, 40, 0.20, spread=2.0)        # 20c bid, 60c ask
    assert select_put(_chain([gappy]), crit) is None


def test_select_put_min_iv_floor():
    # min_iv is off by default; when set, a put needs a known IV at or above the floor.
    base = _put(90, -0.20, 40, 1.5, iv=0.45)
    win = ScreenCriteria(min_dte=30, max_dte=45)
    lo = win.model_copy(update={"min_iv": 0.40})
    hi = win.model_copy(update={"min_iv": 0.60})
    assert select_put(_chain([base]), win) is not None   # floor off -> keep
    assert select_put(_chain([base]), lo) is not None    # IV 0.45 >= 0.40 floor -> keep
    assert select_put(_chain([base]), hi) is None        # IV 0.45 < 0.60 floor -> drop
    no_iv = _put(90, -0.20, 40, 1.5, iv=None)
    assert select_put(_chain([no_iv]), lo) is None       # unknown IV drops when a floor is set


def test_select_put_dte_is_strict_by_default_tolerance_is_opt_in():
    crit = ScreenCriteria(min_dte=30, max_dte=45)  # explicit window, strict (dte_tolerance 0)
    # in-band (35 DTE) wins over an out-of-band 25-DTE even with a richer raw yield
    both = _chain([_put(90, -0.20, 25, 3.0), _put(90, -0.20, 35, 1.0)])
    assert select_put(both, crit).dte == 35
    # monthly-only, nothing in 30-45: strict returns nothing (issue #26)...
    only25 = _chain([_put(90, -0.20, 25, 1.5)])
    assert select_put(only25, crit) is None
    # ...unless tolerance is opted in, which then admits the nearest expiry within ±tol
    assert select_put(only25, ScreenCriteria(min_dte=30, max_dte=45, dte_tolerance=10)).dte == 25


def test_rank_equal_fundamentals_orders_by_yield():
    a = CandidateResult(symbol="A", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.5, annualized_yield=0.10)
    b = CandidateResult(symbol="B", contract=_put(90, -0.2, 40, 2.0),
                        fundamental_score=0.5, annualized_yield=0.25)
    assert [c.symbol for c in rank([a, b])] == ["B", "A"]  # equal fundamentals -> yield decides


def test_rank_blends_fundamentals_and_yield_by_weight():
    # X: strong fundamentals, low yield.  Y: weak fundamentals, high yield.
    x = CandidateResult(symbol="X", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.9, annualized_yield=0.10)
    y = CandidateResult(symbol="Y", contract=_put(90, -0.2, 40, 2.0),
                        fundamental_score=0.2, annualized_yield=0.30)
    assert rank([x, y], fundamental_weight=0.8)[0].symbol == "X"  # quality-weighted
    assert rank([x, y], fundamental_weight=0.2)[0].symbol == "Y"  # yield-weighted


def test_score_is_absolute_and_identical_across_runs() -> None:
    """The headline property: a contract scores the same whatever it was screened alongside.

    Under the old within-run percentile the same candidate moved with its cohort, so scores
    could not be compared between runs and a threshold on one filtered nothing.
    """
    def cand(sym, strength, yld):
        return CandidateResult(symbol=sym, contract=_put(90, -0.2, 40, 1.0),
                               fundamental_score=strength, annualized_yield=yld)

    alone = rank([cand("A", 0.80, 0.20)])[0].score
    crowded = [c for c in rank([cand("A", 0.80, 0.20)] + [
        cand(f"F{i}", 0.9, 0.30) for i in range(9)
    ]) if c.symbol == "A"][0].score
    assert alone == crowded, "score must not depend on the size or quality of the field"


def test_small_quality_gaps_stay_small() -> None:
    a = CandidateResult(symbol="A", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.80, annualized_yield=0.20)
    b = CandidateResult(symbol="B", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.79, annualized_yield=0.20)
    ranked = rank([a, b], fundamental_weight=0.5)
    assert ranked[0].symbol == "A"  # the 0.01-stronger name edges ahead
    assert abs(ranked[0].score - ranked[1].score) < 0.01  # by a hair, not a cohort-sized gap


def test_geometric_mean_refuses_to_average_away_a_weak_half() -> None:
    """A weighted SUM lets a poor company buy its way up on premium alone. These two are tied
    under a sum (0.60 each); the balanced one must win."""
    lopsided = CandidateResult(symbol="LOP", contract=_put(90, -0.2, 40, 1.0),
                               fundamental_score=0.90, annualized_yield=0.09)  # rating 0.30
    balanced = CandidateResult(symbol="BAL", contract=_put(90, -0.2, 40, 1.0),
                               fundamental_score=0.60, annualized_yield=0.17)  # rating 0.60
    ranked = rank([lopsided, balanced], fundamental_weight=0.5)
    assert ranked[0].symbol == "BAL"


def test_unknown_strength_is_judged_on_yield_not_zeroed() -> None:
    """Under a geometric mean, treating unknown fundamentals as 0.0 would drive the score to
    zero and delete the name — a far stronger claim than "we have no data" supports."""
    unknown = CandidateResult(symbol="U", contract=_put(90, -0.2, 40, 1.0),
                              fundamental_score=None, annualized_yield=0.25)
    rated_zero = CandidateResult(symbol="Z", contract=_put(90, -0.2, 40, 1.0),
                                 fundamental_score=0.0, annualized_yield=0.25)
    ranked = rank([unknown, rated_zero], fundamental_weight=0.5)
    assert ranked[0].symbol == "U" and ranked[0].score == 1.0
    assert ranked[1].score == 0.0, "a name we DID rate at zero is a different statement"


def test_min_score_filters_the_shortlist() -> None:
    good = CandidateResult(symbol="G", contract=_put(90, -0.2, 40, 1.0),
                           fundamental_score=0.9, annualized_yield=0.30)
    weak = CandidateResult(symbol="W", contract=_put(90, -0.2, 40, 1.0),
                           fundamental_score=0.3, annualized_yield=0.05)
    assert [c.symbol for c in rank([good, weak], min_score=0.5)] == ["G"]
    assert len(rank([good, weak])) == 2  # off by default


def test_yield_is_graded_against_fixed_bars() -> None:
    from wheel_screener.core.pipeline.rank import yield_rating

    assert yield_rating(0.30) == 1.0  # at or above `good` tops out
    assert yield_rating(0.25) == 1.0
    assert yield_rating(0.15) == 0.5  # the `satisfactory` bar
    assert yield_rating(0.0) == 0.0 and yield_rating(None) == 0.0
    assert yield_rating(-0.1) == 0.0  # a negative yield is not "cheap"
    assert 0.5 < yield_rating(0.20) < 1.0  # straight line between the bars


def _good() -> FundamentalMetrics:
    return FundamentalMetrics(
        pe=10, ps=1, pb=1, roe=0.25, roa=0.12, ros=0.12, roi=0.25,
        debt_to_equity=0.3, net_debt_to_ebitda=0.5, ebitda=100.0,
        current_ratio=1.5, quick_ratio=1.0, cash_ratio=0.6, eps=5.0, total_equity=1000.0,
    )


class _FakeFundamentals:
    """``earnings`` maps symbol -> next report. Note the default is a date far past any test's
    DTE window, NOT an empty calendar: an empty calendar now means "unknown", which is excluded
    by default (fail-closed), so a fake that returns {} would filter every candidate away."""

    def __init__(self, earnings: dict[str, date] | None = None) -> None:
        self.earnings = {"AAA": date(2030, 1, 1)} if earnings is None else earnings
        self.symbol_lookups: list[str] = []

    def screen_universe(self, criteria):
        return [Underlying(symbol="AAA", sector="Technology", market_cap=5e9)]

    def bulk_metrics(self, symbols):
        return {"AAA": _good()}

    def fetch_metrics(self, symbols):
        return {"AAA": _good()}

    def earnings_calendar(self, start, end):
        return {s: d for s, d in self.earnings.items() if start <= d <= end}

    def next_earnings(self, symbol, on_or_after):
        self.symbol_lookups.append(symbol)
        when = self.earnings.get(symbol)
        return when if when is not None and when >= on_or_after else None


class _FakeChains:
    """Honours ``filt.option_type`` the way a real provider does — a fake that returned every
    contract regardless of side would hide a caller asking for the wrong one."""

    def __init__(self, chain, spot: float | None = None):
        self._chain = chain
        self._spot = spot
        self.requested_types: list[OptionType] = []

    def get_chain(self, symbol, filt):
        self.requested_types.append(filt.option_type)
        return ChainSnapshot(
            underlying_symbol=self._chain.underlying_symbol,
            underlying_price=self._chain.underlying_price,
            contracts=[c for c in self._chain.contracts if c.option_type is filt.option_type],
        )

    def capabilities(self):
        return ProviderCaps(name="fake")


class _QuotingChains(_FakeChains):
    """A provider whose chains are option-only (like Alpaca) but which can quote the underlying."""

    def spot(self, symbol):
        return self._spot


def test_select_top_contracts_nearest_target_one_per_expiry():
    crit = ScreenCriteria(min_dte=7, max_dte=45)  # target delta -0.20
    chain = _chain([
        _put(90, -0.20, 14, 1.0), _put(88, -0.30, 14, 1.5),  # 14 DTE: -0.20 is nearest target
        _put(85, -0.19, 28, 1.2), _put(80, -0.28, 28, 2.0),  # 28 DTE: -0.19 nearest
        _put(75, -0.05, 40, 0.3),                             # 40 DTE: -0.05 (far from target)
    ])
    top = select_top_contracts(chain, crit, 2)
    assert [c.dte for c in top] == [14, 28]  # the 2 nearest-target expiries, earliest first
    assert [c.delta for c in top] == [-0.20, -0.19]  # one per expiry, nearest -0.20
    assert len(select_top_contracts(chain, crit, 5)) == 3  # only 3 expiries available


def test_search_ticker_returns_top_puts_with_context():
    chain = _chain([_put(90, -0.20, 14, 1.0), _put(85, -0.19, 28, 1.2), _put(75, -0.05, 40, 0.3)])
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    r = service.search_ticker("aaa", ScreenCriteria(min_dte=7, max_dte=45), date(2026, 6, 22), n=2)
    assert r.symbol == "AAA"  # normalized to upper
    assert [c.contract.dte for c in r.contracts] == [14, 28]
    assert all(c.symbol == "AAA" for c in r.contracts)
    assert r.passes_fundamentals is True and r.gate_reasons == []  # _good() passes the gate
    assert r.fundamental_score is not None  # absolute strength from the ticker's own metrics
    assert r.peer_percentile is not None  # AAA is in the ranked universe -> has a percentile
    assert all(c.fundamental_score == r.fundamental_score for c in r.contracts)
    assert all(c.peer_percentile == r.peer_percentile for c in r.contracts)
    assert r.next_earnings == date(2030, 1, 1) and r.earnings_known


def test_run_screen_end_to_end():
    chain = _chain([_put(90, -0.20, 40, 1.9), _put(95, -0.10, 40, 1.0)])
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    crit = ScreenCriteria(top_n=10, min_dte=30, max_dte=45)
    results = service.run_screen(crit, date(2026, 6, 22))
    assert len(results) == 1
    r = results[0]
    assert r.symbol == "AAA"
    assert r.contract.strike == 90 and r.contract.delta == -0.20
    assert r.annualized_yield and r.annualized_yield > 0
    assert r.collateral == 9000.0
    assert r.fundamental_score is not None
    # AAA reports in 2030, far outside the swept window, so no row comes back for it — and
    # absence from a verified sweep IS the clean verdict (there is no date to display).
    assert r.next_earnings is None
    assert r.earnings_status is EarningsStatus.CLEAN


# --- the earnings blackout: no short put may span a report (issue #113) ------------------

def test_run_screen_drops_a_contract_whose_life_spans_earnings():
    """The reported failure, pinned (RDDT: a 27-DTE expiry offered while reporting 22 days out).
    With the calendar consulted per contract, that expiry cannot reach the results table."""
    chain = _chain([_put(150, -0.27, 27, 7.51)])  # expires _BASE + 27d
    fundamentals = _FakeFundamentals({"AAA": _BASE + timedelta(days=18)})  # reports before it
    service = ScreenerService(fundamentals=fundamentals, chains=_FakeChains(chain))
    crit = ScreenCriteria(top_n=10, min_dte=7, max_dte=45)
    assert service.run_screen(crit, _BASE) == []


def test_run_screen_picks_the_expiry_that_lands_before_the_report():
    """A report inside the DTE window must not veto the whole name — the clean nearer expiry is
    still exactly the trade a put seller wants, and the old name-level blackout threw it away."""
    chain = _chain([
        _put(90, -0.20, 14, 1.0),  # expires before the report
        _put(88, -0.20, 35, 3.0),  # spans it — and pays far more, so yield alone would pick it
    ])
    fundamentals = _FakeFundamentals({"AAA": _BASE + timedelta(days=28)})
    service = ScreenerService(fundamentals=fundamentals, chains=_FakeChains(chain))
    results = service.run_screen(ScreenCriteria(top_n=10, min_dte=7, max_dte=45), _BASE)
    assert len(results) == 1
    assert results[0].contract.dte == 14  # the clean expiry, despite the richer one spanning
    assert results[0].earnings_status is EarningsStatus.CLEAN


def test_run_screen_keeps_a_name_absent_from_a_verified_sweep():
    """Absence from a sweep that covers the whole window is a positive fact, not a gap: nobody
    reports in that range without appearing in it. That is what lets the sweep stay narrow."""
    chain = _chain([_put(90, -0.20, 27, 1.9)])
    service = ScreenerService(fundamentals=_FakeFundamentals({}), chains=_FakeChains(chain))
    results = service.run_screen(ScreenCriteria(top_n=10, min_dte=7, max_dte=45), _BASE)
    assert len(results) == 1 and results[0].earnings_status is EarningsStatus.CLEAN


def test_run_screen_makes_no_per_symbol_earnings_calls():
    """The screen answers from the sweep alone. Per-symbol lookups belong to ticker search —
    doing them here would mean one call per surviving name to learn what the sweep already
    established."""
    chain = _chain([_put(90, -0.20, 27, 1.9)])
    fundamentals = _FakeFundamentals({"AAA": _BASE + timedelta(days=200)})  # outside the window
    service = ScreenerService(fundamentals=fundamentals, chains=_FakeChains(chain))
    service.run_screen(ScreenCriteria(top_n=10, min_dte=7, max_dte=45), _BASE)
    assert fundamentals.symbol_lookups == []


def test_run_screen_sweeps_only_the_window_the_contracts_live_in():
    """No point loading calendar past the furthest expiry we would sell — nothing there can
    change a verdict."""
    seen: list[tuple] = []
    fundamentals = _FakeFundamentals({})
    inner = fundamentals.earnings_calendar
    fundamentals.earnings_calendar = lambda s, e: (seen.append((s, e)), inner(s, e))[1]
    service = ScreenerService(
        fundamentals=fundamentals, chains=_FakeChains(_chain([_put(90, -0.20, 27, 1.9)]))
    )
    crit = ScreenCriteria(top_n=10, min_dte=7, max_dte=45, earnings_buffer_days=2)
    service.run_screen(crit, _BASE)
    assert seen == [(_BASE, _BASE + timedelta(days=47))]  # max_dte + buffer, fetched once


def test_earnings_buffer_covers_a_date_that_drifts_earlier():
    """Published dates move. A report two days after expiry is close enough that a drift would
    put it inside the contract's life, so it counts as spanning."""
    chain = _chain([_put(90, -0.20, 14, 1.0)])
    fundamentals = _FakeFundamentals({"AAA": _BASE + timedelta(days=16)})  # 2 days after expiry
    service = ScreenerService(fundamentals=fundamentals, chains=_FakeChains(chain))
    crit = ScreenCriteria(top_n=10, min_dte=7, max_dte=45, earnings_buffer_days=2)
    assert service.run_screen(crit, _BASE) == []
    no_buffer = crit.model_copy(update={"earnings_buffer_days": 0})
    assert len(service.run_screen(no_buffer, _BASE)) == 1


def test_search_flags_earnings_expiries_without_hiding_them():
    """Search is a research tool: a typed ticker shows its whole term structure, marked — an
    empty table would read as 'no contracts' and hide the risk instead of showing it."""
    chain = _chain([_put(90, -0.20, 14, 1.0), _put(88, -0.20, 35, 3.0)])
    fundamentals = _FakeFundamentals({"AAA": _BASE + timedelta(days=28)})
    service = ScreenerService(fundamentals=fundamentals, chains=_FakeChains(chain))
    r = service.search_ticker("AAA", ScreenCriteria(min_dte=7, max_dte=45), _BASE, n=5)
    assert [c.contract.dte for c in r.contracts] == [14, 35]  # nothing hidden
    assert [c.earnings_status for c in r.contracts] == [EarningsStatus.CLEAN, EarningsStatus.SPANS]
    assert r.next_earnings == _BASE + timedelta(days=28)
    assert fundamentals.symbol_lookups == ["AAA"]  # one per-symbol call, not a market-wide pull


# --- covered calls: the search-side knob -------------------------------------

def test_search_calls_targets_positive_delta_and_pulls_the_call_chain():
    """The criteria carry a magnitude; the call side must re-sign it. Aiming at -0.20 on a chain
    of positive deltas would pick the FURTHEST contract from the target, not the nearest."""
    chain = _chain(
        [_call(110, 0.20, 14, 1.0), _call(105, 0.30, 14, 1.8), _call(120, 0.05, 14, 0.2)],
        underlying_price=100.0,
    )
    chains = _FakeChains(chain)
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=chains)
    r = service.search_ticker(
        "AAA", ScreenCriteria(min_dte=7, max_dte=45), _BASE, n=1, side=OptionType.CALL
    )
    assert chains.requested_types == [OptionType.CALL]  # asked the provider for calls
    assert r.side is OptionType.CALL
    assert [c.contract.strike for c in r.contracts] == [110]  # 0.20Δ, nearest the target


def test_call_yield_is_premium_over_share_price_not_strike():
    """The whole point of the side split: a covered call posts no cash, so the base is what the
    100 pledged shares are worth — using the strike would silently misprice every call."""
    chain = _chain([_call(110, 0.20, 73, 2.0)], underlying_price=100.0)  # 73 DTE = a fifth of a yr
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    r = service.search_ticker(
        "AAA", ScreenCriteria(min_dte=7, max_dte=90), _BASE, n=1, side=OptionType.CALL
    )
    c = r.contracts[0]
    assert abs(c.annualized_yield - (2.0 / 100.0) * (365 / 73)) < 1e-9  # 10%/yr off SPOT
    assert abs(c.annualized_yield - (2.0 / 110.0) * (365 / 73)) > 1e-3  # NOT off the strike
    assert c.collateral == 100.0 * 100  # 100 shares at the market price
    assert r.underlying_price == 100.0


def test_put_yield_still_uses_the_strike():
    """Regression guard on the CSP path — the base for a cash-secured put is the collateral."""
    chain = _chain([_put(90, -0.20, 73, 1.8)], underlying_price=100.0)
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    r = service.search_ticker("AAA", ScreenCriteria(min_dte=7, max_dte=90), _BASE, n=1)
    c = r.contracts[0]
    assert abs(c.annualized_yield - (1.8 / 90.0) * (365 / 73)) < 1e-9
    assert c.collateral == 90 * 100


def test_call_spot_falls_back_to_the_provider_quote():
    """Alpaca's option chains are option-only, so spot comes from a separate quote call."""
    chain = _chain([_call(110, 0.20, 73, 2.0)])  # no underlying_price in-band
    chains = _QuotingChains(chain, spot=100.0)
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=chains)
    r = service.search_ticker(
        "AAA", ScreenCriteria(min_dte=7, max_dte=90), _BASE, n=1, side=OptionType.CALL
    )
    assert r.underlying_price == 100.0
    assert abs(r.contracts[0].annualized_yield - (2.0 / 100.0) * (365 / 73)) < 1e-9


def test_call_without_a_spot_lists_the_contract_with_no_yield():
    """A missing price feed must degrade the yield cell, not delete the row or invent a number."""
    chain = _chain([_call(110, 0.20, 73, 2.0)])  # no spot in-band, no quote endpoint, no profile
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    r = service.search_ticker(
        "AAA", ScreenCriteria(min_dte=7, max_dte=90), _BASE, n=1, side=OptionType.CALL
    )
    assert len(r.contracts) == 1  # still listed — it is perfectly sellable
    assert r.contracts[0].annualized_yield is None and r.contracts[0].collateral is None


def test_search_defaults_to_puts():
    chain = _chain([_put(90, -0.20, 14, 1.0), _call(110, 0.20, 14, 1.0)], underlying_price=100.0)
    chains = _FakeChains(chain)
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=chains)
    r = service.search_ticker("AAA", ScreenCriteria(min_dte=7, max_dte=45), _BASE, n=5)
    assert chains.requested_types == [OptionType.PUT]
    assert all(c.contract.option_type is OptionType.PUT for c in r.contracts)


def test_screen_stays_put_only():
    """Calls are a search-only feature: the screen picks what to *acquire*, which is a put."""
    chain = _chain([_put(90, -0.20, 27, 1.9), _call(110, 0.20, 27, 1.9)], underlying_price=100.0)
    chains = _FakeChains(chain)
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=chains)
    out = service.run_screen(ScreenCriteria(top_n=10, min_dte=7, max_dte=45), _BASE)
    assert chains.requested_types == [OptionType.PUT]
    assert all(c.contract.option_type is OptionType.PUT for c in out)


def test_top_n_is_not_silently_overridden_by_the_prerank_cap() -> None:
    """The pre-rank cap used to override top_n without saying so, which made the caller's
    "check N names" a no-op above ~prerank_keep — a control that looked live and did nothing."""
    from wheel_screener.core.earnings import EarningsGuard
    from wheel_screener.core.models import EarningsPolicy, Underlying
    from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank

    names = [
        Underlying(symbol=f"S{i:03}", price=100.0, market_cap=1e10, sector="Tech")
        for i in range(300)
    ]
    good = FundamentalMetrics(
        pe=12.0, ps=1.0, pb=1.5, roe=0.25, roa=0.12, ros=0.15, eps=4.0,
        debt_to_equity=0.5, current_ratio=2.0, net_debt_to_ebitda=1.0,
        total_equity=1e9, net_income=1e8, free_cash_flow=1e8, ebitda=2e8,
    )

    class _Provider:
        def bulk_metrics(self, symbols):
            return dict.fromkeys(symbols, good)

        def fetch_metrics(self, symbols):
            return dict.fromkeys(symbols, good)

    guard = EarningsGuard({}, date(2026, 8, 29), policy=EarningsPolicy.OFF)
    crit = ScreenCriteria(prerank_keep=50, top_n=200)
    kept = rate_and_rank(_Provider(), names, crit, date(2026, 8, 29), guard)
    assert len(kept) == 200, "top_n must win when it asks for more than prerank_keep"


def test_the_prerank_cap_gates_before_it_cuts() -> None:
    """Gating is free, so cap slots must not be spent on names that are about to be gated out."""
    from wheel_screener.core.earnings import EarningsGuard
    from wheel_screener.core.models import EarningsPolicy, Underlying
    from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank

    ok = FundamentalMetrics(
        pe=12.0, ps=1.0, pb=1.5, roe=0.25, roa=0.12, ros=0.15, eps=4.0,
        debt_to_equity=0.5, current_ratio=2.0, net_debt_to_ebitda=1.0,
        total_equity=1e9, net_income=1e8, free_cash_flow=1e8, ebitda=2e8,
    )
    loss = ok.model_copy(update={"eps": -1.0, "net_income": -1e8})  # loss_maker: a HARD failure
    names = [
        Underlying(symbol=f"S{i:03}", price=100.0, market_cap=1e10, sector="Tech")
        for i in range(40)
    ]

    class _Provider:
        # keyed off the SYMBOL, not list position, so the deep fetch can't reassign metrics
        # to a different set of names than the pre-rank saw
        def bulk_metrics(self, symbols):
            return {s: (ok if int(s[1:]) % 2 == 0 else loss) for s in symbols}

        def fetch_metrics(self, symbols):
            return self.bulk_metrics(symbols)

    guard = EarningsGuard({}, date(2026, 8, 29), policy=EarningsPolicy.OFF)
    crit = ScreenCriteria(prerank_keep=10, top_n=10)
    kept = rate_and_rank(_Provider(), names, crit, date(2026, 8, 29), guard)
    assert len(kept) == 10, "all 10 slots should hold viable names, not gate-failures"


def test_an_unrated_name_ranks_where_a_typical_one_does_not_above_all_of_them() -> None:
    """Scoring an unrated name on yield alone let the ABSENCE of an assessment act as a perfect
    one. Harmless while only the odd thinly-covered stock was unrated; decisive once ETFs joined
    the same list, since none of them can be rated — they swept the top ranks at a flat 1.00,
    a 3x leveraged fund among them, while a stock rated 0.88 on the same yield came ninth."""
    field = [
        CandidateResult(symbol=f"S{i}", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=score, annualized_yield=0.25)
        for i, score in enumerate((0.70, 0.75, 0.80, 0.85, 0.90))
    ]
    etf = CandidateResult(symbol="ETF", contract=_put(90, -0.2, 40, 1.0),
                          fundamental_score=None, annualized_yield=0.25)
    ranked = rank([etf, *field], fundamental_weight=0.5)
    assert ranked[0].symbol != "ETF", "an unassessed name must not outrank every assessed one"
    etf_row = next(c for c in ranked if c.symbol == "ETF")
    # it sits where the median-rated name does: above the weak half, below the strong half
    assert 0 < ranked.index(etf_row) < len(ranked) - 1
    assert abs(etf_row.score - next(c.score for c in ranked if c.symbol == "S2")) < 1e-9


def test_a_field_too_small_to_have_a_middle_falls_back_to_yield_alone() -> None:
    """Two points are not a field. Taking their midpoint would let one weak name drag every
    unrated one to zero — the deletion the unknown-is-not-zero rule exists to prevent."""
    unknown = CandidateResult(symbol="U", contract=_put(90, -0.2, 40, 1.0),
                              fundamental_score=None, annualized_yield=0.25)
    weak = CandidateResult(symbol="Z", contract=_put(90, -0.2, 40, 1.0),
                           fundamental_score=0.0, annualized_yield=0.25)
    ranked = rank([unknown, weak], fundamental_weight=0.5)
    assert ranked[0].symbol == "U" and ranked[0].score == 1.0
