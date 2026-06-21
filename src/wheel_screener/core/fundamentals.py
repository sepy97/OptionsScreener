"""Fundamental ranking — sanitize, hard gates, and a cross-sectional within-sector
percentile composite.

Replaces an earlier absolute-threshold bucket scorer that had sign-inversion bugs
(negative PE / negative-equity ratios scored as "good") and let data coverage drive
the rank. Designed via a multi-expert review; see docs/PLAN.md.

The score is only an intermediate funnel: it picks the top-N names for the expensive
options-chain pull. The user-facing sort is annualized yield, and a human verifies the
shortlist — so the objective is recall (don't drop good names), not false precision.

Flow (the cross-sectional parts are pure given a list of Underlyings):
  sanitize_metrics      — domain guards (drop PE/PEG when EPS<=0, PB/D-E when equity<=0,
                          net-debt/EBITDA when EBITDA<=0; compute the DCF gap)
  gate_reasons          — hard never-trade kills (negative equity, loss-maker, excess
                          leverage, illiquid, insufficient coverage)
  rank_by_fundamentals  — within-sector percentile per metric -> Value/Quality/Safety
                          factors -> weighted composite (durability tilt by default)
"""

from __future__ import annotations

from collections import defaultdict

from wheel_screener.core.models import (
    FundamentalMetrics,
    FundamentalRating,
    ScreenCriteria,
    StockProfile,
    Underlying,
)

# Default factor weights: tilt to durability — the wheel's real risk is being assigned
# and holding a value trap / balance-sheet blowup, so Quality+Safety outweigh cheapness.
DURABILITY_TILT = {"value": 0.20, "quality": 0.45, "safety": 0.35}

# factor -> (sector_neutral?, [(metric, orientation)]). Value is ranked universe-wide so
# absolute cheapness survives; Quality/Safety are sector-neutral (peer-relative).
_FACTORS: dict[str, tuple[bool, list[tuple[str, str]]]] = {
    "value": (
        False,
        [("pe", "low"), ("ps", "low"), ("pb", "low"), ("peg", "low"), ("dcf_gap", "low")],
    ),
    "quality": (True, [("roe", "high"), ("roa", "high"), ("roi", "high"), ("ros", "high")]),
    "safety": (True, [
        ("debt_to_equity", "low"),
        ("net_debt_to_ebitda", "low"),
        ("current_ratio", "high"),
        ("quick_ratio", "high"),
        ("cash_ratio", "high"),
    ]),
}

# metrics counted toward the coverage gate
_CORE_METRICS = ["pe", "ps", "pb", "roe", "roa", "ros", "debt_to_equity", "current_ratio"]

_MIN_BUCKET = 5  # min names in a sector before we sector-neutralize (else universe-wide)


def sanitize_metrics(m: FundamentalMetrics) -> dict[str, float | None]:
    """Apply domain guards and return the per-metric values used for ranking.

    Negative/undefined ratios are set to None (never treated as 'cheap'/'good').
    """
    eps, eq, ebitda = m.eps, m.total_equity, m.ebitda
    loss = eps is not None and eps <= 0
    no_equity = eq is not None and eq <= 0
    return {
        # value (None when the ratio is meaningless)
        "pe": m.pe if (m.pe is not None and m.pe > 0 and not loss) else None,
        "ps": m.ps if (m.ps is not None and m.ps > 0) else None,
        "pb": m.pb if (m.pb is not None and m.pb > 0 and not no_equity) else None,
        "peg": m.peg if (m.peg is not None and m.peg > 0 and not loss) else None,
        "dcf_gap": (m.price / m.dcf)
        if (m.price is not None and m.dcf is not None and m.dcf > 0)
        else None,
        # quality (raw; negatives gate out, see gate_reasons)
        "roe": m.roe,
        "roa": m.roa,
        "roi": m.roi,
        "ros": m.ros,
        # safety (net cash = negative net-debt/EBITDA with positive EBITDA is retained = good)
        "debt_to_equity": m.debt_to_equity if not no_equity else None,
        "net_debt_to_ebitda": m.net_debt_to_ebitda
        if not (ebitda is not None and ebitda <= 0)
        else None,
        "current_ratio": m.current_ratio,
        "quick_ratio": m.quick_ratio,
        "cash_ratio": m.cash_ratio,
    }


