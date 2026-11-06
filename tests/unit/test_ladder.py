"""The covered-call ladder (core.ladder): the orders, the rungs and the report's calendar, on the
TER plan the tab was designed with — 100 shares assigned at $390, a $340 call sold for $6."""

from __future__ import annotations

from datetime import date

import pytest

from wheel_screener.core import ladder as L
from wheel_screener.core.models import Dividend, OptionContract, OptionType

TODAY = date(2026, 11, 3)
DEC, JAN, FEB = date(2026, 12, 18), date(2027, 1, 15), date(2027, 2, 19)
REPORT = date(2027, 1, 27)
RUNGS = (340.0, 355.0, 370.0, 390.0)
EXPIRIES = (date(2026, 11, 20), DEC, JAN, FEB, date(2027, 3, 19))
STRIKES = tuple(range(250, 455, 5))


def _chain(spot: float = 269.28, iv: float = 0.68, today: date = TODAY,
           expiries=EXPIRIES, strikes=STRIKES) -> list[OptionContract]:
    """A chain priced by the model itself, 30 cents either side of the mid."""
    out = []
    for e in expiries:
        for k in strikes:
            mid = L.call_value(spot, k, (e - today).days, iv)
            out.append(OptionContract(
                underlying_symbol="TER", option_symbol=f"TER{e:%y%m%d}C{k}", strike=k,
                option_type=OptionType.CALL, expiration=e, dte=(e - today).days,
                bid=max(0.0, mid - 0.30), ask=mid + 0.30, mid=mid, implied_volatility=iv))
    return out


def _market(spot: float = 269.28, report: date | None = REPORT, **kw) -> L.Market:
    return L.Market(spot=spot, calls=_chain(spot=spot, **kw), next_earnings=report)


def _plan(**kw) -> L.Plan:
    fields = dict(symbol="TER", shares=100, cost=390.0, rungs=RUNGS, put_premium=0.0,
                  call_premiums=6.0, call_strike=340.0, call_expiry=DEC, call_sold_for=6.0)
    return L.Plan(**{**fields, **kw})


def test_black_scholes_matches_the_textbook_value() -> None:
    # S = K = 100, a year, 20% volatility, 4%: d1 = 0.3, d2 = 0.1
    assert L.call_value(100, 100, 365, 0.20, 0.04) == pytest.approx(9.925, abs=0.01)
    assert L.call_value(120, 100, 0, 0.20) == 20.0  # at expiry: what it is in the money


def test_an_order_line_reads_as_a_ticket_does() -> None:
    assert L.leg("BUY TO CLOSE", 1, "TER", DEC, 340.0) == "BUY TO CLOSE 1 TER 18 DEC 26 340 CALL"
    assert L.leg("SELL TO OPEN", 2, "KGC", JAN, 30.5) == "SELL TO OPEN 2 KGC 15 JAN 27 30.5 CALL"


def test_real_cost_is_the_cost_less_every_premium() -> None:
    plan = _plan(put_premium=8.4, call_premiums=6.0)
    assert plan.real_cost == pytest.approx(375.6)
    assert plan.contracts == 1 and _plan(shares=250).contracts == 2


def test_the_ter_plan_places_a_take_profit_and_a_roll_to_january() -> None:
    view = L.build(_plan(), _market(), TODAY)
    assert view.status == "hold"
    assert view.take_profit == 1.50  # 25% of the $6.00 it was sold for
    roll = view.roll
    assert roll.blocked is None
    assert roll.trigger == 329.80  # 97% of the strike
    # four weeks after Dec 18 is Jan 15, the last expiry before the Jan 27 report
    assert (roll.to_strike, roll.to_expiry, roll.added_days) == (355.0, JAN, 28)
    assert view.wall == JAN
    assert roll.lines == ("BUY TO CLOSE 1 TER 18 DEC 26 340 CALL",
                          "SELL TO OPEN 1 TER 15 JAN 27 355 CALL")
    # 85% of the model's worst credit at the trigger, at volatility 10 points lower
    assert roll.limit == 1.75
    # past about $388 with 10 days left, the roll no longer pays the limit
    assert roll.gap == pytest.approx(388.45, abs=1.0)
    assert roll.credit_mid is not None and roll.credit_natural == pytest.approx(
        roll.credit_mid - 0.60, abs=0.01)


def test_each_rung_says_where_it_rolls_and_what_being_called_there_means() -> None:
    view = L.build(_plan(), _market(), TODAY)
    assert [(r.strike, r.state) for r in view.rungs] == [
        (340, "open"), (355, "ahead"), (370, "ahead"), (390, "target")]
    assert [r.trigger for r in view.rungs] == [329.80, 344.35, 358.90, None]
    assert [r.if_called for r in view.rungs] == [-44.0, -29.0, -14.0, 6.0]
    assert view.rungs[0].credit == pytest.approx(8.85, abs=0.01)


