from __future__ import annotations

from datetime import date

from wheel_screener.core.exits import compare, covered_calls, keep, rolls
from wheel_screener.core.models import OptionContract, OptionType

TODAY = date(2026, 8, 30)
EXP = date(2026, 9, 25)          # the position's own expiry, 26 days out


def _c(strike, exp, bid, ask, kind=OptionType.PUT):
    return OptionContract(
        underlying_symbol="AVGO", option_symbol=f"AVGO-{exp}-{strike}-{kind.value}",
        option_type=kind, expiration=exp, strike=strike, dte=(exp - TODAY).days,
        bid=bid, ask=ask,
    )


# The live AVGO position this was built against: $390 put, spot $368.75, $33.74 to close.
SPOT = 368.75
CUR = _c(390.0, EXP, 33.40, 33.74)


def test_keep_earns_the_extrinsic_not_the_whole_mark() -> None:
    """$3,374 to close is $2,125 of intrinsic you expect to hand back plus $1,249 of time value.
    Counting the intrinsic as earnings would make every deep ITM position look wonderful."""
    row = keep([CUR], 390.0, EXP, 1, SPOT, TODAY)
    assert row is not None
    assert round(row.credit) == 1249
    assert row.days == 26 and row.collateral == 39_000
    assert round(row.rate, 3) == 0.450


def test_keep_is_priced_off_the_ask_because_closing_means_buying_it_back() -> None:
    cheap_ask = _c(390.0, EXP, 33.40, 33.50)
    assert keep([cheap_ask], 390.0, EXP, 1, SPOT, TODAY).credit < keep(
        [CUR], 390.0, EXP, 1, SPOT, TODAY).credit


def test_a_roll_earns_over_the_days_it_adds_not_its_whole_life() -> None:
    """The days before the current expiry are yours either way. Scoring a 7-day extension across
    its full 33-day span made it read four times better than it is."""
    later = _c(390.0, date(2026, 11, 20), 42.45, 42.80)
    row = rolls([CUR, later], 390.0, EXP, 1, SPOT, TODAY)[0]
    assert row.days == 56, "20 Nov is 56 days after 25 Sep, not 82 after today"
    assert round(row.credit) == 871
    assert round(row.rate, 3) == 0.146


def test_a_roll_that_costs_money_reports_a_negative_rate() -> None:
    near = _c(390.0, date(2026, 10, 2), 32.99, 33.30)
    row = rolls([CUR, near], 390.0, EXP, 1, SPOT, TODAY)[0]
    assert row.credit < 0 and row.rate < 0


def test_rolling_up_is_scored_on_time_value_so_it_cannot_top_the_table() -> None:
    """The trap, with real quotes: rolling $390 -> $435 pays a $3,476 credit, which on gross cash
    is 417%/yr and sorts first. Net of the intrinsic it sells, it DESTROYS $1,024 of time value
    while committing $4,500 more collateral. Ranked on cash it would be recommended hardest
    exactly when it is worst."""
    up = _c(435.0, date(2026, 10, 2), 68.50, 68.90)
    row = rolls([CUR, up], 390.0, EXP, 1, SPOT, TODAY, roll_strike=435.0)[0]
    assert round(row.credit) == 3476, "the gross credit really is that large"
    assert round(row.extrinsic) == -1024, "and it is entirely intrinsic, then some"
    assert row.rate < 0, "so the honest rate is negative"
    assert row.collateral_delta == 4500, "still carried, for the collateral column to show"
    # ONE warning, and only the thing the numbers cannot show. That collateral grew is already
    # in its own column; that part of the credit is intrinsic is invisible until expiry.
    assert len(row.warnings) == 1 and "in the money" in row.warnings[0]


def test_the_cash_credit_never_outranks_the_time_value() -> None:
    """End to end: the biggest cheque on the table must sort last when it is the worst trade."""
    up = _c(435.0, date(2026, 10, 2), 68.50, 68.90)
    rows = compare([CUR, up], [_c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL)],
                   strike=390.0, expiration=EXP, contracts=1, spot=SPOT, today=TODAY,
                   roll_strike=435.0)
    biggest_cheque = max(rows, key=lambda r: r.credit)
    assert biggest_cheque.credit > 3000 and rows[-1] is biggest_cheque


