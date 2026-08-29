"""Stage 5 — order the candidate shortlist using BOTH fundamental quality and yield.

The score is ABSOLUTE: the same contract scores the same whether it was screened alongside
five names or five hundred. That is what lets it be compared between runs, filtered on, and
coloured meaningfully.

Both halves are mapped to a 0..1 rating against fixed bars:

* **strength** already is one — the absolute financial-strength rating, used as-is.
* **yield** is graded against ``yield_good`` / ``yield_satisfactory``, the same anchors the
  results table colours by, so the number and its colour finally agree.

They are combined as a WEIGHTED GEOMETRIC MEAN, ``strength**w * yield**(1-w)``. A weighted
sum lets a poor company buy its way up the list on premium alone; a geometric mean does not,
because being near-zero on either half drags the whole score down. ``w`` stays the preference
dial: which half leads, not a claim about their relative worth.

An earlier version scored yield as a within-run PERCENTILE. That made the number a rank
position wearing a measurement's clothes: it could not be compared across runs, it destroyed
the distances between candidates, its ceiling was (n-0.5)/n, and a threshold on it filtered
nothing because the best of any list always scored near the top.
"""

from __future__ import annotations

from wheel_screener.core.models import CandidateResult


def yield_rating(
    annualized_yield: float | None, good: float = 0.25, satisfactory: float = 0.15
) -> float:
    """Grade an annualized yield 0..1 against fixed bars — 1.0 at ``good``, 0.5 at
    ``satisfactory``, straight-line between and below, 0 at or under no yield at all."""
    if annualized_yield is None or annualized_yield <= 0:
        return 0.0
    if annualized_yield >= good:
        return 1.0
    if annualized_yield >= satisfactory:
        return 0.5 + 0.5 * (annualized_yield - satisfactory) / (good - satisfactory)
    return 0.5 * annualized_yield / satisfactory


def blend(strength: float | None, yield_rated: float, weight: float) -> float:
    """Weighted geometric mean of the two ratings.

    UNKNOWN strength is not zero. A name whose fundamentals we never established is judged on
    its yield alone rather than being driven to a score of 0 — under a geometric mean that
    would delete it from the list entirely, which is a far stronger claim than the data
    supports. (A name we DID rate at 0 is a different statement and does score 0.)
    """
    if strength is None:
        return yield_rated
    return (strength**weight) * (yield_rated ** (1.0 - weight))


def rank(
    candidates: list[CandidateResult],
    fundamental_weight: float = 0.5,
    *,
    yield_good: float = 0.25,
    yield_satisfactory: float = 0.15,
    min_score: float | None = None,
) -> list[CandidateResult]:
    """Score every candidate, drop anything under ``min_score``, and sort best-first."""
    for c in candidates:
        rated = yield_rating(c.annualized_yield, yield_good, yield_satisfactory)
        c.score = blend(c.fundamental_score, rated, fundamental_weight)
    kept = candidates if min_score is None else [
        c for c in candidates if (c.score or 0.0) >= min_score
    ]
    return sorted(kept, key=lambda c: c.score or 0.0, reverse=True)
