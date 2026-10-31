"""The calendar of a person's portfolios: one build of their bonds, quantities put in per view."""

from __future__ import annotations

from datetime import date

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds import holdings as bh
from wheel_screener.bonds.holdings import Holding

SYS = ("RU000A0JXN21", "АФК Система, 001P-06", date(2027, 3, 26))


def _h(portfolio, code, name, matdate=None, qty=None, pid=None) -> Holding:
    return Holding(pid or portfolio.lower(), portfolio, code, name, matdate, qty)


def _event(secid, isin, name, day=date(2027, 1, 10), kind=bc.COUPON, amount=35.78) -> dict:
    return {"date": day, "kind": kind, "name": name, "isin": isin, "secid": secid,
            "amount": amount, "cur": "RUB", "rate": 14.35, "qty": None, "total": None,
            "note": "record date 2027-01-09", "remind": None}


def test_the_build_is_of_each_bond_once_without_quantities() -> None:
    holdings = [_h("Broker", *SYS, qty=10), _h("IIS", *SYS, qty=5),
                _h("IIS", "SU26238RMFS4", "ОФЗ 26238", qty=1)]
    text = bh.bonds_text(holdings)
    assert text == ('code,name,matdate,qty\r\n'
                    'RU000A0JXN21,"АФК Система, 001P-06",2027-03-26,\r\n'
                    'SU26238RMFS4,ОФЗ 26238,,\r\n')
    # other quantities, other portfolios, another order: the same bonds, the same build
    assert bh.bonds_text([holdings[2], _h("X", *SYS, qty=99)]) == text
    assert bh.bonds_text([holdings[2]]) != text


def test_quantities_add_up_over_the_portfolios_shown() -> None:
    events = [_event("RU000A0JXN21", "RU000A0JXN21", SYS[1]),
              _event("SU26238RMFS4", "RU000A1038V6", "ОФЗ 26238", amount=None)]
    broker, iis = _h("Broker", *SYS, qty=10), _h("IIS", *SYS, qty=5)
    ofz = _h("IIS", "RU000A1038V6", "ОФЗ 26238", qty=3)  # by ISIN; MOEX knows it by SU code
    both = bh.shape(events, [broker, iis, ofz])
    assert [(e["secid"], e["qty"], e["total"]) for e in both] == [
        ("RU000A0JXN21", 15.0, 35.78 * 15), ("SU26238RMFS4", 3.0, None)]  # no amount yet
    assert bh.holders_label(both[0]) == "Broker 10 · IIS 5"
    only = bh.shape(events, [broker])  # one portfolio: its quantities, its bonds only
    assert [(e["secid"], e["qty"]) for e in only] == [("RU000A0JXN21", 10.0)]
    assert events[0]["qty"] is None  # the cached build is not touched


def test_a_bond_without_quantities_still_shows_its_events() -> None:
    [e] = bh.shape([_event("RU000A0JXN21", "RU000A0JXN21", SYS[1])], [_h("Main", *SYS)])
    assert e["qty"] is None and e["total"] is None and bh.holders_label(e) == "Main (no qty)"


def test_a_bond_held_under_both_codes_is_one_event() -> None:
    # held as SU… in one portfolio and RU… in another: built twice, shown once
    dup = [_event("SU26238RMFS4", "RU000A1038V6", "ОФЗ 26238"),
           _event("SU26238RMFS4", "RU000A1038V6", "ОФЗ-ПД 26238")]
    [e] = bh.shape(dup, [_h("A", "SU26238RMFS4", "ОФЗ 26238", qty=1),
                         _h("B", "RU000A1038V6", "ОФЗ-ПД 26238", qty=2)])
    assert e["qty"] == 3.0 and bh.holders_label(e) == "A 1 · B 2"


def test_a_bond_found_by_name_during_the_build_is_matched_by_name() -> None:
    [e] = bh.shape([_event("RU000A0ZYQY7", "RU000A0ZYQY7", "АФК Система, 001P-07")],
                   [_h("Main", "", "АФК Система, 001P-07", qty=4)])
    assert e["qty"] == 4.0


def test_the_ics_carries_the_portfolios_in_the_note() -> None:
    [e] = bh.with_holders_note(bh.shape([_event(SYS[0], SYS[0], SYS[1])],
                                        [_h("Broker", *SYS, qty=10), _h("IIS", *SYS, qty=5)]))
    assert e["note"] == "record date 2027-01-09; Portfolios: Broker 10 · IIS 5"


def test_the_tables_take_a_line_per_event_and_portfolio() -> None:
    events = [_event(SYS[0], SYS[0], SYS[1]),
              _event(SYS[0], SYS[0], SYS[1], day=date(2027, 3, 26), kind=bc.REDEMPTION,
                     amount=1000.0)]
    shaped = bh.shape(events, [_h("Broker", *SYS, qty=10), _h("IIS", *SYS, qty=5),
                               _h("Old", *SYS)])  # imported without quantities
    lines = bh.by_portfolio(shaped)
    assert [(e["kind"], e["portfolio"], e["qty"], e["total"]) for e in lines] == [
        (bc.COUPON, "Broker", 10, 35.78 * 10), (bc.COUPON, "IIS", 5, 35.78 * 5),
        (bc.COUPON, "Old", None, None),
        (bc.REDEMPTION, "Broker", 10, 10000.0), (bc.REDEMPTION, "IIS", 5, 5000.0),
        (bc.REDEMPTION, "Old", None, None)]
    assert all(e["note"] == "record date 2027-01-09" for e in lines)  # no portfolios to list
    assert shaped[0]["qty"] == 15  # the page's events are not touched


def test_a_portfolio_exports_as_the_scripts_watchlist() -> None:
    text = bh.as_text([_h("Main", *SYS, qty=10), _h("Main", "", "ГТЛК, 001P-04")])
    assert bc.parse_watchlist(text) == [
        {"code": "RU000A0JXN21", "name": SYS[1], "matdate": SYS[2], "qty": 10.0},
        {"code": "", "name": "ГТЛК, 001P-04", "matdate": None, "qty": None}]


def test_skip_notes_count_the_bonds_shown_each_once() -> None:
    old = ("RU000A0ZYQY7", "Matured", date(2020, 1, 1))
    shown = [_h("A", *old, qty=1), _h("B", *old, qty=2), _h("B", *SYS, qty=1)]
    assert bh.skip_notes(shown, date(2026, 1, 1)) == [
        "Skipping 1 already matured bonds (show the whole calendar to include them)."]
    assert bh.skip_notes(shown, None) == []  # the whole calendar skips nothing


def test_changes_say_what_is_new_gone_and_held_in_another_quantity() -> None:
    ofz = ("SU26238RMFS4", "ОФЗ 26238", None)
    before = [_h("Main", *SYS, qty=10), _h("Main", *ofz, qty=2),
              _h("Main", "", "ГТЛК, 001P-04", qty=None), _h("Main", "RU000A10EMG2", "", qty=5)]
    after = [_h("Main", *SYS, qty=15), _h("Main", "", "ГТЛК, 001P-04", qty=None),
             _h("Main", "RU000A10EMG2", "", qty=5), _h("Main", "RU000A0JS3W6", "ОФЗ 26207", qty=45)]
    c = bh.changes(before, after)
    assert c.added == [("ОФЗ 26207", 45)] and c.removed == [("ОФЗ 26238", 2)]
    assert c.changed == [("АФК Система, 001P-06", 10, 15)] and c.same == 2 and c.any
    assert not bh.changes(after, after).any and bh.changes(after, after).same == 4