def test_a_same_strike_roll_carries_no_warnings() -> None:
    later = _c(390.0, date(2026, 11, 20), 42.45, 42.80)
    assert rolls([CUR, later], 390.0, EXP, 1, SPOT, TODAY)[0].warnings == ()


def test_keeping_and_assign_plus_covered_call_score_the_same() -> None:
    """Put-call parity, not a coincidence: the put's extrinsic over the strike equals the call's
    premium over the share value. For an ITM short put those two paths ARE the same trade, and
    if this ever drifts apart the pricing maths is wrong somewhere."""
    call = _c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL)
    held = keep([CUR], 390.0, EXP, 1, SPOT, TODAY)
    assigned = covered_calls([call], 390.0, 1, SPOT, TODAY)[0]
    assert abs(held.rate - assigned.rate) < 0.005
    assert assigned.collateral == 36_875  # the shares are worth spot, not what they cost


def test_covered_calls_are_offered_only_when_assignment_is_the_live_outcome() -> None:
    call = _c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL)
    assert covered_calls([call], 390.0, 1, 420.0, TODAY) == [], "OTM: no assignment to compare"
    assert covered_calls([call], 390.0, 1, None, TODAY) == [], "no quote: no claim"
    assert covered_calls([call], 390.0, 1, SPOT, TODAY)


def test_only_an_in_the_money_call_is_flagged() -> None:
    """Same single idea as the roll ladder — flag the sale whose premium is partly intrinsic,
    and stay quiet on the ordinary one."""
    itm = covered_calls([_c(360.0, EXP, 22.0, 22.4, kind=OptionType.CALL)], 390.0, 1, SPOT,
                        TODAY, call_strike=360.0)[0]
    assert len(itm.warnings) == 1 and "in the money" in itm.warnings[0]
    otm = covered_calls([_c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL)], 390.0, 1, SPOT,
                        TODAY)[0]
    assert otm.warnings == ()


def test_compare_ranks_by_rate_and_always_includes_keeping() -> None:
    rows = compare(
        [CUR, _c(390.0, date(2026, 11, 20), 42.45, 42.80),
         _c(390.0, date(2026, 10, 2), 32.99, 33.30)],
        [_c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL)],
        strike=390.0, expiration=EXP, contracts=1, spot=SPOT, today=TODAY,
    )
    assert any(r.kind == "keep" for r in rows), "the baseline is always on the table"
    rates = [r.rate for r in rows]
    assert rates == sorted(rates, reverse=True)
    assert rows[0].kind in ("keep", "assign_cc"), "on this position, doing nothing wins"


def test_multiple_contracts_scale_every_leg_together() -> None:
    later = _c(390.0, date(2026, 11, 20), 42.45, 42.80)
    one = rolls([CUR, later], 390.0, EXP, 1, SPOT, TODAY)[0]
    three = rolls([CUR, later], 390.0, EXP, 3, SPOT, TODAY)[0]
    assert round(three.credit) == round(one.credit * 3)
    assert three.collateral == one.collateral * 3
    assert abs(three.rate - one.rate) < 1e-9, "a rate must not depend on position size"


def test_an_unpriced_or_missing_position_yields_nothing_rather_than_guessing() -> None:
    assert keep([], 390.0, EXP, 1, SPOT, TODAY) is None
    assert keep([_c(390.0, EXP, 33.40, None)], 390.0, EXP, 1, SPOT, TODAY) is None
    assert rolls([], 390.0, EXP, 1, SPOT, TODAY) == []


def test_expiries_on_or_before_the_current_one_are_not_rolls() -> None:
    same = _c(390.0, EXP, 33.40, 33.74)
    earlier = _c(390.0, date(2026, 9, 18), 30.0, 30.4)
    assert rolls([CUR, same, earlier], 390.0, EXP, 1, SPOT, TODAY) == []