def test_the_calendar_runs_to_the_report_and_past_it() -> None:
    view = L.build(_plan(), _market(), TODAY)
    assert [(e.day, e.kind) for e in view.events] == [
        (TODAY, "now"), (DEC, "expiry"), (JAN, "wall"), (REPORT, "report"),
        (date(2027, 1, 28), "resume")]
    # if December's call expires, January's has four weeks to the wall
    nxt = view.next_call
    assert (nxt.strike, nxt.expiry, nxt.estimated) == (340.0, JAN, True)
    assert nxt.premium == pytest.approx(3.04, abs=0.05)


def test_the_call_before_the_report_can_not_roll() -> None:
    view = L.build(_plan(call_strike=355.0, call_expiry=JAN), _market(), TODAY)
    assert view.roll.blocked == "report"
    assert view.grid is None
    expiry = next(e for e in view.events if e.kind == "expiry")
    assert expiry.day == JAN and expiry.wall


def test_a_call_open_over_the_report_is_flagged() -> None:
    view = L.build(_plan(call_expiry=FEB), _market(), TODAY)
    assert view.status == "spans"


def test_with_no_call_open_it_suggests_the_lowest_rung_above_the_price() -> None:
    view = L.build(_plan(call_strike=None, call_expiry=None, call_sold_for=None), _market(),
                   TODAY)
    assert view.status == "sell"
    new = view.new_call
    assert (new.strike, new.expiry, new.estimated) == (340.0, DEC, False)
    assert new.line == "SELL TO OPEN 1 TER 18 DEC 26 340 CALL"
    # with TER at $345, the $340 rung would be in the money: the next one up
    above = L.build(_plan(call_strike=None, call_expiry=None), _market(spot=345.0), TODAY)
    assert above.new_call.strike == 355.0


def test_a_call_that_pays_too_little_waits() -> None:
    view = L.build(_plan(call_strike=None, call_expiry=None), _market(spot=200.0), TODAY)
    assert view.status == "thin" and not view.new_call.sells


def test_at_the_target_there_is_nothing_to_roll() -> None:
    view = L.build(_plan(call_strike=390.0), _market(), TODAY)
    assert view.status == "target" and view.roll.blocked == "target"


def test_a_cheap_call_is_a_take_profit() -> None:
    view = L.build(_plan(), _market(spot=215.0), TODAY)
    assert view.quote.ask <= 1.50 and view.status == "take-profit"


def test_near_the_strike_it_is_in_the_roll_zone() -> None:
    assert L.build(_plan(), _market(spot=332.0), TODAY).status == "roll-zone"


def test_an_expired_call_asks_for_the_next() -> None:
    view = L.build(_plan(call_expiry=date(2026, 10, 30)), _market(), TODAY)
    assert view.status == "expired" and view.roll is None


def test_without_a_report_date_it_says_so_and_rolls_freely() -> None:
    view = L.build(_plan(), _market(report=None), TODAY)
    assert "no-report" in view.notes and view.wall is None
    assert view.roll.to_expiry == JAN  # the shortest four to six weeks on


def test_a_strike_the_chain_does_not_list_is_noted() -> None:
    view = L.build(_plan(call_strike=342.0), _market(), TODAY)
    assert "no-quote" in view.notes and view.quote is None


def test_a_dividend_inside_the_call_is_on_the_calendar() -> None:
    market = L.Market(spot=269.28, calls=_chain(), next_earnings=REPORT, dividends_known=True,
                      dividends=[Dividend(ex_date=date(2026, 12, 4), amount=0.13,
                                          estimated=True)])
    view = L.build(_plan(), market, TODAY)
    div = next(e for e in view.events if e.kind == "dividend")
    assert div.day == date(2026, 12, 4) and div.dividend.amount == 0.13
    assert view.early is not None and view.early.risk.value == "low"


def test_the_roll_table_brackets_the_trigger() -> None:
    grid = L.build(_plan(), _market(), TODAY).grid
    assert grid.prices[:4] == [310.0, 320.0, 329.80, 340.0]
    assert grid.days_left == [45, 30, 20, 10, 5, 1]
    trigger_row = grid.cells[grid.prices.index(329.80)]
    assert min(trigger_row) >= grid.limit  # at the trigger, the limit fills all cycle long
    assert grid.cells[-1][0] < grid.limit  # $50 past the strike, it doesn't


def test_the_minimums_scale_with_the_price() -> None:
    # the TER plan's $3.00 at $300 and $0.50 on the $340, as shares of the price
    assert L.min_premium(300) == 3.0 and L.min_roll_credit(340) == 0.51
    # a $31 stock's $33 call isn't held to TER's $3.00
    view = L.build(_plan(symbol="KGC", cost=36.0, rungs=(33.0, 34.0, 35.0, 36.0),
                         call_strike=None, call_expiry=None),
                   L.Market(spot=31.40, calls=_chain(spot=31.40, iv=0.40,
                                                     strikes=[x / 2 for x in range(50, 90)]),
                            next_earnings=date(2027, 2, 11)), TODAY)
    assert view.new_call.strike == 33.0 and view.new_call.minimum == 0.31
    assert view.status == "sell"
