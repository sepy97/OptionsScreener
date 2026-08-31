from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.core.exits import (
    compare,
    keep,
    rolls,
)
from wheel_screener.core.exits import (
    write_after_assignment as covered_calls,
)
from wheel_screener.core.models import OptionContract, OptionType

TODAY = date(2026, 8, 30)
EXP = date(2026, 9, 25)          # the position's own expiry, 26 days out
LATER = date(2026, 10, 16)       # after assignment, so a call can be written


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
    rows, _after = compare([CUR, up], [_c(390.0, LATER, 11.81, 12.05, kind=OptionType.CALL)],
                           strike=390.0, expiration=EXP, contracts=1, spot=SPOT, today=TODAY,
                           roll_strike=435.0)
    biggest_cheque = max(rows, key=lambda r: r.credit)
    assert biggest_cheque.credit > 3000 and rows[-1] is biggest_cheque


def test_a_same_strike_roll_carries_no_warnings() -> None:
    later = _c(390.0, date(2026, 11, 20), 42.45, 42.80)
    assert rolls([CUR, later], 390.0, EXP, 1, SPOT, TODAY)[0].warnings == ()


def test_the_call_ladder_prices_tenors_not_future_contracts() -> None:
    """Quoting the contract you would actually sell is wrong: AVGO's 16 Oct call holds 47 days of
    life today and would be written with 21 left. Pairing today's price with the post-assignment
    period read 77%/yr where 51% was real. Each row is a TENOR priced from today's market."""
    chain = [_c(390.0, TODAY + timedelta(days=d), bid, bid + 0.2, kind=OptionType.CALL)
             for d, bid in ((19, 10.53), (26, 11.81), (47, 16.48))]
    rows = covered_calls(chain, 390.0, 1, SPOT, TODAY)
    assert [r.days for r in rows] == [19, 26, 47], "the tenor is the holding period, and its own"
    assert [round(r.credit) for r in rows] == [1053, 1181, 1648]
    assert all(r.expiration is None for r in rows), \
        "these are tenors, not contracts — a date invites 'sell the 02 Sep call' all over again"
    assert rows[0].label == "19-day $390 call"


def test_the_tenor_proxy_agrees_with_decaying_the_real_contract() -> None:
    """Two independent estimates of what a 21-day call fetches on 25 Sep. Today's ~21-day quote
    interpolates to $1,090; decaying the 16 Oct contract's $1,648 by root-time gives $1,102. They
    agree to about 1%, which is the reason to trust either — and neither is $1,648."""
    import math

    proxy = 1053 + (1181 - 1053) * (21 - 19) / (26 - 19)
    decayed = 1648 * math.sqrt(21 / 47)
    assert abs(proxy - decayed) / proxy < 0.02
    assert proxy < 1200, "and both are far below the 1,648 the contract quotes today"


def test_covered_calls_are_offered_only_when_assignment_is_the_live_outcome() -> None:
    call = _c(390.0, LATER, 11.81, 12.05, kind=OptionType.CALL)
    # The in-the-money guard lives in compare(), which is the layer that knows whether the
    # position is even short — a long option is exercised by choice, not assigned to you.
    _, otm = compare([CUR], [call], strike=390.0, expiration=EXP, contracts=1,
                     spot=420.0, today=TODAY)
    _, long_itm = compare([CUR], [call], strike=390.0, expiration=EXP, contracts=1,
                          spot=SPOT, today=TODAY, is_short=False)
    _, short_itm = compare([CUR], [call], strike=390.0, expiration=EXP, contracts=1,
                           spot=SPOT, today=TODAY)
    assert otm == [], "out of the money: no assignment to plan past"
    assert long_itm == [], "a long option is exercised by choice, never assigned"
    assert short_itm, "a short position in the money is the case that has one"


def test_only_an_in_the_money_call_is_flagged() -> None:
    """Same single idea as the roll ladder — flag the sale whose premium is partly intrinsic,
    and stay quiet on the ordinary one."""
    itm = covered_calls([_c(360.0, LATER, 22.0, 22.4, kind=OptionType.CALL)], 390.0, 1, SPOT,
                        TODAY, write_strike=360.0)[0]
    assert len(itm.warnings) == 1 and "in the money" in itm.warnings[0]
    otm = covered_calls([_c(390.0, LATER, 11.81, 12.05, kind=OptionType.CALL)], 390.0, 1, SPOT,
                        TODAY)[0]
    assert otm.warnings == ()


