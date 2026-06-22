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
from wheel_screener.core.pipeline.select_strike import select_put
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
    put = select_put(chain, ScreenCriteria())
    assert put is not None
    assert put.strike == 90 and put.dte == 40


def test_select_put_applies_gates():
    crit = ScreenCriteria()  # min_oi=100, max_spread=0.10, max_abs_delta=0.30, dte 30-45
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, oi=50)]), crit) is None      # low OI
    assert select_put(_chain([_put(90, -0.20, 40, 1.5, spread=0.5)]), crit) is None  # wide spread
    assert select_put(_chain([_put(90, -0.40, 40, 1.5)]), crit) is None  # |delta|>0.30
    assert select_put(_chain([_put(90, -0.20, 10, 1.5)]), crit) is None  # DTE below window
    assert select_put(_chain([_put(90, -0.20, 40, 0.0)]), crit) is None  # bid 0 = unsellable


def test_select_put_prefers_in_band_then_nearest_monthly():
    crit = ScreenCriteria()  # target band 30-45, dte_tolerance 10 -> window 20-55
    # in-band (35 DTE) wins over an out-of-band 25-DTE even with a richer raw yield
    both = _chain([_put(90, -0.20, 25, 3.0), _put(90, -0.20, 35, 1.0)])
    assert select_put(both, crit).dte == 35
    # monthly-only: nothing in 30-45 -> nearest monthly within tolerance is taken
    assert select_put(_chain([_put(90, -0.20, 25, 1.5)]), crit).dte == 25


def test_rank_orders_by_yield():
    a = CandidateResult(symbol="A", contract=_put(90, -0.2, 40, 1.0), annualized_yield=0.10)
    b = CandidateResult(symbol="B", contract=_put(90, -0.2, 40, 2.0), annualized_yield=0.25)
    ordered = rank([a, b])
    assert [c.symbol for c in ordered] == ["B", "A"]
    assert ordered[0].score == 0.25


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


def test_run_screen_end_to_end():
    chain = _chain([_put(90, -0.20, 40, 1.9), _put(95, -0.10, 40, 1.0)])
    service = ScreenerService(fundamentals=_FakeFundamentals(), chains=_FakeChains(chain))
    results = service.run_screen(ScreenCriteria(top_n=10), date(2026, 6, 22))
    assert len(results) == 1
    r = results[0]
    assert r.symbol == "AAA"
    assert r.contract.strike == 90 and r.contract.delta == -0.20
    assert r.annualized_yield and r.annualized_yield > 0
    assert r.collateral == 9000.0
    assert r.fundamental_score is not None
