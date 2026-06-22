"""Stage 5 — order the candidate shortlist by annualized yield (the user-facing sort)."""

from __future__ import annotations

from wheel_screener.core.models import CandidateResult


def rank(candidates: list[CandidateResult]) -> list[CandidateResult]:
    """Sort candidates by annualized yield desc (fundamental score as a tiebreak) and set
    ``score`` to the annualized yield."""
    for c in candidates:
        c.score = c.annualized_yield
    return sorted(
        candidates,
        key=lambda c: (c.annualized_yield or 0.0, c.fundamental_score or 0.0),
        reverse=True,
    )
