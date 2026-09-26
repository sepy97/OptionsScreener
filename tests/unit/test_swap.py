"""The put swap rule — checked against the worked example in the spec (docs/PUT_SWAP_RULE.md)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wheel_screener.core.models import SwapAction, SwapSuggestion
from wheel_screener.core.swap import (
    OpenCall,
    OpenPut,
    SwapParams,
    list_median_yield,
    put_yield,
    review,
    review_covered_call,
)

TODAY = date(2026, 9, 21)
FRESH_YIELD = 0.219  # the spec's example: the average yield of the three puts opened that day


def _pick(symbol: str, yld: float, strike: float = 100.0) -> SwapSuggestion:
    return SwapSuggestion(
        symbol=symbol, strike=strike, expiration=TODAY + timedelta(days=30), dte=30,
        bid=1.0, annualized_yield=yld, collateral=strike * 100, same_ticker=True,
    )


def _open(symbol: str, strike: float, days: int, old_yield: float, spot: float,
          contracts: float = 1) -> OpenPut:
    """An open put whose ask reproduces the stated yield — the spec quotes yields, not prices."""
    return OpenPut(symbol=symbol, strike=strike, days=days, contracts=contracts, spot=spot,
                   ask=old_yield * strike * days / 365)


def test_yield_is_the_yearly_rate_on_the_locked_cash() -> None:
    assert put_yield(0.55, 130.0, 25) == pytest.approx(0.0618, abs=1e-4)
    assert put_yield(1.0, 0.0, 25) is None and put_yield(1.0, 100.0, 0) is None


# --- the spec's worked example, line by line -------------------------------------------------

@pytest.mark.parametrize(
    ("symbol", "strike", "days", "old_yield", "spot", "action", "extra", "stopped_by"),
    [
        ("CRDO", 130.0, 25, 0.062, 145.0, SwapAction.SWAP, 130.0, None),
        ("TER", 290.0, 25, 0.069, 320.0, SwapAction.SWAP, 288.0, None),
        # The spec's trace stops both of these on rule 1. At the shipped floor (10%/yr) neither
        # gets that far: a put paying 12% or 18% is not one whose cash is idle. Same verdicts,
        # earlier test — which is the point of having the floor first.
        ("SCCO", 170.0, 25, 0.125, 180.0, SwapAction.KEEP, None, "used up"),
        ("LRCX", 270.0, 4, 0.184, 290.0, SwapAction.KEEP, None, "used up"),
    ],
)
def test_the_worked_example(symbol, strike, days, old_yield, spot, action, extra,
                            stopped_by) -> None:
    r = review(_open(symbol, strike, days, old_yield, spot), _pick(symbol, FRESH_YIELD))
    assert r.action is action, r.reason
    assert r.old_yield == pytest.approx(old_yield, abs=5e-4)
    if extra is not None:
        assert r.extra_premium == pytest.approx(extra, abs=1.0)
    else:
        assert stopped_by in r.reason


def test_a_put_the_stock_has_fallen_below_is_the_assignment_question() -> None:
    """AVGO in the example: out of scope, and for a reason worth saying rather than a bare No."""
    r = review(_open("AVGO", 390.0, 4, 0.05, 380.0), _pick("AVGO", FRESH_YIELD))
    assert r.action is SwapAction.NOT_APPLICABLE and "assignment" in r.reason


# --- the two rules ---------------------------------------------------------------------------

def test_rule_1_needs_the_fresh_put_to_be_twice_as_good() -> None:
    """A quiet put whose replacement pays no more is not worth the trade."""
    quiet = review(_open("X", 100.0, 30, 0.08, 110.0), _pick("X", 0.09))
    assert quiet.action is SwapAction.KEEP and quiet.used_up and quiet.rule1_passed is False
    assert "1.1x" in quiet.reason


def test_a_put_that_still_pays_well_is_never_a_candidate() -> None:
    """MRVL, 21 Sep 2026, the case that prompted the floor: a put sold days earlier, 39 days
    left, still paying 22%/yr on its cash. A fresh put at the same expiry paid 27.8%. Nothing
    about that position is idle, so the rest of the rule never runs."""
    r = review(_open("MRVL", 210.0, 39, 0.219, 310.0), _pick("MRVL", 0.278))
    assert r.action is SwapAction.KEEP and r.used_up is False
    assert r.rule1_passed is None and r.rule2_passed is None  # never reached
    assert "22%/yr" in r.reason and "10%/yr" in r.reason


def test_the_floor_is_tunable() -> None:
    """A put at 8%/yr is under the default floor, so the rest of the rule runs. Lowering the
    floor is what makes the rule quieter: fewer puts count as used up."""
    eligible = review(_open("X", 100.0, 30, 0.08, 130.0), _pick("X", 0.40))
    assert eligible.action is SwapAction.SWAP and eligible.used_up
    stricter = review(_open("X", 100.0, 30, 0.08, 130.0), _pick("X", 0.40),
                      params=SwapParams(used_up_yield=0.05))
    assert stricter.action is SwapAction.KEEP and stricter.used_up is False


def test_rule_2_stops_small_and_nearly_expired_positions() -> None:
    """Same 5x yield gap, but too little cash and too few days for the swap to collect anything
    worth the trouble."""
    small = review(_open("X", 20.0, 30, 0.04, 30.0), _pick("X", 0.20))
    assert small.action is SwapAction.KEEP and small.rule1_passed and small.rule2_passed is False
    assert "rule 2" in small.reason
    # the same put with 10x the contracts clears the floor
    big = review(_open("X", 20.0, 30, 0.04, 30.0, contracts=10), _pick("X", 0.20))
    assert big.action is SwapAction.SWAP


def test_the_extra_is_net_of_the_swap_cost() -> None:
    free = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", FRESH_YIELD),
                  params=SwapParams(swap_cost=0.0))
    charged = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", FRESH_YIELD),
                     params=SwapParams(swap_cost=10.0))
    assert free.extra_premium - charged.extra_premium == pytest.approx(10.0)


def test_the_ratio_and_extra_limits_are_tunable() -> None:
    put = _open("X", 130.0, 25, 0.062, 145.0)
    assert review(put, _pick("X", FRESH_YIELD), params=SwapParams(min_ratio=4.0)).action is (
        SwapAction.KEEP)
    assert review(put, _pick("X", FRESH_YIELD), params=SwapParams(min_extra=500.0)).action is (
        SwapAction.KEEP)


# --- the fresh put ---------------------------------------------------------------------------

def test_without_a_same_ticker_pick_the_list_median_stands_in() -> None:
    """Removing TER from the picks falls back to the median — never the best of the list, which
    would fire a swap constantly."""
    yields = [0.10, 0.219, 0.90]
    r = review(_open("TER", 290.0, 25, 0.069, 320.0), None, list_median=list_median_yield(yields))
    assert r.fresh_yield == pytest.approx(0.219) and r.fresh_source == "list median"
    assert r.action is SwapAction.SWAP


def test_nothing_to_swap_into_means_keep() -> None:
    r = review(_open("X", 100.0, 30, 0.02, 120.0), None, list_median=None)
    assert r.action is SwapAction.KEEP and "nothing passes the entry rules" in r.reason


def test_a_swap_suggests_the_same_ticker_first_then_the_best_of_the_rest() -> None:
    others = [_pick("AAA", 0.40), _pick("BBB", 0.35), _pick("CCC", 0.30), _pick("DDD", 0.25)]
    r = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", FRESH_YIELD), others)
    assert [s.symbol for s in r.suggestions] == ["X", "AAA", "BBB", "CCC"]  # TOP_N = 3 others
    assert r.suggestions[0].same_ticker


def test_scope_and_pricing_guards() -> None:
    assert review(OpenPut("X", 100.0, 30, 1, None, 1.0), _pick("X", 0.5)).action is (
        SwapAction.NOT_APPLICABLE)  # no share price
    assert review(OpenPut("X", 100.0, 0, 1, 120.0, 1.0), _pick("X", 0.5)).action is (
        SwapAction.NOT_APPLICABLE)  # expires today
    no_ask = review(OpenPut("X", 100.0, 30, 1, 120.0, None), _pick("X", 0.5))
    assert no_ask.action is SwapAction.NOT_APPLICABLE and "ask" in no_ask.reason


# --- covered calls: are the shares still earning? --------------------------------------------

def _call(strike: float, days: int, spot: float, price: float, contracts: float = 1) -> OpenCall:
    return OpenCall(symbol="X", strike=strike, days=days, contracts=contracts, spot=spot,
                    price=price)


def test_a_call_decayed_to_nothing_says_the_shares_are_idle() -> None:
    """$120 call, stock at $110, 5c of premium left over 25 days: the shares are working for
    about 0.7%/yr. Reported as a fact, with no strike recommended."""
    r = review_covered_call(_call(120.0, 25, 110.0, 0.05))
    assert r.action is SwapAction.IDLE and r.used_up
    assert r.old_yield == pytest.approx(0.05 / 110 * 365 / 25, abs=1e-4)
    assert r.cash == pytest.approx(11_000.0)  # what the shares are worth, not the strike
    assert "earning almost nothing" in r.reason
    assert r.suggestions == []  # nothing is suggested: closing a call frees no capital


def test_a_call_still_paying_is_left_alone() -> None:
    r = review_covered_call(_call(120.0, 25, 110.0, 1.50))  # ~20%/yr on the shares
    assert r.action is SwapAction.KEEP and r.used_up is False
    assert "still earning" in r.reason


def test_a_call_in_the_money_is_the_called_away_question_instead() -> None:
    r = review_covered_call(_call(120.0, 25, 125.0, 5.40))
    assert r.action is SwapAction.NOT_APPLICABLE and "called-away" in r.reason


def test_a_call_needs_a_share_price_and_a_mark() -> None:
    assert review_covered_call(_call(120.0, 25, None, 0.05)).action is SwapAction.NOT_APPLICABLE
    no_mark = review_covered_call(_call(120.0, 25, 110.0, None))
    assert no_mark.action is SwapAction.NOT_APPLICABLE and "no price" in no_mark.reason
    assert review_covered_call(_call(120.0, 0, 110.0, 0.05)).action is SwapAction.NOT_APPLICABLE


def test_the_call_floor_is_the_same_setting_as_the_put_one() -> None:
    quiet = _call(120.0, 25, 110.0, 0.30)  # ~4%/yr
    assert review_covered_call(quiet).action is SwapAction.IDLE
    assert review_covered_call(quiet, SwapParams(used_up_yield=0.02)).action is SwapAction.KEEP