def test_compare_ranks_by_rate_and_always_includes_keeping() -> None:
    rows, after = compare(
        [CUR, _c(390.0, date(2026, 11, 20), 42.45, 42.80),
         _c(390.0, date(2026, 10, 2), 32.99, 33.30)],
        [_c(390.0, LATER, 18.00, 18.30, kind=OptionType.CALL)],
        strike=390.0, expiration=EXP, contracts=1, spot=SPOT, today=TODAY,
    )
    assert any(r.kind == "keep" for r in rows), "the baseline is always on the table"
    rates = [r.rate for r in rows]
    assert rates == sorted(rates, reverse=True)
    assert rows[0].kind == "keep", "on this position, doing nothing wins"
    assert all(r.kind != "assign_cc" for r in rows), "not an alternative, so not in the ranking"
    assert [r.kind for r in after] == ["assign_cc"]


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
    chain = [
        _c(390.0, EXP, 11.81, 12.05, kind=OptionType.CALL),
        _c(395.0, EXP, 9.60, 9.90, kind=OptionType.CALL),
        _c(400.0, EXP, 8.14, 8.40, kind=OptionType.CALL),
        _c(390.0, date(2026, 10, 2), 12.95, 13.20, kind=OptionType.CALL),
        _c(395.0, date(2026, 10, 2), 11.20, 11.50, kind=OptionType.CALL),
    ]
    rows = covered_calls(chain, 390.0, 1, SPOT, TODAY)
    assert [r.strike for r in rows] == [390.0, 390.0], "the put's own strike, both tenors"
    assert [r.days for r in rows] == [26, 33], "one row per tenor, nearest first"


def test_the_default_call_strike_is_the_cost_basis_assignment_hands_you() -> None:
    chain = [_c(k, EXP, 10.0, 10.2, kind=OptionType.CALL) for k in (380.0, 390.0, 400.0)]
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY)[0].strike == 390.0
    # and an explicit choice overrides it
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY,
                         write_strike=400.0)[0].strike == 400.0


def test_a_call_strike_the_chain_does_not_list_falls_back_to_the_nearest() -> None:
    """Assignment gives you shares whatever the chain lists; refusing to show any call because
    the exact strike is missing would answer a real position with a blank."""
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
    put_strike = 190.0
    chain = [_c(k, EXP, 8.0, 8.2, kind=OptionType.CALL)
             for k in (180.0, 185.0, 190.0, 195.0, 200.0, 390.0)]
    rows = covered_calls(chain, put_strike, 2, 185.0, TODAY)
    assert [r.strike for r in rows] == [190.0]
    assert rows[0].label.endswith("$190 call")
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


def test_an_unlisted_call_strike_snaps_to_the_nearest_rather_than_emptying_the_table() -> None:
    """Emptying the table over a strike that simply is not traded looks exactly like the control
    doing nothing — which is how the bug was reported."""
    chain = [_c(k, TODAY + timedelta(days=26), 8.0, 8.2, kind=OptionType.CALL)
             for k in (385.0, 390.0, 400.0)]
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY, write_strike=402.0)[0].strike == 400.0
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY, write_strike=1_000.0)[0].strike == 400.0
    assert covered_calls(chain, 390.0, 1, SPOT, TODAY, write_strike=400.0)[0].strike == 400.0
    assert covered_calls([], 390.0, 1, SPOT, TODAY, write_strike=400.0) == [], "no chain, no claim"


# ── all four kinds of option ───────────────────────────────────────────────────────────────

def _chain(kind, *rows):
    return [_c(k, TODAY + timedelta(days=d), bid, ask, kind=kind) for k, d, bid, ask in rows]


_PUT_CHAIN = _chain(OptionType.PUT, (390.0, 26, 33.40, 33.74), (390.0, 82, 42.45, 42.80))
_CALL_CHAIN = _chain(OptionType.CALL, (390.0, 19, 10.53, 10.80), (390.0, 26, 11.81, 12.05),
                     (390.0, 82, 24.13, 24.50))
_HELD = TODAY + timedelta(days=26)


