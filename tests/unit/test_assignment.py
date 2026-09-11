"""Early assignment: exercise happens when it gains the holder more than the time value left."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wheel_screener.core.assignment import NO_TIME_VALUE, assess, interest_on_strike
from wheel_screener.core.models import AssignmentCause, AssignmentRisk, Dividend, OptionType

TODAY = date(2026, 9, 11)
EXPIRY = TODAY + timedelta(days=35)
CALL, PUT = OptionType.CALL, OptionType.PUT


def _div(days: int, amount: float = 0.71, estimated: bool = False) -> Dividend:
    return Dividend(ex_date=TODAY + timedelta(days=days), amount=amount,
                    frequency="quarterly", estimated=estimated)


def test_interest_on_the_strike() -> None:
    """$100 of strike cash for 30 days at 4% earns about 33 cents a share."""
    assert interest_on_strike(100.0, 30, 0.04) == pytest.approx(0.3293, abs=1e-3)
    assert interest_on_strike(100.0, 0, 0.04) == 0.0


def test_no_spot_no_verdict() -> None:
    assert assess(PUT, 50.0, None, 1.0, EXPIRY, TODAY) is None


# --- calls: the dividend ---------------------------------------------------------------------

def test_an_in_the_money_call_worth_less_than_the_dividend_is_exercised_the_day_before() -> None:
    """VZ-like: $50 call, stock $52, $2.13 bid = $0.13 of time value against a $0.71 dividend."""
    ea = assess(CALL, 50.0, 52.0, 2.13, EXPIRY, TODAY, [_div(28)])
    assert ea.risk is AssignmentRisk.LIKELY and ea.cause is AssignmentCause.DIVIDEND
    assert ea.on == TODAY + timedelta(days=27)  # the eve of the ex-date
    assert ea.time_value == pytest.approx(0.13) and ea.threshold == pytest.approx(0.71)
    assert ea.in_the_money and ea.intrinsic == pytest.approx(2.0)


def test_a_call_with_time_value_near_the_dividend_is_one_to_watch() -> None:
    ea = assess(CALL, 50.0, 52.0, 3.00, EXPIRY, TODAY, [_div(28)])  # $1.00 vs $0.71
    assert ea.risk is AssignmentRisk.POSSIBLE and ea.cause is AssignmentCause.DIVIDEND
    ea = assess(CALL, 50.0, 52.0, 4.00, EXPIRY, TODAY, [_div(28)])  # $2.00: well clear
    assert ea.risk is AssignmentRisk.LOW and ea.threshold == pytest.approx(0.71)


def test_without_a_dividend_a_call_is_not_exercised_early() -> None:
    """Merton: exercising a call early on a non-payer only throws the time value away."""
    ea = assess(CALL, 50.0, 55.0, 5.30, EXPIRY, TODAY, [])
    assert ea.risk is AssignmentRisk.LOW and ea.cause is None


def test_a_dividend_after_expiry_does_not_count() -> None:
    ea = assess(CALL, 50.0, 52.0, 2.13, EXPIRY, TODAY, [_div(40)])
    assert ea.risk is AssignmentRisk.LOW and ea.dividends == []


def test_an_out_of_the_money_call_carries_the_dividend_it_would_face() -> None:
    ea = assess(CALL, 55.0, 52.0, 0.40, EXPIRY, TODAY, [_div(28)])
    assert ea.risk is AssignmentRisk.LOW and not ea.in_the_money
    assert ea.threshold == pytest.approx(0.71) and ea.on == TODAY + timedelta(days=27)


# --- puts: interest on the strike ------------------------------------------------------------

def test_a_deep_put_worth_less_than_the_interest_on_its_strike_is_exercised() -> None:
    """$100 put, stock $80, 35 days: the strike earns ~$0.38; $0.20 of time value is less."""
    ea = assess(PUT, 100.0, 80.0, 20.20, EXPIRY, TODAY, [])
    assert ea.risk is AssignmentRisk.LIKELY and ea.cause is AssignmentCause.INTEREST
    assert ea.threshold == pytest.approx(interest_on_strike(100.0, 35, 0.04))
    assert ea.on is None  # any day


def test_a_put_with_time_value_near_the_interest_is_one_to_watch() -> None:
    carry = interest_on_strike(100.0, 35, 0.04)
    assert assess(PUT, 100.0, 80.0, 20 + 1.5 * carry, EXPIRY, TODAY).risk is (
        AssignmentRisk.POSSIBLE)
    assert assess(PUT, 100.0, 80.0, 20 + 3 * carry, EXPIRY, TODAY).risk is AssignmentRisk.LOW


def test_a_higher_rate_makes_early_exercise_likelier() -> None:
    assert assess(PUT, 100.0, 80.0, 20.60, EXPIRY, TODAY, rate=0.04).risk is (
        AssignmentRisk.POSSIBLE)
    assert assess(PUT, 100.0, 80.0, 20.60, EXPIRY, TODAY, rate=0.08).risk is (
        AssignmentRisk.LIKELY)


def test_a_dividend_ahead_defers_a_puts_exercise_until_it_has_passed() -> None:
    """Holding through the ex-date gains the drop, so a holder waits for it — the verdict moves
    to the ex-date instead of reading as any day now."""
    ea = assess(PUT, 100.0, 80.0, 20.10, EXPIRY, TODAY, [_div(10, 0.50)])
    assert ea.risk is AssignmentRisk.POSSIBLE and ea.deferred
    assert ea.on == TODAY + timedelta(days=10)
    assert ea.threshold == pytest.approx(interest_on_strike(100.0, 25, 0.04))
    roomy = assess(PUT, 100.0, 80.0, 22.00, EXPIRY, TODAY, [_div(10, 0.50)])
    assert roomy.risk is AssignmentRisk.LOW and roomy.deferred


# --- either side -----------------------------------------------------------------------------

def test_no_time_value_left_means_exercise_any_day() -> None:
    ea = assess(CALL, 50.0, 60.0, 10.0 + NO_TIME_VALUE / 2, EXPIRY, TODAY, [])
    assert ea.risk is AssignmentRisk.LIKELY and ea.cause is AssignmentCause.NO_TIME_VALUE
    ea = assess(PUT, 100.0, 80.0, 20.0, TODAY + timedelta(days=2), TODAY, [])
    assert ea.risk is AssignmentRisk.LIKELY  # at parity: nothing lost by exercising


def test_out_of_the_money_is_never_exercised() -> None:
    ea = assess(PUT, 90.0, 100.0, 0.50, EXPIRY, TODAY, [])
    assert ea.risk is AssignmentRisk.LOW and not ea.in_the_money
    assert ea.time_value == pytest.approx(0.50)


def test_in_the_money_without_a_quote_is_unknown_not_safe() -> None:
    ea = assess(PUT, 100.0, 80.0, None, EXPIRY, TODAY, [])
    assert ea.risk is AssignmentRisk.UNKNOWN and ea.in_the_money and ea.time_value is None


def test_a_positions_mark_comes_from_its_market_value_and_zero_means_none() -> None:
    from wheel_screener.core.models import Position, PositionKind

    def short_call(value):
        return Position(symbol="VZ x", underlying="VZ", kind=PositionKind.SHORT_CALL,
                        option_type=CALL, quantity=2, strike=50.0, market_value=value)

    assert short_call(-426.0).mark == pytest.approx(2.13)  # |value| / 100 / contracts
    assert short_call(0.0).mark is None and short_call(None).mark is None
    shares = Position(symbol="VZ", underlying="VZ", kind=PositionKind.SHARES, quantity=200,
                      market_value=10_400.0)
    assert shares.mark is None
