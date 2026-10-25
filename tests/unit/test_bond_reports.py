"""Reading a portfolio from a file: Intelinvest's portfolio report (.xlsx), other sheets, CSV."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from _workbooks import intelinvest_report, workbook

from wheel_screener.bonds.reports import ReportError, read_portfolio


@pytest.mark.parametrize("shared", [False, True])
def test_an_intelinvest_report_gives_the_bonds_held(shared) -> None:
    found = read_portfolio(intelinvest_report(shared=shared))
    assert found.items == [
        {"code": "RU000A10EMG2", "name": "НоваБев3Р3", "matdate": None, "qty": 120.0},
        {"code": "RU000A0JS3W6", "name": "ОФЗ 26207", "matdate": None, "qty": 45.0},
    ]
    assert found.sold_out == 1 and found.source == "sheet «Портфель облигаций»"


def test_another_spreadsheet_is_read_like_the_scripts_csv() -> None:
    raw = workbook({"Notes": [["just", "text"]],
                 "Bonds": [["My bonds"], [], ["ISIN", "Name", "matdate", "qty"],
                           ["RU000A0JXN21", "АФК Система, 001P-06", 46472, 10],  # Excel's date
                           ["", "ГТЛК, 001P-04", "2032-04-06", ""]]})
    found = read_portfolio(raw)
    assert found.items == [
        {"code": "RU000A0JXN21", "name": "АФК Система, 001P-06", "matdate": date(2027, 3, 26),
         "qty": 10.0},
        {"code": "", "name": "ГТЛК, 001P-04", "matdate": date(2032, 4, 6), "qty": None},
    ]
    assert found.source == "sheet «Bonds»"


def test_a_csv_or_text_list_still_reads_and_drops_what_is_sold() -> None:
    raw = "code;name;qty\nRU000A0ZYQY7;АФК Система, 001P-07;3\nRU000A0JXN21;;0\n".encode("cp1251")
    found = read_portfolio(raw)
    assert [(it["code"], it["qty"]) for it in found.items] == [("RU000A0ZYQY7", 3.0)]
    assert found.sold_out == 1 and found.source == ""
    assert read_portfolio(b"SU26238RMFS4 2\n").items[0]["qty"] == 2.0


@pytest.mark.parametrize("raw, says", [
    (workbook({"Sheet1": [["a", "b"], [1, 2]]}), "column of bond codes"),
    (b"PK\x03\x04 not really a zip", "not a spreadsheet"),
])
def test_a_spreadsheet_without_bonds_is_refused_with_a_reason(raw, says) -> None:
    with pytest.raises(ReportError, match=says):
        read_portfolio(raw)


def test_a_part_that_declares_entities_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(intelinvest_report())) as src, zipfile.ZipFile(buf, "w") as z:
        for name in src.namelist():
            data = src.read(name)
            if name == "xl/workbook.xml":
                data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + data
            z.writestr(name, data)
    with pytest.raises(ReportError, match="does not accept"):
        read_portfolio(buf.getvalue())
