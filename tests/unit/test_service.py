from __future__ import annotations

from datetime import date

from wheel_screener.core.models import (
    ChainFilter,
    ChainSnapshot,
    FundamentalMetrics,
    ProviderCaps,
    ScreenCriteria,
    Underlying,
)
from wheel_screener.core.service import ScreenerService


class _FakeFundamentals:
    def __init__(self, universe, metrics, earnings=None, with_bulk=True):
        self._universe = universe
        self._metrics = metrics
        self._earnings = earnings or {}
        self._with_bulk = with_bulk

    def screen_universe(self, criteria: ScreenCriteria):
        return [Underlying(symbol=s, sector=sec, market_cap=1.0e9) for s, sec in self._universe]

    def bulk_metrics(self, symbols):
        if not self._with_bulk:  # simulate a tier without the *-ttm-bulk endpoints
            return {}
        return {s: self._metrics[s] for s in symbols if s in self._metrics}

    def fetch_metrics(self, symbols):
        return {s: self._metrics[s] for s in symbols if s in self._metrics}

    def earnings_calendar(self, start, end):
        return self._earnings


class _FakeChains:
    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        return ChainSnapshot(underlying_symbol=symbol)

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(name="fake")


def _good() -> FundamentalMetrics:
    return FundamentalMetrics(
        pe=10, ps=1, pb=1, roe=0.25, roa=0.12, ros=0.12, roi=0.25,
        debt_to_equity=0.3, net_debt_to_ebitda=0.5, ebitda=100.0,
        current_ratio=1.5, quick_ratio=1.0, cash_ratio=0.6, eps=5.0, total_equity=1000.0,
    )


def test_screen_fundamentals_end_to_end() -> None:
    universe = [("GOOD", "Tech"), ("ALSO", "Tech"), ("LOSS", "Tech")]
    metrics = {
        "GOOD": _good(),
        "ALSO": _good(),
        "LOSS": FundamentalMetrics(  # loss-maker -> gated out
            pe=5, ps=1, pb=1, roe=0.2, roa=0.1, ros=-0.1, roi=0.1,
            debt_to_equity=0.3, current_ratio=1.5, eps=-1.0, total_equity=500.0,
        ),
    }
    service = ScreenerService(
        fundamentals=_FakeFundamentals(universe, metrics), chains=_FakeChains()
    )

    ranked = service.screen_fundamentals(ScreenCriteria(top_n=5), date(2026, 6, 21))

    symbols = {u.symbol for u in ranked}
    assert symbols == {"GOOD", "ALSO"}  # LOSS gated out (loss_maker)
    assert all(u.fundamental_score is not None for u in ranked)


def test_screen_fundamentals_respects_top_n_and_blackout() -> None:
    universe = [(f"S{i}", "Tech") for i in range(6)]
    metrics = {f"S{i}": _good() for i in range(6)}
    earnings = {"S0": date(2026, 7, 1)}  # inside the DTE window -> dropped
    service = ScreenerService(
        fundamentals=_FakeFundamentals(universe, metrics, earnings), chains=_FakeChains()
    )

    ranked = service.screen_fundamentals(ScreenCriteria(top_n=3), date(2026, 6, 21))

    assert len(ranked) == 3
    assert "S0" not in {u.symbol for u in ranked}


def test_screen_fundamentals_falls_back_without_bulk() -> None:
    # Lower FMP tier: bulk_metrics returns {} -> market-cap-capped deep fetch path.
    universe = [("GOOD", "Tech"), ("LOSS", "Tech")]
    metrics = {
        "GOOD": _good(),
        "LOSS": FundamentalMetrics(
            pe=5, ps=1, pb=1, roe=0.2, roa=0.1, ros=-0.1, roi=0.1,
            debt_to_equity=0.3, current_ratio=1.5, eps=-1.0, total_equity=500.0,
        ),
    }
    service = ScreenerService(
        fundamentals=_FakeFundamentals(universe, metrics, with_bulk=False), chains=_FakeChains()
    )

    criteria = ScreenCriteria(top_n=5, universe_limit=10)
    ranked = service.screen_fundamentals(criteria, date(2026, 6, 21))

    assert {u.symbol for u in ranked} == {"GOOD"}  # LOSS still gated; fallback path works
