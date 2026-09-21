"""The put swap rule — checked against the worked example in the spec (docs/PUT_SWAP_RULE.md)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wheel_screener.core.models import SwapAction, SwapSuggestion
from wheel_screener.core.swap import OpenPut, SwapParams, list_median_yield, put_yield, review

TODAY = date(2026, 9, 21)
YARDSTICK = 0.219  # the spec's example: the average yield of the three puts opened that day


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
    ("symbol", "strike", "days", "old_yield", "spot", "action", "extra"),
    [
        ("CRDO", 130.0, 25, 0.062, 145.0, SwapAction.SWAP, 130.0),
        ("TER", 290.0, 25, 0.069, 320.0, SwapAction.SWAP, 288.0),
        ("SCCO", 170.0, 25, 0.125, 180.0, SwapAction.KEEP, None),   # rule 1
        ("LRCX", 270.0, 4, 0.184, 290.0, SwapAction.KEEP, None),    # rule 1
    ],
)
def test_the_worked_example(symbol, strike, days, old_yield, spot, action, extra) -> None:
    r = review(_open(symbol, strike, days, old_yield, spot), _pick(symbol, YARDSTICK))
    assert r.action is action, r.reason
    assert r.old_yield == pytest.approx(old_yield, abs=5e-4)
    if extra is not None:
        assert r.extra_premium == pytest.approx(extra, abs=1.0)
    else:
        assert "rule 1" in r.reason  # both keeps in the example fail on the ratio


def test_a_put_the_stock_has_fallen_below_is_the_assignment_question() -> None:
    """AVGO in the example: out of scope, and for a reason worth saying rather than a bare No."""
    r = review(_open("AVGO", 390.0, 4, 0.05, 380.0), _pick("AVGO", YARDSTICK))
    assert r.action is SwapAction.NOT_APPLICABLE and "assignment" in r.reason


# --- the two rules ---------------------------------------------------------------------------

def test_rule_1_stops_a_swap_right_after_opening() -> None:
    """On day one a put pays about what a fresh one pays, so nothing can be twice as good."""
    fresh = review(_open("X", 100.0, 30, YARDSTICK, 110.0), _pick("X", YARDSTICK))
    assert fresh.action is SwapAction.KEEP and fresh.rule1_passed is False
    assert "1.0x" in fresh.reason


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
    free = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", YARDSTICK),
                  params=SwapParams(swap_cost=0.0))
    charged = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", YARDSTICK),
                     params=SwapParams(swap_cost=10.0))
    assert free.extra_premium - charged.extra_premium == pytest.approx(10.0)


def test_the_limits_are_tunable() -> None:
    put = _open("X", 130.0, 25, 0.062, 145.0)
    assert review(put, _pick("X", YARDSTICK), params=SwapParams(min_ratio=4.0)).action is (
        SwapAction.KEEP)
    assert review(put, _pick("X", YARDSTICK), params=SwapParams(min_extra=500.0)).action is (
        SwapAction.KEEP)


# --- the yardstick ---------------------------------------------------------------------------

def test_without_a_same_ticker_pick_the_list_median_stands_in() -> None:
    """Removing TER from the picks falls back to the median — never the best of the list, which
    would fire a swap constantly."""
    yields = [0.10, 0.219, 0.90]
    r = review(_open("TER", 290.0, 25, 0.069, 320.0), None, list_median=list_median_yield(yields))
    assert r.yardstick == pytest.approx(0.219) and r.yardstick_source == "list median"
    assert r.action is SwapAction.SWAP


def test_nothing_to_swap_into_means_keep() -> None:
    r = review(_open("X", 100.0, 30, 0.02, 120.0), None, list_median=None)
    assert r.action is SwapAction.KEEP and "nothing passes the entry rules" in r.reason


def test_a_swap_suggests_the_same_ticker_first_then_the_best_of_the_rest() -> None:
    others = [_pick("AAA", 0.40), _pick("BBB", 0.35), _pick("CCC", 0.30), _pick("DDD", 0.25)]
    r = review(_open("X", 130.0, 25, 0.062, 145.0), _pick("X", YARDSTICK), others)
    assert [s.symbol for s in r.suggestions] == ["X", "AAA", "BBB", "CCC"]  # TOP_N = 3 others
    assert r.suggestions[0].same_ticker


def test_scope_and_pricing_guards() -> None:
    assert review(OpenPut("X", 100.0, 30, 1, None, 1.0), _pick("X", 0.5)).action is (
        SwapAction.NOT_APPLICABLE)  # no share price
    assert review(OpenPut("X", 100.0, 0, 1, 120.0, 1.0), _pick("X", 0.5)).action is (
        SwapAction.NOT_APPLICABLE)  # expires today
    no_ask = review(OpenPut("X", 100.0, 30, 1, 120.0, None), _pick("X", 0.5))
    assert no_ask.action is SwapAction.NOT_APPLICABLE and "ask" in no_ask.reason