def gate_reasons(metrics: FundamentalMetrics | None, criteria: ScreenCriteria) -> list[str]:
    """Return hard never-trade reasons to drop a name ([] = passes the gates)."""
    if metrics is None:
        return ["no_metrics"]
    reasons: list[str] = []
    if metrics.total_equity is not None and metrics.total_equity <= 0:
        reasons.append("negative_equity")
    if (metrics.eps is not None and metrics.eps <= 0) or (
        metrics.ros is not None and metrics.ros < 0
    ):
        reasons.append("loss_maker")
    if metrics.roe is not None and metrics.roe < 0:
        reasons.append("negative_roe")
    if (
        metrics.net_debt_to_ebitda is not None
        and metrics.ebitda is not None
        and metrics.ebitda > 0
        and metrics.net_debt_to_ebitda > criteria.max_leverage
    ):
        reasons.append("excess_leverage")
    if metrics.current_ratio is not None and metrics.current_ratio < 1.0:
        reasons.append("illiquid")
    present = sum(1 for k in _CORE_METRICS if getattr(metrics, k) is not None)
    if present < criteria.min_metrics_present:
        reasons.append("insufficient_data")
    return reasons


def _pct_ranks(values: list[float | None]) -> list[float | None]:
    """Ascending percentile (0..1) of each value among the non-None values; ties share
    the mid-rank. None passes through."""
    present = [v for v in values if v is not None]
    n = len(present)
    out: list[float | None] = [None] * len(values)
    if n == 0:
        return out
    for i, v in enumerate(values):
        if v is None:
            continue
        less = sum(1 for x in present if x < v)
        equal = sum(1 for x in present if x == v)
        out[i] = (less + 0.5 * equal) / n
    return out


def _oriented_pct(values: list[float | None], orientation: str) -> list[float | None]:
    pct = _pct_ranks(values)
    if orientation == "low":
        return [(1.0 - p) if p is not None else None for p in pct]
    return pct


def _sector_pct(
    values: list[float | None], sectors: list[str], orientation: str
) -> list[float | None]:
    """Percentile within each sector; fall back to universe-wide for thin sectors."""
    out: list[float | None] = list(_oriented_pct(values, orientation))
    groups: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(sectors):
        groups[s].append(i)
    for idxs in groups.values():
        if len(idxs) >= _MIN_BUCKET:
            sub = _oriented_pct([values[i] for i in idxs], orientation)
            for j, i in enumerate(idxs):
                out[i] = sub[j]
    return out


def rank_by_fundamentals(
    names: list[Underlying],
    weights: dict[str, float] | None = None,
    profile: StockProfile = StockProfile.STALWART,
) -> list[Underlying]:
    """Score each name by a within-sector percentile composite; return them best-first.

    Sets ``fundamental_score`` and ``rating`` on each Underlying. Pure and deterministic
    given the input list.
    """
    if not names:
        return []
    weights = weights or DURABILITY_TILT
    total_w = sum(weights.values()) or 1.0

    san = [sanitize_metrics(u.metrics) if u.metrics else {} for u in names]
    sectors = [u.sector or "UNKNOWN" for u in names]

    factor_scores: list[dict[str, float]] = [{} for _ in names]
    for fname, (sector_neutral, specs) in _FACTORS.items():
        per_metric: dict[str, list[float | None]] = {}
        for metric, orient in specs:
            raw = [s.get(metric) for s in san]
            per_metric[metric] = (
                _sector_pct(raw, sectors, orient) if sector_neutral else _oriented_pct(raw, orient)
            )
        for i in range(len(names)):
            vals = [
                per_metric[metric][i] if per_metric[metric][i] is not None else 0.5
                for metric, _ in specs
            ]
            factor_scores[i][fname] = sum(vals) / len(vals)

    for i, u in enumerate(names):
        composite = sum(weights.get(f, 0.0) * factor_scores[i][f] for f in _FACTORS) / total_w
        u.fundamental_score = composite
        u.rating = FundamentalRating(
            profile=profile, category_scores=factor_scores[i], composite=composite
        )
    return sorted(names, key=lambda u: u.fundamental_score or 0.0, reverse=True)
