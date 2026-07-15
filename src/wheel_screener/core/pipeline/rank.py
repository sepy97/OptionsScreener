"""Stage 5 — order the candidate shortlist using BOTH fundamental quality and yield.

After the fundamental gate has filtered out the bad names, the survivors that have a
tradeable put are ranked by a blend of (a) their **absolute strength rating** and (b) their
annualized yield, combined with ``fundamental_weight`` (1.0 = all strength, 0.0 = all yield).

Strength enters the blend RAW: it's already an absolute 0..1 score, so an 80/100 name
contributes 0.80 regardless of the cohort (small quality gaps stay small, big ones stay big).
Yield is unbounded and right-skewed, so it's mapped to a within-run percentile first — the only
axis that genuinely needs normalizing to sit on the same 0..1 ruler.
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
    # strength is already absolute 0..1 → used raw; only yield needs normalizing → percentile
    yield_pct = _percentiles([c.annualized_yield or 0.0 for c in candidates])
    w = fundamental_weight
    for c, yp in zip(candidates, yield_pct, strict=True):
        c.score = w * (c.fundamental_score or 0.0) + (1.0 - w) * yp
    return sorted(candidates, key=lambda c: c.score or 0.0, reverse=True)
