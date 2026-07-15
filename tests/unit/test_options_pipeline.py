from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.core.models import (
    CandidateResult,
    ChainSnapshot,
    FundamentalMetrics,
    OptionContract,
    OptionType,
    ProviderCaps,
    ScreenCriteria,
    Underlying,
)
from wheel_screener.core.pipeline.rank import rank
from wheel_screener.core.pipeline.select_strike import select_put, select_top_puts
from wheel_screener.core.service import ScreenerService

_BASE = date(2026, 6, 22)


def _put(strike, delta, dte, bid, oi=500, spread=0.02):
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
    )


def _chain(contracts):
    return ChainSnapshot(underlying_symbol="AAA", contracts=contracts)


def test_select_put_picks_best_yield_near_target_delta():
    chain = _chain([
        _put(95, -0.10, 35, 1.0), _put(90, -0.20, 35, 1.5), _put(85, -0.30, 35, 2.2),
        _put(90, -0.20, 40, 1.9),  # same delta, higher annualized yield than the 35-DTE
    ])
    put = select_put(chain, ScreenCriteria(min_dte=30, max_dte=45))  # explicit window (35 & 40 in)
    assert put is not None
    assert put.strike == 90 and put.dte == 40


def test_select_put_applies_gates():
    crit = ScreenCriteria(min_dte=30, max_dte=45)  # min_oi=100, max_spread=0.10, |Δ|≤0.30
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, oi=50)]), crit) is None      # low OI
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, spread=0.5)]), crit) is None  # wide spread
    assert select_put(_chain([_put(90, -0.40, 40, 1.5)]), crit) is None  # |delta|>0.30
    assert select_put(_chain([_put(90, -0.20, 10, 1.5)]), crit) is None  # DTE below window
    assert select_put(_chain([_put(90, -0.20, 40, 0.0)]), crit) is None  # bid 0 = unsellable


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


def test_rank_uses_raw_absolute_strength() -> None:
    # strength enters the blend RAW (it's already absolute 0..1), so a tiny quality gap makes a
    # tiny score gap — it is NOT amplified into a big cohort-percentile gap.
    a = CandidateResult(symbol="A", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.80, annualized_yield=0.20)
    b = CandidateResult(symbol="B", contract=_put(90, -0.2, 40, 1.0),
                        fundamental_score=0.79, annualized_yield=0.20)
    ranked = rank([a, b], fundamental_weight=0.5)  # equal yield -> yield percentile 0.5 each
    assert ranked[0].symbol == "A"  # the 0.01-stronger name edges ahead
    assert abs(ranked[0].score - 0.65) < 1e-9  # 0.5*0.80 (raw strength) + 0.5*0.5 (yield pct)
    assert abs(ranked[0].score - ranked[1].score) < 0.02  # tiny gap, not cohort-amplified


def _good() -> FundamentalMetrics:
    return FundamentalMetrics(
        pe=10, ps=1, pb=1, roe=0.25, roa=0.12, ros=0.12, roi=0.25,
        debt_to_equity=0.3, net_debt_to_ebitda=0.5, ebitda=100.0,
        current_ratio=1.5, quick_ratio=1.0, cash_ratio=0.6, eps=5.0, total_equity=1000.0,
    )


class _FakeFundamentals:
    def screen_universe(self, criteria):
        return [Underlying(symbol="AAA", sector="Technology", market_cap=5e9)]

    def bulk_metrics(self, symbols):
        return {"AAA": _good()}

    def fetch_metrics(self, symbols):
        return {"AAA": _good()}

    def earnings_calendar(self, start, end):
        return {}


class _FakeChains:
    def __init__(self, chain):
        self._chain = chain

    def get_chain(self, symbol, filt):
        return self._chain

    def capabilities(self):
        return ProviderCaps(name="fake")


def test_select_top_puts_nearest_target_one_per_expiry():
    crit = ScreenCriteria(min_dte=7, max_dte=45)  # target delta -0.20
    chain = _chain([
        _put(90, -0.20, 14, 1.0), _put(88, -0.30, 14, 1.5),  # 14 DTE: -0.20 is nearest target
        _put(85, -0.19, 28, 1.2), _put(80, -0.28, 28, 2.0),  # 28 DTE: -0.19 nearest
        _put(75, -0.05, 40, 0.3),                             # 40 DTE: -0.05 (far from target)
    ])
    top = select_top_puts(chain, crit, 2)
    assert [c.dte for c in top] == [14, 28]  # the 2 nearest-target expiries, earliest first
    assert [c.delta for c in top] == [-0.20, -0.19]  # one per expiry, nearest -0.20
    assert len(select_top_puts(chain, crit, 5)) == 3  # only 3 expiries available


def test_search_ticker_returns_top_puts_with_context():
    chain = _chain([_put(90, -0.20, 14, 1.0), _put(85, -0.19, 28, 1.2), _put(75, -0.05, 40, 0.3)])
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    r = service.search_ticker("aaa", ScreenCriteria(min_dte=7, max_dte=45), date(2026, 6, 22), n=2)
    assert r.symbol == "AAA"  # normalized to upper
    assert [c.contract.dte for c in r.puts] == [14, 28] and all(c.symbol == "AAA" for c in r.puts)
    assert r.passes_fundamentals is True and r.gate_reasons == []  # _good() passes the gate
    assert r.fundamental_score is not None  # absolute strength from the ticker's own metrics
    assert r.peer_percentile is not None  # AAA is in the ranked universe -> has a percentile
    assert all(c.fundamental_score == r.fundamental_score for c in r.puts)
    assert all(c.peer_percentile == r.peer_percentile for c in r.puts)
    assert r.next_earnings is None


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
