"""The calendar as Excel workbooks.

Both take ``holdings.by_portfolio``'s lines: an event of a bond held in two portfolios is two
lines, each with its portfolio's quantity and total.

``calendar_xlsx`` is the CSV's table, but with dates and numbers Excel reads as its own, so it
opens the same in any locale — a CSV's columns and decimals depend on Excel's. One sheet,
«Bonds»: a bold header that stays in view and filters, then a row per line. Dates use Excel's
own short date, amounts a money format with their currency's sign, and Excel shows both the way
the computer's locale does (120 000,00 ₽ in a Russian one).

``grouped_xlsx`` is the grouped export: coupons on «Купоны», every other event on «Другие
события», in the person's own format —
``25.10.2026  Систем2P03  21,50  ГПБ  17670``: the date, MOEX's short name, the coupon's rate
(on the second sheet, what the event is), the portfolio and what it receives. No header, so
every line is in that format; the numbers are text, written the Russian way, so they read the
same in any locale.

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
GROUPED_SHEETS = ("Купоны", "Другие события")
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_EXCEL_EPOCH = date(1899, 12, 30)  # day 0 of Excel's date numbers
_WIDTHS = [11, 30, 22, 15, 15, 13, 9, 14, 16, 8, 15, 60]  # COLUMNS', in characters
_GROUPED_WIDTHS = [11, 16, 30, 16, 14]
# a currency's sign, on the page too; one without a sign shows as its code
CURRENCY_SIGNS = {"RUB": "₽", "SUR": "₽", "USD": "$", "EUR": "€", "CNY": "¥"}
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")  # control characters XML can't hold

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# cell styles (cellXfs), in order; a currency's money styles follow, plain then bold
_PLAIN, _HEADER, _DATE, _RATE, _COUNT = range(5)
_RU_DATE = 1  # the grouped export's date: 25.10.2026 in any locale
_CUSTOM_FMT = 164  # the first custom number format
_RATE_FMT = _CUSTOM_FMT  # the calendar's; the currencies' come after it


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


def _worksheet(rows: list[str], widths: list[int], *, header: bool = False) -> str:
    """A sheet of rows; with a ``header``, its first row stays in view and filters."""
    last = f"{_col(len(widths) - 1)}{max(len(rows), 1)}"
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths, start=1))
    view = ('<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            "</sheetView></sheetViews>") if header else ""
    tail = f'<autoFilter ref="A1:{last}"/>' if header else ""
    return (
        f'{_HEAD}<worksheet xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        f'<dimension ref="A1:{last}"/>{view}'
        f'<cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData>{tail}</worksheet>'
    )


def _styles(formats: list[str], xfs: list[tuple[int, int]]) -> str:
    """Custom number formats (ids from 164, in order) and the cell styles, each a (number
    format id, font) — font 0 regular, 1 bold."""
    num_fmts = "".join(f'<numFmt numFmtId="{_CUSTOM_FMT + i}" formatCode={quoteattr(f)}/>'
                       for i, f in enumerate(formats))
    xf = ('<xf numFmtId="{}" fontId="{}" fillId="0" borderId="0" xfId="0" '
          'applyNumberFormat="1" applyFont="1"/>')
    return (
        f'{_HEAD}<styleSheet xmlns="{_MAIN}">'
        f'<numFmts count="{len(formats)}">{num_fmts}</numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        f'<cellXfs count="{len(xfs)}">{"".join(xf.format(*x) for x in xfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _package(sheets: list[tuple[str, str]], styles: str, defined_names: str = "") -> bytes:
    """The .xlsx around its sheets — (name, worksheet XML) each — and their styles."""
    n = len(sheets)
    workbook = (
        f'{_HEAD}<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<bookViews><workbookView/></bookViews><sheets>'
        + "".join(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>'
                  for i, (name, _) in enumerate(sheets, start=1))
        + f"</sheets>{defined_names}</workbook>"
    )
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    parts = {
        "[Content_Types].xml": (
            f'{_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                      f'ContentType="{sheet_type}"/>' for i in range(1, n + 1))
            + '<Override PartName="/xl/styles.xml" '
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
            + "".join(f'<Relationship Id="rId{i}" Type="{_DOC_REL}/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
            + f'<Relationship Id="rId{n + 1}" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": styles,
        **{f"xl/worksheets/sheet{i}.xml": xml for i, (_, xml) in enumerate(sheets, start=1)},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            # a fixed timestamp: the same calendar makes the same file
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), xml.encode("utf-8"),
                       zipfile.ZIP_DEFLATED)
    return buf.getvalue()


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
        cells.append(_text(f"I{n}", e.get("portfolio") or ""))
        if (qty := e["qty"]) is not None:
            cells.append(_number(f"J{n}", qty, _COUNT if qty == int(qty) else _PLAIN))
        if e["total"] is not None:
            cells.append(_number(f"K{n}", e["total"], money[cur] + 1))
        cells.append(_text(f"L{n}", e["note"] or ""))
        rows.append(f'<row r="{n}">' + "".join(cells) + "</row>")

    last = f"L{len(rows)}"
    formats = ["0.00##"] + [_money_format(cur) for cur in currencies]
    xfs = [(0, 0), (0, 1), (14, 0), (_RATE_FMT, 0), (3, 0)]  # 14: the locale's short date
    for i in range(len(currencies)):
        xfs += [(_RATE_FMT + 1 + i, 0), (_RATE_FMT + 1 + i, 1)]
    filter_range = ('<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" '
                    f'hidden="1">{SHEET}!$A$1:${last[0]}${last[1:]}</definedName></definedNames>')
    return _package([(SHEET, _worksheet(rows, _WIDTHS, header=True))],
                    _styles(formats, xfs), filter_range)


def _rate(x: float | None) -> str:
    """14.2 -> '14,20'."""
    return "" if x is None else f"{x:.2f}".replace(".", ",")


def _sum(x: float | None, cur: str | None) -> str:
    """17670.0 -> '17670', 35388.6 -> '35388,60': kopecks only when there are some, no digit
    groups; a currency other than the rouble is named."""
    if x is None:
        return ""
    x = round(x, 2)
    s = f"{x:.0f}" if x == int(x) else f"{x:.2f}".replace(".", ",")
    return s if not cur or cur == "RUB" else f"{s} {cur}"


def grouped_lines(events: list[dict]) -> tuple[list[list], list[list]]:
    """The grouped export's coupons and other events: [date, short name, rate or event,
    portfolio, amount] each, in the events' order."""
    coupons: list[list] = []
    other: list[list] = []
    for e in events:
        coupon = e["kind"] == bc.COUPON
        (coupons if coupon else other).append([
            e["date"], e.get("short") or e["name"], _rate(e["rate"]) if coupon else e["kind"],
            e.get("portfolio") or "", _sum(e["total"], e["cur"]),
        ])
    return coupons, other


def _lines_sheet(lines: list[list]) -> str:
    rows = []
    for n, (day, *texts) in enumerate(lines, start=1):
        cells = [_number(f"A{n}", (day - _EXCEL_EPOCH).days, _RU_DATE)]
        cells += [_text(f"{_col(i)}{n}", s) for i, s in enumerate(texts, start=1)]
        rows.append(f'<row r="{n}">' + "".join(cells) + "</row>")
    return _worksheet(rows, _GROUPED_WIDTHS)


def grouped_xlsx(events: list[dict]) -> bytes:
    sheets = [(name, _lines_sheet(lines))
              for name, lines in zip(GROUPED_SHEETS, grouped_lines(events), strict=True)]
    return _package(sheets, _styles(["dd.mm.yyyy"], [(0, 0), (_CUSTOM_FMT, 0)]))
