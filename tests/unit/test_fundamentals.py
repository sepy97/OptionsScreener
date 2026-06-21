from __future__ import annotations

from datetime import date

from wheel_screener.core.fundamentals import (
    gate_reasons,
    rank_by_fundamentals,
    sanitize_metrics,
)
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying
from wheel_screener.core.pipeline.rate_fundamentals import apply_earnings_blackout, select_top


def _healthy(**kw) -> FundamentalMetrics:
    base = dict(
        pe=12, ps=2, pb=2, roe=0.20, roa=0.10, ros=0.10, roi=0.20,
        debt_to_equity=0.5, net_debt_to_ebitda=1.0, ebitda=100.0,
        current_ratio=1.5, quick_ratio=1.0, cash_ratio=0.5,
        eps=4.0, total_equity=1000.0,
    )
    base.update(kw)
    return FundamentalMetrics(**base)


def _u(symbol: str, sector: str = "Tech", **kw) -> Underlying:
    return Underlying(symbol=symbol, sector=sector, metrics=_healthy(**kw))


# --- sanitize: the bug fixes ------------------------------------------------

def test_sanitize_drops_valuation_for_loss_maker() -> None:
    s = sanitize_metrics(FundamentalMetrics(pe=-5, peg=-1, eps=-2.0, ps=3))
    assert s["pe"] is None and s["peg"] is None  # negative PE/PEG no longer "cheap"
    assert s["ps"] == 3


def test_sanitize_drops_book_ratios_for_negative_equity() -> None:
    s = sanitize_metrics(FundamentalMetrics(pb=-2, debt_to_equity=-3, total_equity=-100))
    assert s["pb"] is None and s["debt_to_equity"] is None


def test_sanitize_drops_netdebt_when_ebitda_nonpositive() -> None:
    s = sanitize_metrics(FundamentalMetrics(net_debt_to_ebitda=-2, ebitda=-10))
    assert s["net_debt_to_ebitda"] is None


def test_sanitize_keeps_net_cash() -> None:
    # negative net-debt/EBITDA with positive EBITDA = net cash = legitimately good
    s = sanitize_metrics(FundamentalMetrics(net_debt_to_ebitda=-1.0, ebitda=50))
    assert s["net_debt_to_ebitda"] == -1.0


def test_sanitize_dcf_gap() -> None:
    assert sanitize_metrics(FundamentalMetrics(price=80, dcf=100))["dcf_gap"] == 0.8


# --- gates ------------------------------------------------------------------

def test_gate_clean_name_passes() -> None:
    assert gate_reasons(_healthy(), ScreenCriteria()) == []


def test_gate_loss_maker() -> None:
    assert "loss_maker" in gate_reasons(_healthy(eps=-1.0), ScreenCriteria())


def test_gate_negative_equity() -> None:
    assert "negative_equity" in gate_reasons(_healthy(total_equity=-5), ScreenCriteria())


def test_gate_excess_leverage() -> None:
    reasons = gate_reasons(_healthy(net_debt_to_ebitda=6, ebitda=100), ScreenCriteria())
    assert "excess_leverage" in reasons


def test_gate_illiquid() -> None:
    assert "illiquid" in gate_reasons(_healthy(current_ratio=0.8), ScreenCriteria())


def test_gate_insufficient_data() -> None:
    assert "insufficient_data" in gate_reasons(FundamentalMetrics(pe=10), ScreenCriteria())


def test_gate_none_metrics() -> None:
    assert gate_reasons(None, ScreenCriteria()) == ["no_metrics"]


# --- ranking ----------------------------------------------------------------

def test_rank_is_monotonic() -> None:
    good = _u("GOOD", roe=0.40, roa=0.30, roi=0.40, ros=0.30, pe=6, ps=0.5, pb=0.5,
              debt_to_equity=0.1, net_debt_to_ebitda=0.0)
    mid = _u("MID")
    bad = _u("BAD", roe=0.06, roa=0.02, roi=0.06, ros=0.02, pe=19, ps=4, pb=5,
             debt_to_equity=1.8, net_debt_to_ebitda=3.5)
    ranked = rank_by_fundamentals([bad, mid, good])
    assert [u.symbol for u in ranked] == ["GOOD", "MID", "BAD"]
    assert ranked[0].fundamental_score >= ranked[-1].fundamental_score


def test_rank_sets_factor_breakdown() -> None:
    ranked = rank_by_fundamentals([_u("A"), _u("B", roe=0.05)])
    assert set(ranked[0].rating.category_scores) == {"value", "quality", "safety"}


def test_rank_is_deterministic() -> None:
    mk = lambda: [_u("A", roe=0.30), _u("B", roe=0.10), _u("C", roe=0.20)]  # noqa: E731
    assert [u.symbol for u in rank_by_fundamentals(mk())] == [
        u.symbol for u in rank_by_fundamentals(mk())
    ]


# --- select_top: end-to-end stage logic -------------------------------------

def test_select_top_excludes_sign_trap() -> None:
    # A loss-maker with negative PE scored 1.0 "good" under the old bug; now gated out.
    today = date(2026, 6, 21)
    out = select_top([_u("LOSS", eps=-3.0, pe=-5), _u("OK")], ScreenCriteria(), {}, today)
    assert [u.symbol for u in out] == ["OK"]


def test_select_top_truncates_and_blacks_out_earnings() -> None:
    today = date(2026, 6, 21)
    names = [_u(f"S{i}") for i in range(10)]
    criteria = ScreenCriteria(top_n=3)
    out = select_top(names, criteria, {"S0": date(2026, 7, 1)}, today)
    assert len(out) == 3
    assert "S0" not in {u.symbol for u in out}


def test_select_top_sector_cap() -> None:
    today = date(2026, 6, 21)
    names = [_u(f"T{i}", sector="Tech") for i in range(5)] + [
        _u(f"E{i}", sector="Energy") for i in range(5)
    ]
    out = select_top(names, ScreenCriteria(top_n=10, max_per_sector=2), {}, today)
    sectors = [u.sector for u in out]
    assert sectors.count("Tech") <= 2 and sectors.count("Energy") <= 2


def test_earnings_blackout_window() -> None:
    today = date(2026, 6, 21)
    names = [Underlying(symbol="AAA"), Underlying(symbol="BBB"), Underlying(symbol="CCC")]
    earnings = {"AAA": date(2026, 7, 1), "BBB": date(2026, 9, 1)}  # AAA in-window; BBB out
    kept = {u.symbol for u in apply_earnings_blackout(names, earnings, today, max_dte=45)}
    assert kept == {"BBB", "CCC"}
