"""Stage 5 — order the candidate shortlist using BOTH fundamental quality and yield.

After the fundamental gate has filtered out the bad names, the survivors that have a
tradeable put are ranked by a blend of (a) their fundamental composite and (b) their
annualized yield — each as a percentile rank among the candidates, combined with
``fundamental_weight`` (1.0 = all fundamentals, 0.0 = all yield).
"""

from __future__ import annotations

from wheel_screener.core.models import CandidateResult


def _percentiles(values: list[float]) -> list[float]:
    """Percentile rank (0..1) of each value among the list; ties share the mid-rank."""
    n = len(values)
    return [
        (sum(1 for x in values if x < v) + 0.5 * sum(1 for x in values if x == v)) / n
        for v in values
    ]


def rank(
    candidates: list[CandidateResult], fundamental_weight: float = 0.5
) -> list[CandidateResult]:
    if not candidates:
        return []
    fund_pct = _percentiles([c.fundamental_score or 0.0 for c in candidates])
    yield_pct = _percentiles([c.annualized_yield or 0.0 for c in candidates])
    w = fundamental_weight
    for c, fp, yp in zip(candidates, fund_pct, yield_pct, strict=True):
        c.score = w * fp + (1.0 - w) * yp
    return sorted(candidates, key=lambda c: c.score or 0.0, reverse=True)
