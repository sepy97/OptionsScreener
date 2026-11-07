"""The covered-call ladder (core.ladder): the orders, the rungs and the plan through the reports,
on the TER plan the ladder was designed with — 100 shares assigned at $390, a $340 call sold for
$6 — and which holdings need one."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wheel_screener.core import ladder as L
from wheel_screener.core.models import (
    Dividend,
    OptionContract,
    OptionType,
    Position,
    PositionKind,
)

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
    fields = dict(symbol="TER", shares=100, cost=390.0, rungs=RUNGS, premiums=0.0,
                  call_premiums=6.0, call_strike=340.0, call_expiry=DEC, call_sold_for=6.0)
    return L.Plan(**{**fields, **kw})


NO_CALL = dict(call_strike=None, call_expiry=None, call_sold_for=None, call_premiums=0.0)


def test_black_scholes_matches_the_textbook_value() -> None:
    # S = K = 100, a year, 20% volatility, 4%: d1 = 0.3, d2 = 0.1
    assert L.call_value(100, 100, 365, 0.20, 0.04) == pytest.approx(9.925, abs=0.01)
    assert L.call_value(120, 100, 0, 0.20) == 20.0  # at expiry: what it is in the money


def test_an_order_line_reads_as_a_ticket_does() -> None:
    assert L.leg("BUY TO CLOSE", 1, "TER", DEC, 340.0) == "BUY TO CLOSE 1 TER 18 DEC 26 340 CALL"
    assert L.leg("SELL TO OPEN", 2, "KGC", JAN, 30.5) == "SELL TO OPEN 2 KGC 15 JAN 27 30.5 CALL"


def test_real_cost_is_the_cost_less_every_premium() -> None:
    plan = _plan(premiums=8.4, call_premiums=6.0)
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


RUNGS4 = (340.0, 355.0, 375.0, 390.0)


def test_on_a_steady_climb_the_plan_runs_until_the_shares_are_called() -> None:
    # TER 2% a month, at 45% volatility: $340 calls until it reaches the $329.80 trigger, then a
    # rung a quarter or so, a report between each roll and the next
    view = L.build(_plan(rungs=RUNGS4, growth=0.02), _market(iv=0.45), TODAY)
    plan = view.projection
    assert [(x.how, x.strike) for x in plan.stretches] == [
        ("open", 340), ("sell", 340), ("roll", 355), ("sell", 355), ("roll", 375),
        ("sell", 375), ("roll", 390), ("sell", 390), ("called", 390)]
    sells = plan.stretches[1]
    assert (sells.start, sells.end, sells.calls) == (date(2027, 1, 28), date(2027, 9, 17), 5)
    assert sells.reports == 3 and sells.skipped == 8  # under the minimum, it waits
    roll = plan.stretches[2]
    assert (roll.start, roll.end, roll.trigger, roll.from_strike) == (
        date(2027, 9, 11), date(2027, 10, 15), 329.80, 340)
    assert roll.spot >= roll.trigger and roll.credit > L.min_roll_credit(340)
    assert plan.called.start == date(2028, 6, 16) and plan.called.spot > 390
    assert plan.premiums > 0 and plan.real_cost == pytest.approx(
        _plan().real_cost - plan.premiums, abs=0.02)


def test_the_plan_never_steps_down_or_sells_into_a_roll_zone() -> None:
    for growth in (0.005, 0.01, 0.03):
        plan = L.build(_plan(rungs=RUNGS4, growth=growth), _market(), TODAY).projection
        strikes = [x.strike for x in plan.stretches]
        assert strikes == sorted(strikes)
        for x in plan.stretches:
            if x.how == "sell" and x.strike != 390:
                assert x.spot < L.ROLL_TRIGGER * x.strike


def test_flat_shares_sell_the_first_rung_until_the_plan_stops() -> None:
    plan = L.build(_plan(rungs=RUNGS4, growth=0.0), _market(), TODAY).projection
    assert [(x.how, x.strike) for x in plan.stretches] == [
        ("open", 340), ("sell", 340), ("end", 340)]
    assert plan.called is None and plan.stretches[1].reports == 15  # four years of them


def test_with_no_call_the_plan_starts_with_the_one_to_sell() -> None:
    view = L.build(_plan(rungs=(355.0, 370.0, 390.0), growth=0.02, **NO_CALL),
                   _market(iv=0.45), TODAY)
    assert [(x.how, x.strike) for x in view.projection.stretches] == [
        ("sell", 355), ("roll", 370), ("sell", 390), ("called", 390)]


def test_reports_past_the_next_are_a_quarter_apart() -> None:
    assert L._reports(REPORT, TODAY)[:3] == [REPORT, date(2027, 4, 28), date(2027, 7, 28)]
    assert L._reports(None, TODAY) == []


def test_expiries_to_come_are_the_usual_fridays() -> None:
    monthly = L._ahead(list(EXPIRIES), TODAY)
    assert date(2027, 6, 18) in monthly and date(2027, 6, 11) not in monthly
    weeklies = [date(2026, 11, 6) + timedelta(days=7 * i) for i in range(8)]
    weekly = L._ahead(weeklies, TODAY)
    assert date(2027, 6, 11) in weekly and date(2027, 6, 18) in weekly


def test_without_volatility_there_is_no_plan() -> None:
    chain = [c.model_copy(update={"implied_volatility": None}) for c in _chain()]
    view = L.build(_plan(), L.Market(spot=269.28, calls=chain, next_earnings=REPORT), TODAY)
    assert view.projection is None


def test_the_plan_takes_one_percent_a_month_unless_told() -> None:
    assert _plan().monthly_growth == L.DEFAULT_GROWTH == 0.01
    assert _plan(growth=0.0).monthly_growth == 0.0


def test_if_the_open_call_expires_the_next_has_four_weeks_to_the_wall() -> None:
    view = L.build(_plan(), _market(), TODAY)
    # if December's call expires, January's has four weeks to the wall
    nxt = view.next_call
    assert (nxt.strike, nxt.expiry, nxt.estimated) == (340.0, JAN, True)
    assert nxt.premium == pytest.approx(3.04, abs=0.05)


def test_the_call_before_the_report_can_not_roll() -> None:
    view = L.build(_plan(call_strike=355.0, call_expiry=JAN), _market(), TODAY)
    assert view.roll.blocked == "report"


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
    # with TER at $345, the $340 rung would be in the money, and the $355 past its $344.35
    # trigger, in its roll zone already: the next one up
    above = L.build(_plan(call_strike=None, call_expiry=None), _market(spot=345.0), TODAY)
    assert above.new_call.strike == 370.0


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


def test_a_dividend_inside_the_call_is_on_the_plan() -> None:
    market = L.Market(spot=269.28, calls=_chain(), next_earnings=REPORT, dividends_known=True,
                      dividends=[Dividend(ex_date=date(2026, 12, 4), amount=0.13,
                                          estimated=True)])
    view = L.build(_plan(), market, TODAY)
    assert [(d.ex_date, d.amount) for d in view.dividends] == [(date(2026, 12, 4), 0.13)]
    assert view.early is not None and view.early.risk.value == "low"


def test_the_roll_limit_fills_at_the_trigger_all_cycle_long() -> None:
    view = L.build(_plan(), _market(), TODAY)
    roll = view.roll
    for left in (45, 30, 20, 10, 5, 1):
        credit = L.roll_credit(roll.trigger, 340.0, 355.0, left, roll.added_days, view.iv, 0.04)
        assert credit >= roll.limit, left
    # $50 past the strike, it doesn't
    assert L.roll_credit(390.0, 340.0, 355.0, 45, roll.added_days, view.iv, 0.04) < roll.limit


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



def test_the_ter_call_plans_its_rungs_up_to_the_cost() -> None:
    planned = L.plan_rungs(_plan(rungs=()), _market(), TODAY)
    assert planned.rungs == (340.0, 355.0, 375.0, 390.0)
    assert (planned.start, planned.iv, planned.tight) == ("call", 0.68, False)
    # every step pays the minimum, even with the trigger reached the day after a call is sold
    view = L.build(_plan(rungs=planned.rungs), _market(), TODAY)
    assert [r.pays for r in view.rungs] == [True, True, True, None]


def test_a_step_too_big_for_a_roll_is_marked() -> None:
    view = L.build(_plan(rungs=(340.0, 390.0)), _market(), TODAY)
    assert view.rungs[0].pays is False


def test_lower_volatility_plans_more_smaller_steps() -> None:
    calm = L.plan_rungs(_plan(rungs=()), _market(iv=0.45), TODAY)
    assert calm.rungs == (340.0, 350.0, 360.0, 370.0, 380.0, 390.0)


def test_with_no_call_the_plan_starts_at_the_delta_the_gap_to_cost_asks() -> None:
    # TER 31% under the $390 real cost: delta 0.15, on the call that would be sold, Dec 18
    planned = L.plan_rungs(_plan(rungs=(), **NO_CALL), _market(), TODAY)
    assert (planned.start, planned.delta) == ("delta", 0.15)
    assert planned.rungs == (355.0, 370.0, 390.0)
    assert L.call_delta(269.28, 355.0, (DEC - TODAY).days, 0.68) == pytest.approx(0.15, abs=0.02)


def test_the_start_delta_follows_the_gap_to_the_real_cost() -> None:
    assert [L.start_delta(s, 384.0) for s in (300, 350, 380, 400)] == [0.15, 0.25, 0.40, 0.40]


def test_near_or_past_the_cost_the_plan_is_one_rung() -> None:
    # 11% under it: delta 0.25, which with TER at $345 is past $390 already
    assert L.plan_rungs(_plan(rungs=(), **NO_CALL), _market(spot=345.0), TODAY).rungs == (390.0,)
    above = L.plan_rungs(_plan(rungs=(), **NO_CALL), _market(spot=400.0), TODAY)
    assert (above.rungs, above.start) == ((405.0,), "target")


def test_strikes_past_the_listed_ones_carry_on_at_their_spacing() -> None:
    planned = L.plan_rungs(_plan(rungs=(), cost=478.0), _market(), TODAY)  # listed up to $450
    assert planned.rungs[0] == 340.0 and planned.rungs[-1] == 480.0


def test_without_volatility_the_steps_are_about_five_percent() -> None:
    chain = [c.model_copy(update={"implied_volatility": None}) for c in _chain()]
    planned = L.plan_rungs(_plan(rungs=()), L.Market(spot=269.28, calls=chain), TODAY)
    assert planned.iv is None and planned.rungs == (340.0, 355.0, 375.0, 390.0)


def test_a_calm_stock_far_under_its_cost_plans_tight() -> None:
    # 18% volatility, $70 against an $80 cost: even twelve rungs leave steps that pay too little
    market = L.Market(spot=70.0, calls=_chain(spot=70.0, iv=0.18,
                                              strikes=[x / 2 for x in range(120, 180)]))
    planned = L.plan_rungs(_plan(symbol="KO", cost=80.0, rungs=(), **NO_CALL), market, TODAY)
    assert planned.tight and len(planned.rungs) == L.MAX_RUNGS and planned.rungs[-1] == 80.0


def test_with_nothing_to_plan_from_there_is_no_plan() -> None:
    no_price = L.Market(spot=None, calls=_chain())
    assert L.plan_rungs(_plan(rungs=(), **NO_CALL), no_price, TODAY) is None
    assert L.plan_rungs(_plan(rungs=()), L.Market(spot=269.28, calls=[]), TODAY) is None


# --- which holdings need a ladder -------------------------------------------------------------

def _shares(quantity=100, cost=390.0, price=269.28, symbol="TER") -> Position:
    return Position(symbol=symbol, underlying=symbol, kind=PositionKind.SHARES, quantity=quantity,
                    asset_type="EQUITY", market_value=price * quantity, average_price=cost)


def _short_call(strike=340.0, expiry=DEC, sold=6.0, average=None, symbol="TER", dte=45):
    return Position(symbol=f"{symbol} {expiry:%y%m%d}C{strike:g}", underlying=symbol,
                    kind=PositionKind.SHORT_CALL, quantity=1, option_type=OptionType.CALL,
                    strike=strike, expiration=expiry, dte=dte, opening_price=sold,
                    average_price=average)


def test_a_call_below_the_cost_needs_a_ladder_from_it() -> None:
    held = _shares()
    plan = L.for_holding(held, [held, _short_call()])
    assert plan == L.Plan("TER", 100, 390.0, (), call_premiums=6.0, call_strike=340.0,
                          call_expiry=DEC, call_sold_for=6.0)
    assert plan.real_cost == 384.0


def test_with_no_call_open_shares_below_the_cost_need_one() -> None:
    held = _shares(quantity=250)
    plan = L.for_holding(held, [held])
    assert plan == L.Plan("TER", 250, 390.0, ()) and plan.contracts == 2


def test_the_ladder_follows_the_lowest_call_of_several() -> None:
    held = _shares(quantity=200)
    plan = L.for_holding(held, [_short_call(355.0, JAN), _short_call(340.0, DEC), held])
    assert (plan.call_strike, plan.call_expiry) == (340.0, DEC)


def test_a_sale_too_old_for_the_broker_s_record_falls_back_to_the_average_price() -> None:
    held = _shares()
    plan = L.for_holding(held, [held, _short_call(sold=None, average=5.8)])
    assert plan.call_sold_for == 5.8
    plan = L.for_holding(held, [held, _short_call(sold=None)])
    assert plan.call_sold_for is None and plan.call_premiums == 0.0


@pytest.mark.parametrize("held, others", [
    (_shares(), [_short_call(strike=390.0)]),  # the call is at the cost: the target already
    (_shares(price=395.0), []),  # no call, and the shares are past the cost
    (_shares(quantity=99), [_short_call()]),  # too few shares for a call
    (_shares(cost=None), [_short_call()]),  # no cost reported
])
def test_holdings_that_need_no_ladder(held, others) -> None:
    assert L.for_holding(held, [held, *others]) is None


def test_only_a_live_call_on_these_shares_counts() -> None:
    held = _shares()
    plan = L.for_holding(held, [held, _short_call(expiry=TODAY, dte=-1)])
    assert plan is not None and not plan.has_call
    # nor does a call on something else
    assert not L.for_holding(held, [held, _short_call(symbol="KGC")]).has_call
    assert L.for_holding(_short_call(), [_short_call()]) is None  # only shares have a ladder
