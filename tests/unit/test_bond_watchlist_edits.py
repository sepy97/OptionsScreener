"""Editing a watchlist a bond at a time: the saved text stays the script's format throughout."""

from __future__ import annotations

from datetime import date

import pytest

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds import watchlist as bw

SAVED = ('code,name,matdate,qty\r\n'
         'RU000A0JXN21,"АФК Система, 001P-06",2027-03-26,10\r\n'
         'SU26238RMFS4,,,\r\n'
         ',"ГТЛК, 001P-04",2032-04-06,\r\n')


def _item(code="RU000A10FA72", name="АФК Система БО 002Р-14", matdate=date(2030, 2, 1), qty=None):
    return {"code": code, "name": name, "matdate": matdate, "qty": qty}


def test_rows_are_sorted_by_name_and_keyed_by_code_or_name() -> None:
    rows = bw.rows(SAVED)
    assert [r["name"] or r["code"] for r in rows] == [
        "SU26238RMFS4", "АФК Система, 001P-06", "ГТЛК, 001P-04"]
    assert [r["key"] for r in rows] == ["SU26238RMFS4", "RU000A0JXN21", "гтлк, 001p-04"]
    assert bw.codes(SAVED) == {"RU000A0JXN21", "SU26238RMFS4"}


def test_add_appends_in_the_scripts_format() -> None:
    text = bw.add(SAVED, _item(), limit=10)
    assert text.startswith(SAVED)
    assert text.endswith('RU000A10FA72,АФК Система БО 002Р-14,2030-02-01,\r\n')
    assert bc.parse_watchlist(text)[-1] == _item()


def test_add_refuses_a_bond_already_listed_under_either_code() -> None:
    with pytest.raises(bw.WatchlistError, match="already in your watchlist"):
        bw.add(SAVED, _item(code="RU000A0JXN21"), limit=10)
    # the OFZ is listed by its MOEX code; adding it by ISIN is the same bond
    with pytest.raises(bw.WatchlistError, match="already"):
        bw.add(SAVED, _item(code="RU000A1038V6", name="ОФЗ 26238"),
               aliases=("SU26238RMFS4", "RU000A1038V6"), limit=10)


def test_add_stops_at_the_limit() -> None:
    with pytest.raises(bw.WatchlistError, match="at most 3 bonds"):
        bw.add(SAVED, _item(), limit=3)


def test_added_names_survive_being_read_back() -> None:
    # the text is read line by line, and a # starts a comment: neither may cut the name
    text = bw.add("", _item(name="Займ #1\nвторая  строка"), limit=10)
    assert bc.parse_watchlist(text)[0]["name"] == "Займ 1 вторая строка"
    assert bw.clean({"code": " ru000a10fa72 ", "name": None})["code"] == "RU000A10FA72"


def test_remove_returns_the_bond_to_undo_with() -> None:
    text, gone = bw.remove(SAVED, "RU000A0JXN21")
    assert gone == {"code": "RU000A0JXN21", "name": "АФК Система, 001P-06",
                    "matdate": date(2027, 3, 26), "qty": 10.0}
    assert "RU000A0JXN21" not in text and len(bc.parse_watchlist(text)) == 2
    assert bw.add(text, gone, limit=10).count("RU000A0JXN21,") == 1  # undo puts it back
    text, _ = bw.remove(SAVED, "гтлк, 001p-04")  # a bond with no code goes by its name
    assert "ГТЛК" not in text
    with pytest.raises(bw.WatchlistError):
        bw.remove(SAVED, "RU000XXXXXXX")
    assert bc.parse_watchlist(bw.remove(bw.remove(bw.remove(SAVED, "RU000A0JXN21")[0],
                                                  "SU26238RMFS4")[0], "гтлк, 001p-04")[0]) == []


def test_set_qty_sets_clears_and_refuses_nonsense() -> None:
    text, it = bw.set_qty(SAVED, "SU26238RMFS4", 2.5)
    assert it["qty"] == 2.5 and "SU26238RMFS4,,,2.5\r\n" in text
    text, _ = bw.set_qty(SAVED, "RU000A0JXN21", None)
    assert bc.parse_watchlist(text)[0]["qty"] is None
    for bad in (-1.0, float("inf"), float("nan")):
        with pytest.raises(bw.WatchlistError):
            bw.set_qty(SAVED, "RU000A0JXN21", bad)
    with pytest.raises(bw.WatchlistError):
        bw.set_qty(SAVED, "RU000XXXXXXX", 1.0)
