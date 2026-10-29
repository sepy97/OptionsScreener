"""The calendar as an Excel workbook: the CSV's table, but with dates and numbers Excel reads as
its own, so it opens the same in any locale — a CSV's columns and decimals depend on Excel's.

One sheet, «Bonds»: a bold header that stays in view and filters, then a row per event. Dates use
Excel's own short date, amounts a money format with their currency's sign, and Excel shows both
the way the computer's locale does (120 000,00 ₽ in a Russian one).

An .xlsx is a zip of XML parts, written here with the standard library, as reports.py reads one:
only the parts Excel needs, strings inline rather than in a shared table.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from xml.sax.saxutils import escape, quoteattr

from wheel_screener.bonds import bond_calendar as bc

SHEET = "Bonds"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_EXCEL_EPOCH = date(1899, 12, 30)  # day 0 of Excel's date numbers
_WIDTHS = [11, 30, 22, 15, 15, 13, 9, 14, 8, 15, 60]  # COLUMNS', in characters
# a currency's sign, on the page too; one without a sign shows as its code
CURRENCY_SIGNS = {"RUB": "₽", "SUR": "₽", "USD": "$", "EUR": "€", "CNY": "¥"}
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")  # control characters XML can't hold

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# cell styles (cellXfs), in order; a currency's money styles follow, plain then bold
_PLAIN, _HEADER, _DATE, _RATE, _COUNT = range(5)
_RATE_FMT = 164  # the first custom number format; the currencies' come after it


def _col(i: int) -> str:
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]


def _text(ref: str, s: str, style: int = _PLAIN) -> str:
    s = _NOT_XML.sub("", s)
    if not s:
        return ""  # an empty cell is left out
    space = ' xml:space="preserve"' if s != s.strip() else ""
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t{space}>{escape(s)}</t></is></c>'


def _number(ref: str, x: float, style: int) -> str:
    # rounded past any amount's digits: a total is a product, 706.8000000000001 without it
    return f'<c r="{ref}" s="{style}"><v>{round(x, 10)!r}</v></c>'


def _money_format(cur: str) -> str:
    sign = CURRENCY_SIGNS.get(cur, cur)
    return f'#,##0.00##" {sign}"' if sign else "#,##0.00##"


def calendar_xlsx(events: list[dict]) -> bytes:
    currencies = sorted({e["cur"] or "" for e in events})
    money = {cur: 5 + 2 * i for i, cur in enumerate(currencies)}  # its plain style; bold is +1

    rows = ['<row r="1">' + "".join(_text(f"{_col(i)}1", name, _HEADER)
                                    for i, name in enumerate(bc.COLUMNS)) + "</row>"]
    for n, e in enumerate(events, start=2):
        cells = [_number(f"A{n}", (e["date"] - _EXCEL_EPOCH).days, _DATE)]
        for col, value in zip("BCDE", (e["kind"], e["name"], e["isin"], e["secid"]), strict=True):
            cells.append(_text(f"{col}{n}", value or ""))
        cur = e["cur"] or ""
        if e["amount"] is not None:
            cells.append(_number(f"F{n}", e["amount"], money[cur]))
        cells.append(_text(f"G{n}", cur))
        if e["rate"] is not None:
            cells.append(_number(f"H{n}", e["rate"], _RATE))
        if (qty := e["qty"]) is not None:
            cells.append(_number(f"I{n}", qty, _COUNT if qty == int(qty) else _PLAIN))
        if e["total"] is not None:
            cells.append(_number(f"J{n}", e["total"], money[cur] + 1))
        cells.append(_text(f"K{n}", e["note"] or ""))
        rows.append(f'<row r="{n}">' + "".join(cells) + "</row>")

    last = f"K{len(events) + 1}"
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(_WIDTHS, start=1))
    sheet = (
        f'{_HEAD}<worksheet xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        f'<dimension ref="A1:{last}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f'<cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData>'
        f'<autoFilter ref="A1:{last}"/></worksheet>'
    )

    formats = ["0.00##"] + [_money_format(cur) for cur in currencies]
    num_fmts = "".join(f'<numFmt numFmtId="{_RATE_FMT + i}" formatCode={quoteattr(f)}/>'
                       for i, f in enumerate(formats))
    xf = ('<xf numFmtId="{}" fontId="{}" fillId="0" borderId="0" xfId="0" '
          'applyNumberFormat="1" applyFont="1"/>')
    xfs = [xf.format(0, 0), xf.format(0, 1), xf.format(14, 0), xf.format(_RATE_FMT, 0),
           xf.format(3, 0)]  # 14: the locale's short date; 3: #,##0
    for i in range(len(currencies)):
        xfs += [xf.format(_RATE_FMT + 1 + i, 0), xf.format(_RATE_FMT + 1 + i, 1)]
    styles = (
        f'{_HEAD}<styleSheet xmlns="{_MAIN}">'
        f'<numFmts count="{len(formats)}">{num_fmts}</numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )

    workbook = (
        f'{_HEAD}<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<bookViews><workbookView/></bookViews>'
        f'<sheets><sheet name="{SHEET}" sheetId="1" r:id="rId1"/></sheets>'
        '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
        f"{SHEET}!$A$1:${last[0]}${last[1:]}</definedName></definedNames></workbook>"
    )
    parts = {
        "[Content_Types].xml": (
            f'{_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'{_HEAD}<Relationships xmlns="{_PKG_REL}">'
            f'<Relationship Id="rId1" Type="{_DOC_REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": (
            f'{_HEAD}<Relationships xmlns="{_PKG_REL}">'
            f'<Relationship Id="rId1" Type="{_DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            # a fixed timestamp: the same calendar makes the same file
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), xml.encode("utf-8"),
                       zipfile.ZIP_DEFLATED)
    return buf.getvalue()