def test_covered_calls_are_one_strike_across_expiries() -> None:
    """A ladder, not a grid. The question is how long to write for; answering with every strike
    at every expiry turns a decision into a spreadsheet."""
    from wheel_screener.core.exits import covered_calls

    chain = [
        _c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL),
        _c(395.0, EXP, 9.60, 9.90, kind=OptionType.CALL),
        _c(400.0, EXP, 8.14, 8.40, kind=OptionType.CALL),
        _c(390.0, date(2026, 10, 2), 12.95, 13.20, kind=OptionType.CALL),
        _c(395.0, date(2026, 10, 2), 11.20, 11.50, kind=OptionType.CALL),
    ]
    rows = covered_calls(chain, 390.0, 1, SPOT, TODAY)
    assert [r.strike for r in rows] == [390.0, 390.0], "the put's own strike, both expiries"
    assert [r.expiration for r in rows] == [EXP, date(2026, 10, 2)]


def test_the_default_call_strike_is_the_cost_basis_assignment_hands_you() -> None:
    from wheel_screener.core.exits import covered_calls

    chain = [_c(k, EXP, 10.0, 10.2, kind=OptionType.CALL) for k in (380.0, 390.0, 400.0)]
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY)[0].strike == 390.0
    # and an explicit choice overrides it
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY, call_strike=400.0)[0].strike == 400.0


def test_a_call_strike_the_chain_does_not_list_falls_back_to_the_nearest() -> None:
    """Assignment gives you shares whatever the chain lists; refusing to show any call because
    the exact strike is missing would answer a real position with a blank."""
    from wheel_screener.core.exits import covered_calls

    chain = [_c(k, EXP, 10.0, 10.2, kind=OptionType.CALL) for k in (385.0, 395.0)]
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY)[0].strike == 385.0


def test_rolls_show_one_row_per_expiry_even_with_adjusted_contracts() -> None:
    """An adjusted contract shares its strike and expiry with the standard one, so the ladder
    would list the same expiry twice at different prices."""
    later = date(2026, 11, 20)
    standard = _c(390.0, later, 42.45, 42.80)
    adjusted = _c(390.0, later, 41.10, 42.00)
    rows = rolls([CUR, standard, adjusted], 390.0, EXP, 1, SPOT, TODAY)
    assert len(rows) == 1 and rows[0].expiration == later


def test_the_call_ladder_ignores_strikes_that_are_not_the_position_s() -> None:
    """A chain carries every strike the market lists. Offering a $390 call against a $190 put is
    not a rounding error — it is an answer to somebody else's position."""
    from wheel_screener.core.exits import covered_calls

    put_strike = 190.0
    chain = [_c(k, EXP, 8.0, 8.2, kind=OptionType.CALL)
             for k in (180.0, 185.0, 190.0, 195.0, 200.0, 390.0)]
    rows = covered_calls(chain, put_strike, 2, 185.0, TODAY)
    assert [r.strike for r in rows] == [190.0]
    assert rows[0].label == "Assign, sell $190 call 25 Sep"
    assert all(str(int(put_strike)) in r.label for r in rows)


def test_a_higher_strike_is_not_the_same_as_an_in_the_money_one() -> None:
    """QCOM: $150 put, stock at $164. Rolling to $155 sells NO intrinsic — both strikes are out
    of the money — and warning about it there teaches the reader to ignore the warning that
    matters. Compare the obligations, not the strikes."""
    from wheel_screener.core.exits import rolls as _rolls

    spot, exp = 164.06, date(2026, 9, 4)
    cur = _c(150.0, exp, 0.20, 0.23)
    later = date(2026, 9, 11)

    otm = _rolls([cur, _c(155.0, later, 1.38, 1.45)], 150.0, exp, 1, spot, TODAY,
                 roll_strike=155.0)[0]
    assert otm.warnings == (), "an ordinary roll up to a still-OTM strike is unremarkable"
    assert otm.extrinsic == otm.credit, "nothing intrinsic changed hands"
    assert otm.collateral_delta == 500, "the collateral column still says it grew"

    itm = _rolls([cur, _c(170.0, later, 7.00, 7.20)], 150.0, exp, 1, spot, TODAY,
                 roll_strike=170.0)[0]
    assert any("intrinsic" in w for w in itm.warnings), "$170 IS in the money at $164"
