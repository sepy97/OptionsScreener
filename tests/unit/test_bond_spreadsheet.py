"""The calendar's Excel workbook, read back with the app's own .xlsx reader."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds.holdings import by_portfolio
from wheel_screener.bonds.reports import Workbook
from wheel_screener.bonds.spreadsheet import (
    GROUPED_SHEETS,
    SHEET,
    calendar_xlsx,
    grouped_lines,
    grouped_xlsx,
)

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
    raw = calendar_xlsx([_event(portfolio="ГПБ"),
                         _event(kind=bc.OFFER_LAST_DAY, amount=None, cur="", rate=None,
                                qty=None, total=None, note="")])
    book = Workbook(raw)
    assert book.sheet_names == [SHEET]
    head, coupon, offer = book.rows(SHEET)
    assert head == bc.COLUMNS
    assert coupon == ["46397", bc.COUPON, "АФК Система, 001P-06", "RU000A0JXN21",
                      "RU000A0JXN21", "35.78", "RUB", "14.35", "ГПБ", "1500.0", "53670.0",
                      "record date 2027-01-09"]
    assert offer == ["46397", bc.OFFER_LAST_DAY, *coupon[2:5]]  # nothing else: no empty cells
    fmt = _cell_formats(raw)
    assert fmt["A1"] == ("General", True)  # the header
    assert fmt["A2"] == ("short date", False)  # 46397 is 2027-01-10, shown in Excel's locale
    assert fmt["F2"] == ('#,##0.00##" ₽"', False) and fmt["K2"] == ('#,##0.00##" ₽"', True)
    assert fmt["H2"] == ("0.00##", False) and fmt["J2"] == ("#,##0", False)
    assert fmt["I2"] == ("General", False)  # the portfolio


def test_each_currency_shows_its_own_sign_or_code() -> None:
    raw = calendar_xlsx([_event(cur="USD"), _event(cur="CHF"), _event(cur=None),
                         _event(qty=0.5)])
    fmt = _cell_formats(raw)
    assert fmt["F2"][0] == '#,##0.00##" $"' and fmt["F3"][0] == '#,##0.00##" CHF"'
    assert fmt["F4"][0] == "#,##0.00##"  # no currency: a plain number
    assert fmt["J5"][0] == "General"  # a part of a bond, not cut to a whole one


def test_a_total_is_written_without_float_noise() -> None:
    raw = calendar_xlsx([_event(amount=17.67, qty=40.0, total=17.67 * 40)])  # 706.8000000000001
    assert Workbook(raw).rows(SHEET)[1][10] == "706.8"


def test_any_text_is_kept_as_written() -> None:
    odd = ' Bond <A> & "B"\x07 '  # markup, quotes, a control character XML can't hold, spaces
    raw = calendar_xlsx([_event(name=odd)])
    assert Workbook(raw).rows(SHEET)[1][2] == ' Bond <A> & "B" '


def test_the_header_stays_in_view_and_filters() -> None:
    z = zipfile.ZipFile(io.BytesIO(calendar_xlsx([_event()] * 3)))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert 'state="frozen"' in sheet and '<autoFilter ref="A1:L4"/>' in sheet
    assert "Bonds!$A$1:$L$4" in z.read("xl/workbook.xml").decode()


def test_an_empty_calendar_is_a_header_and_the_same_file_each_time() -> None:
    raw = calendar_xlsx([])
    assert Workbook(raw).rows(SHEET) == [bc.COLUMNS]
    assert calendar_xlsx([_event()]) == calendar_xlsx([_event()])


# ---- the grouped export ----

def _serial(d: date) -> str:
    return str((d - date(1899, 12, 30)).days)


def test_grouped_export_puts_coupons_and_the_rest_on_two_sheets_a_line_per_portfolio() -> None:
    day = date(2026, 10, 25)
    coupon = _event(date=day, name="АФК Система, 002P-03", short="Систем2P03", amount=17.67,
                    rate=21.5, holders=[("ГПБ", 1000.0), ("ИИС", 5.0)])
    redemption = _event(date=date(2027, 4, 22), kind=bc.REDEMPTION, short="Роснфт1P4",
                        amount=1000.0, rate=None, holders=[("ГПБ", 1014.0)])
    last_day = _event(date=date(2027, 3, 5), kind=bc.OFFER_LAST_DAY, short="Роснфт1P4",
                      amount=None, cur="", rate=None, holders=[("ГПБ", 1014.0)])
    raw = grouped_xlsx(by_portfolio([coupon, last_day, redemption]))
    book = Workbook(raw)
    assert book.sheet_names == list(GROUPED_SHEETS)
    coupons, other = (book.rows(name) for name in GROUPED_SHEETS)
    assert coupons == [[_serial(day), "Систем2P03", "21,50", "ГПБ", "17670"],
                       [_serial(day), "Систем2P03", "21,50", "ИИС", "88,35"]]  # no header
    assert other == [[_serial(date(2027, 3, 5)), "Роснфт1P4", bc.OFFER_LAST_DAY, "ГПБ"],
                     [_serial(date(2027, 4, 22)), "Роснфт1P4", bc.REDEMPTION, "ГПБ", "1014000"]]
    z = zipfile.ZipFile(io.BytesIO(raw))
    styles = ET.fromstring(z.read("xl/styles.xml"))
    assert [f.get("formatCode") for f in styles.iter(MAIN + "numFmt")] == ["dd.mm.yyyy"]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    a1 = next(c for c in sheet.iter(MAIN + "c") if c.get("r") == "A1")
    xfs = styles.find(MAIN + "cellXfs").findall(MAIN + "xf")
    assert xfs[int(a1.get("s"))].get("numFmtId") == "164"  # 25.10.2026 in any locale


def test_grouped_lines_fall_back_and_name_a_foreign_currency() -> None:
    no_short = _event(holders=[("Main", None)])  # a build cached before short names
    dollars = _event(cur="USD", amount=25.5, short="X", holders=[("Main", 3.0)])
    unannounced = _event(amount=None, rate=None, short="Y")  # no holders: the event's own qty
    coupons, other = grouped_lines(by_portfolio([no_short, dollars, unannounced]))
    assert [line[1:] for line in coupons] == [
        ["АФК Система, 001P-06", "14,35", "Main", ""], ["X", "14,35", "Main", "76,50 USD"],
        ["Y", "", "", ""]]
    assert other == []


def test_an_empty_grouped_export_is_two_empty_sheets() -> None:
    book = Workbook(grouped_xlsx([]))
    assert [book.rows(name) for name in GROUPED_SHEETS] == [[], []]
    assert grouped_xlsx([_event()]) == grouped_xlsx([_event()])
