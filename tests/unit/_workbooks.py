"""Workbooks for tests: .xlsx files built in code, shaped like the portfolio reports Intelinvest
(intelinvest.ru) exports — so the reader is tested on the real layout without anyone's real
holdings in the repository."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

HEADER = ["Тикер", "Название", "Количество, шт.", "Средняя цена", "Текущая цена"]


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def workbook(sheets: dict[str, list[list]], *, shared: bool = False) -> bytes:
    """A workbook as Apache POI writes Intelinvest's: inline strings (or a shared-string table),
    numbers as numbers."""
    strings: list[str] = []
    parts: dict[str, str] = {}
    for n, rows in enumerate(sheets.values(), 1):
        xml_rows = []
        for r, row in enumerate(rows, 1):
            cells = []
            for i, v in enumerate(row):
                ref = f"{_col(i)}{r}"
                if isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}" t="n"><v>{v}</v></c>')
                elif shared:
                    strings.append(v)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        parts[f"xl/worksheets/sheet{n}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns='
            '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(xml_rows) + "</sheetData></worksheet>")
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts["xl/workbook.xml"] = (
        f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
        + "".join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>'
                  for n, name in enumerate(sheets, 1))
        + "</sheets></workbook>")
    parts["xl/_rels/workbook.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml" Type="x"/>'
                  for n in range(1, len(sheets) + 1))
        + "</Relationships>")
    if shared:
        parts["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{main}">' + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
            + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def intelinvest_report(**kw) -> bytes:
    return workbook({
        # its trades come first, and their header names a ticker too — not the holdings
        "Сделки": [["intelinvest.ru"], ["Тикер", "Название", "Операция", "Дата", "Кол-во"],
                   ["RU000A0JQS09", "РСХБ 10", "Покупка", "2017-08-02", 30.0]],
        "Портфель акций": [["intelinvest.ru"], HEADER, ["SBER", "Сбербанк", 10.0, 220.59]],
        "Портфель облигаций": [
            ["intelinvest.ru"], HEADER,
            ["RU000A10EMG2", "НоваБев3Р3", 120.0, 100.25, 98.75],
            ["RU000A0JS3W6", "ОФЗ 26207", 45.0, 95.1, 96.0],
            ["RU000A0JVUP7", "ВымпелКБО3", 0.0, 100.65, 101.0],  # sold out
        ],
        "Все бумаги": [["intelinvest.ru"], ["Тип", *HEADER]],
    }, **kw)
