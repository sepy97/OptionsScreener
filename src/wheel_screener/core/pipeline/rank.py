"""Stage 5 — score and order the shortlist."""

from __future__ import annotations

from wheel_screener.core.models import CandidateResult


def rank(candidates: list[CandidateResult]) -> list[CandidateResult]:
    """Sort candidates by fit (annualized yield, IV rank, quality, liquidity).

    TODO(M4): compute ``score`` from yield / IV-rank / fundamentals / distance-to-
    strike and sort descending.
    """
    raise NotImplementedError("Stage 5 (ranking) lands in M4")
