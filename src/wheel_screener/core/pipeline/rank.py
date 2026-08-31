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

    UNKNOWN strength is not zero — under a geometric mean that would delete the name from the
    list, a far stronger claim than the data supports. But it is not one either, and returning
    the yield alone made it exactly that: an unrated name scored as though it had rated
    PERFECTLY. Harmless while the only unrated names were the odd stock with thin coverage;
    decisive once ETFs joined the same list, since none of them can be rated. They swept the
    top five ranks at a flat 1.00 — including a 3x leveraged fund, whose yield is high
    precisely because its assignment risk is — while a stock rated 0.88 on the same yield came
    ninth. The absence of an assessment is not a good assessment.

    ``strength`` is therefore expected to be substituted with the field's median before this is
    called (see ``rank``), so an unrated name sits where a typical one does: neither rewarded
    nor punished for a question that has no answer.
    """
    if strength is None:
        return yield_rated
    return (strength**weight) * (yield_rated ** (1.0 - weight))


# A median needs a field to be the middle of. Below this many rated names there is no field,
# only a couple of points, and substituting their midpoint would let one weak name drag every
# unrated one to zero — which is exactly the deletion the unknown-is-not-zero rule exists to
# prevent. Under it, unrated names fall back to yield alone.
_MIN_RATED_FOR_MEDIAN = 5


def _median(values: list[float]) -> float | None:
    """The middle of the rated field, or None when there is not enough of one to speak of.

    None rather than a made-up 0.5: a screen with nothing rated has no field to sit in the
    middle of, and inventing one would rank against a fiction.
    """
    if len(values) < _MIN_RATED_FOR_MEDIAN:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def rank(
    candidates: list[CandidateResult],
    fundamental_weight: float = 0.5,
    *,
    yield_good: float = 0.25,
    yield_satisfactory: float = 0.15,
    min_score: float | None = None,
) -> list[CandidateResult]:
    """Score every candidate, drop anything under ``min_score``, and sort best-first.

    Unrated names — ETFs, and the occasional stock with too little coverage to judge — take the
    field's MEDIAN strength rather than none at all. Scoring them on yield alone let the
    absence of an assessment act as a perfect one, which put every ETF above every stock.
    """
    rated_scores = sorted(
        c.fundamental_score for c in candidates if c.fundamental_score is not None
    )
    stand_in = _median(rated_scores)
    for c in candidates:
        rated = yield_rating(c.annualized_yield, yield_good, yield_satisfactory)
        strength = c.fundamental_score if c.fundamental_score is not None else stand_in
        c.score = blend(strength, rated, fundamental_weight)
    kept = candidates if min_score is None else [
        c for c in candidates if (c.score or 0.0) >= min_score
    ]
    return sorted(kept, key=lambda c: c.score or 0.0, reverse=True)