def test_a_long_position_pays_to_wait_where_a_short_is_paid() -> None:
    """The same extrinsic, opposite signs. Reporting both as positive would stand a bleeding
    long call beside a working short put as though they were the same thing."""
    short = keep(_PUT_CHAIN, 390.0, _HELD, 1, SPOT, TODAY, is_short=True)
    long_ = keep(_PUT_CHAIN, 390.0, _HELD, 1, SPOT, TODAY, is_short=False)
    assert short.credit > 0 and long_.credit < 0
    assert short.rate > 0 > long_.rate


def test_capital_committed_differs_by_kind_and_that_is_the_denominator() -> None:
    """Get this wrong and no two rows are comparable. A short put reserves the strike in cash; a
    short call encumbers shares at their CURRENT value; a long ties up only what it would fetch."""
    sp = keep(_PUT_CHAIN, 390.0, _HELD, 1, SPOT, TODAY)
    sc = keep(_CALL_CHAIN, 390.0, _HELD, 1, SPOT, TODAY, option_type=OptionType.CALL)
    lp = keep(_PUT_CHAIN, 390.0, _HELD, 1, SPOT, TODAY, is_short=False)
    assert sp.collateral == 39_000, "the strike, because that is what assignment costs"
    assert sc.collateral == 36_875, "the shares at market, not what they cost"
    assert lp.collateral == 3_340, "only what selling it would return"


def test_an_out_of_the_money_long_reads_as_a_total_loss_of_its_own_value() -> None:
    """It expires worthless if nothing moves, and the rate says so rather than hiding it."""
    row = keep(_CALL_CHAIN, 390.0, _HELD, 1, SPOT, TODAY,
               option_type=OptionType.CALL, is_short=False)
    assert row.credit == -row.collateral, "all of it is time value, and all of it goes"


def test_rolling_a_long_is_a_debit_not_a_credit() -> None:
    row = rolls(_PUT_CHAIN, 390.0, _HELD, 1, SPOT, TODAY, is_short=False)[0]
    assert row.credit < 0 and row.rate < 0, "you pay for more time; you are not paid for it"


def test_an_assigned_short_call_frees_cash_to_write_puts_with() -> None:
    """The wheel's other half, and the exact mirror: a short put assigned leaves shares to write
    calls against, a short call assigned leaves cash to write puts with."""
    above = 420.0  # the $390 call is in the money here
    _, after = compare(_CALL_CHAIN, _PUT_CHAIN, strike=390.0, expiration=_HELD, contracts=1,
                       spot=above, today=TODAY, option_type=OptionType.CALL)
    assert after and all("put" in r.label for r in after), "puts, not calls"
    assert after[0].collateral == 39_000, "the cash the sale hands you, not the shares' value"


def test_the_continuation_is_offered_only_where_assignment_can_happen_to_you() -> None:
    for label, kw, expected in (
        ("short put ITM", {}, True),
        ("long put ITM", {"is_short": False}, False),
        ("short put OTM", {"spot": 420.0}, False),
    ):
        spot = kw.pop("spot", SPOT)
        _, after = compare(_PUT_CHAIN, _CALL_CHAIN, strike=390.0, expiration=_HELD, contracts=1,
                           spot=spot, today=TODAY, **kw)
        assert bool(after) is expected, label


def test_a_grid_cell_books_the_whole_position_not_one_share() -> None:
    """A five-contract position books five times what a per-share quote suggests, and the cell
    is meant to be the value of the deal."""
    from datetime import timedelta as _td

    from wheel_screener.core import rollgrid

    exp = TODAY + _td(days=26)
    chain = [
        OptionContract(underlying_symbol="KGC", option_symbol=f"K{d}", option_type=OptionType.PUT,
                       expiration=TODAY + _td(days=d), strike=25.0, dte=d,
                       bid=b, ask=b + 0.05, delta=-0.3, open_interest=500)
        for d, b in ((26, 0.40), (54, 0.95))
    ]
    one = rollgrid.build(chain, strike=25.0, expiration=exp, contracts=1, spot=31.27, today=TODAY)
    five = rollgrid.build(chain, strike=25.0, expiration=exp, contracts=5, spot=31.27, today=TODAY)
    later = TODAY + _td(days=54)
    assert one.cell(25.0, later).credit_total == round(
        five.cell(25.0, later).credit_total / 5, 2)
    assert abs(five.cell(25.0, later).credit_total - 5 * 100 * 0.50) < 1e-6
    # and the per-day figure scales with it rather than staying per-share
    cell = five.cell(25.0, later)
    assert abs(cell.per_day_total - cell.credit_total / 28) < 1e-9
