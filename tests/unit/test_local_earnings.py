from __future__ import annotations

from datetime import date
from pathlib import Path

from wheel_screener.adapters.local.earnings import LocalEarningsCalendar


def test_reads_and_filters_to_window(tmp_path: Path) -> None:
    cal = tmp_path / "cal.csv"
    cal.write_text("symbol,date\nAAA,2026-07-01\nBBB,2026-09-01\nCCC,bad-date\n")
    out = LocalEarningsCalendar(str(cal)).earnings_calendar(date(2026, 6, 22), date(2026, 8, 6))
    assert out == {"AAA": date(2026, 7, 1)}  # BBB outside window, CCC unparseable


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    reader = LocalEarningsCalendar(str(tmp_path / "nope.csv"))
    assert reader.earnings_calendar(date(2026, 1, 1), date(2026, 12, 31)) == {}
