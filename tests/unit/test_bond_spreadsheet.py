"""The calendar's Excel workbook, read back with the app's own .xlsx reader."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds.reports import Workbook
from wheel_screener.bonds.spreadsheet import SHEET, calendar_xlsx

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _event(**kw) -> dict:
    e = {"date": date(2027, 1, 10), "kind": bc.COUPON, "name": "АФК Система, 001P-06",
         "isin": "RU000A0JXN21", "secid": "RU000A0JXN21", "amount": 35.78, "cur": "RUB",
         "rate": 14.35, "qty": 1500.0, "total": 53670.0, "note": "record date 2027-01-09"}
    return {**e, **kw}


def _cell_formats(raw: bytes) -> dict[str, tuple[str, bool]]:
    """Each cell's number format and whether it is bold, by its reference."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    styles = ET.fromstring(z.read("xl/styles.xml"))
    codes = {f.get("numFmtId"): f.get("formatCode") for f in styles.iter(MAIN + "numFmt")}
    codes |= {"0": "General", "3": "#,##0", "14": "short date"}
    bold = [f.find(MAIN + "b") is not None for f in styles.iter(MAIN + "font")]
    xfs = [(codes[x.get("numFmtId")], bold[int(x.get("fontId"))])
           for x in styles.find(MAIN + "cellXfs").findall(MAIN + "xf")]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    return {c.get("r"): xfs[int(c.get("s"))] for c in sheet.iter(MAIN + "c")}


def test_the_table_is_the_csvs_with_real_dates_and_numbers() -> None:
    raw = calendar_xlsx([_event(), _event(kind=bc.OFFER_LAST_DAY, amount=None, cur="",
                                          rate=None, qty=None, total=None, note="")])
    book = Workbook(raw)
    assert book.sheet_names == [SHEET]
    head, coupon, offer = book.rows(SHEET)
    assert head == bc.COLUMNS
    assert coupon == ["46397", bc.COUPON, "АФК Система, 001P-06", "RU000A0JXN21",
                      "RU000A0JXN21", "35.78", "RUB", "14.35", "1500.0", "53670.0",
                      "record date 2027-01-09"]
    assert offer == ["46397", bc.OFFER_LAST_DAY, *coupon[2:5]]  # nothing else: no empty cells
    fmt = _cell_formats(raw)
    assert fmt["A1"] == ("General", True)  # the header
    assert fmt["A2"] == ("short date", False)  # 46397 is 2027-01-10, shown in Excel's locale
    assert fmt["F2"] == ('#,##0.00##" ₽"', False) and fmt["J2"] == ('#,##0.00##" ₽"', True)
    assert fmt["H2"] == ("0.00##", False) and fmt["I2"] == ("#,##0", False)


def test_each_currency_shows_its_own_sign_or_code() -> None:
    raw = calendar_xlsx([_event(cur="USD"), _event(cur="CHF"), _event(cur=None),
                         _event(qty=0.5)])
    fmt = _cell_formats(raw)
    assert fmt["F2"][0] == '#,##0.00##" $"' and fmt["F3"][0] == '#,##0.00##" CHF"'
    assert fmt["F4"][0] == "#,##0.00##"  # no currency: a plain number
    assert fmt["I5"][0] == "General"  # a part of a bond, not cut to a whole one


def test_a_total_is_written_without_float_noise() -> None:
    raw = calendar_xlsx([_event(amount=17.67, qty=40.0, total=17.67 * 40)])  # 706.8000000000001
    assert Workbook(raw).rows(SHEET)[1][9] == "706.8"


def test_any_text_is_kept_as_written() -> None:
    odd = ' Bond <A> & "B"\x07 '  # markup, quotes, a control character XML can't hold, spaces
    raw = calendar_xlsx([_event(name=odd)])
    assert Workbook(raw).rows(SHEET)[1][2] == ' Bond <A> & "B" '


def test_the_header_stays_in_view_and_filters() -> None:
    z = zipfile.ZipFile(io.BytesIO(calendar_xlsx([_event()] * 3)))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert 'state="frozen"' in sheet and '<autoFilter ref="A1:K4"/>' in sheet
    assert "Bonds!$A$1:$K$4" in z.read("xl/workbook.xml").decode()


def test_an_empty_calendar_is_a_header_and_the_same_file_each_time() -> None:
    raw = calendar_xlsx([])
    assert Workbook(raw).rows(SHEET) == [bc.COLUMNS]
    assert calendar_xlsx([_event()]) == calendar_xlsx([_event()])
