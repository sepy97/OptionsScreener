"""Reading a portfolio from a file: an Intelinvest portfolio report, or a list in the script's way.

The portfolio report Intelinvest (intelinvest.ru) exports — "complex_portfolio_report_<id>.xlsx" —
has a sheet per kind of holding. Its bonds are on «Портфель облигаций»: a title row
(«intelinvest.ru»), then a header — «Тикер» (the ISIN, OFZ too), «Название» (MOEX's short name),
«Количество, шт.», and profit columns we don't need — then a row per bond ever held. Bonds sold
out stay on the sheet with a quantity of 0; a portfolio holds only the rest.

Intelinvest names the file after the portfolio's number in it (11111 in
complex_portfolio_report_11111.xlsx) and writes nothing inside that tells one portfolio's report
from another's — so ``report_number`` reads it from the file's name, to tell a newer download of
the same report from another portfolio's.

Other spreadsheets work the same way: the first sheet whose header names a code or name column,
read like the script's CSV (``bond_calendar.parse_rows``). Anything that isn't a spreadsheet is
read as the script's CSV or plain-text list.

An .xlsx is a zip of XML parts, read here with the standard library: only the few parts needed,
each capped in size, and none that declares a DTD (a spreadsheet never needs one).
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta

from wheel_screener.bonds import bond_calendar as bc

BOND_SHEET = "Портфель облигаций"  # Intelinvest's sheet of bonds
_PART_BYTES = 16 * 1024 * 1024  # a report's largest sheet (every trade) is ~4 MB of XML
_MAX_ROWS = 10_000  # a bond sheet is a row per bond ever held
_MAX_COLUMNS = 64
_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_EXCEL_EPOCH = date(1899, 12, 30)  # day 0 of Excel's date numbers
_REPORT_NAME = re.compile(r"complex_portfolio_report_(\d+)", re.IGNORECASE)


class ReportError(ValueError):
    """A file that can't be read as a portfolio; the message says why, in words for the page."""


@dataclass
class PortfolioFile:
    items: list[dict]  # the bonds held: code, name, matdate, qty
    sold_out: int = 0  # bonds on the sheet with a quantity of 0, left out
    source: str = ""  # where in the file they came from, for the page


def file_name(raw: str | None) -> str:
    """An uploaded file's name as the page shows it: no folders (a browser may send them), no
    control characters, at most 120 characters."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in name if ch.isprintable()).strip()[:120]


def report_number(name: str) -> str:
    """The Intelinvest portfolio number in a report's file name — also in a renamed copy, such
    as «complex_portfolio_report_11111 (1).xlsx» — or "" for any other file."""
    m = _REPORT_NAME.search(name or "")
    return m.group(1) if m else ""


def is_xlsx(raw: bytes) -> bool:
    return raw[:4] == b"PK\x03\x04"


def _part(z: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ReportError("That spreadsheet is missing a part — is it an .xlsx file?") from None
    if info.file_size > _PART_BYTES:
        raise ReportError("That spreadsheet is too large to read.")
    with z.open(info) as f:
        data = f.read(_PART_BYTES + 1)
    if len(data) > _PART_BYTES:
        raise ReportError("That spreadsheet is too large to read.")
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:
        raise ReportError("That spreadsheet has parts this reader does not accept.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise ReportError("That spreadsheet could not be read.") from None


def _column(ref: str | None) -> int | None:
    """"C12" → 2; None for a reference that isn't one."""
    m = re.match(r"[A-Z]{1,3}(?=\d)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell(c: ET.Element, shared: list[str]) -> str:
    kind = c.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter(_MAIN + "t"))
    v = c.find(_MAIN + "v")
    if v is None or v.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _rows(sheet: ET.Element, shared: list[str]) -> list[list[str]]:
    rows = []
    for row in sheet.iter(_MAIN + "row"):
        if len(rows) >= _MAX_ROWS:
            break
        cells: list[str] = []
        for c in row.findall(_MAIN + "c"):
            i = _column(c.get("r"))
            i = len(cells) if i is None else i
            if i >= _MAX_COLUMNS:
                continue
            if i >= len(cells):
                cells.extend([""] * (i - len(cells) + 1))
            cells[i] = _cell(c, shared)
        rows.append(cells)
    return rows


class Workbook:
    """An .xlsx's sheet names, and any sheet's rows of cell text — read only when asked for."""

    def __init__(self, raw: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise ReportError("That file is not a spreadsheet that can be read.") from None
        names = self._zip.namelist()
        self._shared = []
        if "xl/sharedStrings.xml" in names:
            self._shared = ["".join(t.text or "" for t in si.iter(_MAIN + "t"))
                            for si in _part(self._zip, "xl/sharedStrings.xml")
                            .findall(_MAIN + "si")]
        targets = {r.get("Id"): r.get("Target", "") for r in
                   _part(self._zip, "xl/_rels/workbook.xml.rels").iter(_PKG_REL + "Relationship")}
        self._paths: dict[str, str] = {}
        for sheet in _part(self._zip, "xl/workbook.xml").iter(_MAIN + "sheet"):
            target = targets.get(sheet.get(_REL + "id"), "").lstrip("/")
            self._paths[sheet.get("name", "")] = target if target.startswith("xl/") else (
                f"xl/{target}")

    @property
    def sheet_names(self) -> list[str]:
        return list(self._paths)

    def rows(self, name: str) -> list[list[str]]:
        return _rows(_part(self._zip, self._paths[name]), self._shared)


def _excel_dates(rows: list[list[str]]) -> list[list[str]]:
    """A maturity column of Excel date numbers (46107) as ISO dates, as the parser reads them."""
    header = [c.strip().lower() for c in rows[0]]
    mat = next((i for i, c in enumerate(header) if c in bc.MAT_COLUMNS), None)
    if mat is None:
        return rows
    out = [rows[0]]
    for row in rows[1:]:
        value = row[mat] if mat < len(row) else ""
        if re.fullmatch(r"\d{4,6}(\.0+)?", value.strip()):
            row = [*row]
            row[mat] = (_EXCEL_EPOCH + timedelta(days=int(float(value)))).isoformat()
        out.append(row)
    return out


def _bond_table(book: Workbook) -> tuple[str, list[list[str]]]:
    """The sheet with the bonds, from its header row down: Intelinvest's bond sheet if there is
    one, else the first sheet with a header naming a code or name column."""
    for name in sorted(book.sheet_names, key=lambda name: name != BOND_SHEET):
        rows = book.rows(name)
        start = next((i for i, row in enumerate(rows[:20]) if bc.is_header(row)), None)
        if start is not None:
            return name, rows[start:]
    raise ReportError("No sheet in that spreadsheet has a column of bond codes or names "
                      "(«Тикер», ISIN, code or name).")


def read_portfolio(raw: bytes) -> PortfolioFile:
    """The bonds a file says are held, from a spreadsheet or a CSV / text list."""
    if is_xlsx(raw):
        name, rows = _bond_table(Workbook(raw))
        items = bc.parse_rows(_excel_dates(rows))
        source = f"sheet «{name}»"
    else:
        items = bc.parse_watchlist(decode(raw))
        source = ""
    held = [it for it in items if it["qty"] is None or it["qty"] > 0]
    return PortfolioFile(held, len(items) - len(held), source)


def decode(raw: bytes) -> str:
    """cbonds and Excel exports are UTF-8 or, in Russian Windows locales, cp1251."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")
