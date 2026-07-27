from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from wheel_screener.adapters.local.earnings import (
    LocalEarningsCalendar,
    assert_near_term_coverage,
    write_calendar,
)
from wheel_screener.core.errors import ProviderDataError


def _write(tmp_path: Path, calendar: dict[str, date], *, fetched: date, days: int = 120) -> str:
    path = str(tmp_path / "cal.csv")
    write_calendar(
        path, calendar, covers_from=fetched, covers_to=fetched + timedelta(days=days),
        fetched=fetched,
    )
    return path


def test_reads_and_filters_to_window(tmp_path: Path) -> None:
    today = date.today()
    path = _write(
        tmp_path,
        {"AAA": today + timedelta(days=9), "BBB": today + timedelta(days=71)},
        fetched=today,
    )
    out = LocalEarningsCalendar(path).earnings_calendar(today, today + timedelta(days=45))
    assert out == {"AAA": today + timedelta(days=9)}  # BBB is outside the requested window


def test_missing_file_raises_instead_of_returning_empty(tmp_path: Path) -> None:
    """An empty dict downstream means 'nobody reports' — the exact silent failure of issue #113."""
    reader = LocalEarningsCalendar(str(tmp_path / "nope.csv"))
    with pytest.raises(ProviderDataError, match="no earnings calendar"):
        reader.earnings_calendar(date(2026, 1, 1), date(2026, 12, 31))


def test_stale_file_raises(tmp_path: Path) -> None:
    today = date.today()
    path = _write(tmp_path, {"AAA": today + timedelta(days=9)}, fetched=today - timedelta(days=10))
    with pytest.raises(ProviderDataError, match="too stale"):
        LocalEarningsCalendar(path).earnings_calendar(today, today + timedelta(days=45))


def test_window_beyond_coverage_raises(tmp_path: Path) -> None:
    today = date.today()
    path = _write(tmp_path, {"AAA": today + timedelta(days=9)}, fetched=today, days=30)
    with pytest.raises(ProviderDataError, match="does not span"):
        LocalEarningsCalendar(path).earnings_calendar(today, today + timedelta(days=120))


def test_legacy_file_without_provenance_raises(tmp_path: Path) -> None:
    cal = tmp_path / "cal.csv"
    cal.write_text("symbol,date\nAAA,2026-07-01\n")  # the pre-#113 format
    with pytest.raises(ProviderDataError, match="coverage header"):
        LocalEarningsCalendar(str(cal)).earnings_calendar(date(2026, 6, 22), date(2026, 8, 6))


def test_next_earnings_for_one_symbol(tmp_path: Path) -> None:
    today = date.today()
    path = _write(tmp_path, {"AAA": today + timedelta(days=9)}, fetched=today)
    reader = LocalEarningsCalendar(path)
    assert reader.next_earnings("AAA", today) == today + timedelta(days=9)
    assert reader.next_earnings("AAA", today + timedelta(days=30)) is None  # already past
    assert reader.next_earnings("ZZZ", today) is None


def test_write_is_atomic_and_roundtrips(tmp_path: Path) -> None:
    today = date.today()
    path = _write(
        tmp_path, {"BBB": today + timedelta(days=3), "AAA": today + timedelta(days=1)},
        fetched=today,
    )
    assert not list(tmp_path.glob("*.tmp"))  # no partial file left behind
    out = LocalEarningsCalendar(path).earnings_calendar(today, today + timedelta(days=45))
    assert out == {"AAA": today + timedelta(days=1), "BBB": today + timedelta(days=3)}


def test_near_term_coverage_check_rejects_a_truncated_calendar() -> None:
    """The #113 signature: thousands of symbols, none of them reporting any time soon."""
    today = date(2026, 7, 25)
    far = {f"S{i}": today + timedelta(days=60 + i % 30) for i in range(3754)}
    with pytest.raises(ProviderDataError, match="0 reporters"):
        assert_near_term_coverage(far, today)
    assert_near_term_coverage({**far, "RDDT": today + timedelta(days=5)}, today)  # fine
