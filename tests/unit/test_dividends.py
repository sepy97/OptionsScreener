"""The ex-dividend flag: which dates a contract lives through, and what they do to it."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wheel_screener.core.dividends import impact, in_life, upcoming
from wheel_screener.core.models import Dividend, OptionType

TODAY = date(2026, 9, 11)


def _div(days: int, amount: float = 0.71, freq: str | None = "quarterly") -> Dividend:
    return Dividend(ex_date=TODAY + timedelta(days=days), amount=amount, frequency=freq)


# --- which dates a contract lives through ---------------------------------------------------

def test_an_announced_ex_date_before_expiry_is_in_the_life() -> None:
    history = [_div(-91), _div(28)]  # VZ: last paid in July, Oct 9 announced
    got = in_life(history, TODAY, TODAY + timedelta(days=35))
    assert [d.ex_date for d in got] == [TODAY + timedelta(days=28)]
    assert not got[0].estimated


def test_the_expiry_day_itself_counts_and_today_does_not() -> None:
    """The stock opens ex-dividend on the expiry morning and settles against that price; an
    ex-date of today is already in the price the screen saw."""
    expiry = TODAY + timedelta(days=28)
    assert in_life([_div(-91), _div(28)], TODAY, expiry)  # on expiry -> in
    assert in_life([_div(-91), _div(28)], TODAY, expiry - timedelta(days=1)) == []  # after -> out
    today_ex = [_div(-91), _div(0)]
    assert all(d.ex_date > TODAY for d in in_life(today_ex, TODAY, expiry))


def test_a_regular_payer_projects_its_next_date_when_none_is_announced() -> None:
    """SPY's shape: paid quarterly, next date not announced yet. Leaving it out would read as "no
    dividend" for exactly the ex-dates furthest out, which are the unannounced ones."""
    history = [_div(-182, 1.80), _div(-85, 1.90)]  # last ex-date 85 days ago
    got = in_life(history, TODAY, TODAY + timedelta(days=35))
    assert len(got) == 1
    assert got[0].estimated and got[0].ex_date == TODAY + timedelta(days=6)  # -85 + 91
    assert got[0].amount == 1.90  # the last payment stands in for the next


def test_no_projection_past_an_announced_date() -> None:
    """An announced date IS the next one — projecting from the previous would double-count."""
    history = [_div(-91), _div(28)]
    got = upcoming(history, TODAY, TODAY + timedelta(days=100))
    assert [(d.ex_date - TODAY).days for d in got] == [28]  # next projection lands at 119


def test_a_lapsed_schedule_is_not_projected() -> None:
    """Six months since a quarterly payment: most likely suspended. Projecting it would invent
    money that is not coming."""
    assert in_life([_div(-400), _div(-180)], TODAY, TODAY + timedelta(days=45)) == []


def test_an_overdue_payment_is_due_now_not_skipped() -> None:
    """Past its usual date but still inside the schedule: it is coming, so it lands inside
    every contract rather than being pushed a full quarter out."""
    got = in_life([_div(-100)], TODAY, TODAY + timedelta(days=14))
    assert len(got) == 1 and got[0].estimated and got[0].ex_date == TODAY + timedelta(days=1)


def test_a_monthly_payer_can_go_ex_twice_inside_one_contract() -> None:
    history = [_div(-10, 0.27, "monthly")]
    got = in_life(history, TODAY, TODAY + timedelta(days=45))
    assert [(d.ex_date - TODAY).days for d in got] == [20]
    got = in_life(history, TODAY, TODAY + timedelta(days=55))
    assert [(d.ex_date - TODAY).days for d in got] == [20, 50]


def test_specials_show_when_announced_but_are_never_projected() -> None:
    history = [_div(-60), _div(-20, 3.00, "special"), _div(10, 0.30, "irregular")]
    got = in_life(history, TODAY, TODAY + timedelta(days=45))
    assert [(d.frequency, d.estimated, (d.ex_date - TODAY).days) for d in got] == [
        ("irregular", False, 10),  # announced -> shown
        ("quarterly", True, 31),  # the regular schedule, projected from 60 days ago
    ]  # and nothing projected from the special


def test_a_non_payer_has_nothing() -> None:
    assert in_life([], TODAY, TODAY + timedelta(days=45)) == []


# --- what the dividend does to the contract -------------------------------------------------

def test_a_put_keeps_its_value_but_loses_cushion() -> None:
    """VZ-like: $0.71 ex-dividend, $47.40 strike at $50.59. The cushion against the price the
    stock trades at after the dividend is what assignment is judged on."""
    d = impact([_div(28)], OptionType.PUT, strike=47.40, spot=50.59, premium=0.39)
    assert d is not None and d.count == 1 and not d.estimated
    assert d.cushion_now == pytest.approx((50.59 - 47.40) / 50.59)
    assert d.cushion_after == pytest.approx((50.59 - 0.71 - 47.40) / (50.59 - 0.71))
    assert d.cushion_after < d.cushion_now
    assert d.pct_of_spot == pytest.approx(0.71 / 50.59)
    assert d.per_contract == pytest.approx(71.0)
    assert d.exercise_eve is None and not d.early_assignment_likely  # a put-only concern: none


def test_an_in_the_money_call_with_less_time_value_than_the_dividend_is_flagged() -> None:
    """The textbook case: a $50 call at $52 with $0.13 of time value against a $0.71 dividend
    gets exercised the day before the ex-date."""
    d = impact([_div(28)], OptionType.CALL, strike=50.0, spot=52.0, premium=2.13)
    assert d.exercise_eve == TODAY + timedelta(days=27)
    assert d.time_value == pytest.approx(0.13)
    assert d.early_assignment_likely


def test_an_out_of_the_money_call_is_not_likely_assigned_early() -> None:
    d = impact([_div(28)], OptionType.CALL, strike=55.0, spot=52.0, premium=0.40)
    assert d.time_value == pytest.approx(0.40) and not d.early_assignment_likely
    assert d.cushion_after > d.cushion_now  # the drop moves the stock away from a call strike


def test_several_ex_dates_are_totalled_and_an_estimate_marks_the_whole() -> None:
    divs = [_div(5, 0.27, "monthly"), _div(35, 0.27, "monthly").model_copy(
        update={"estimated": True})]
    d = impact(divs, OptionType.PUT, strike=50.0, spot=55.0)
    assert d.count == 2 and d.total == pytest.approx(0.54) and d.estimated
    assert d.first == TODAY + timedelta(days=5) and d.first_amount == pytest.approx(0.27)


def test_no_spot_still_describes_the_dividend_without_cushion_numbers() -> None:
    d = impact([_div(28)], OptionType.PUT, strike=47.40, spot=None)
    assert d.total == pytest.approx(0.71)
    assert d.cushion_now is None and d.cushion_after is None and d.pct_of_spot is None


def test_no_dividends_no_impact() -> None:
    assert impact([], OptionType.PUT, strike=50.0, spot=55.0) is None
