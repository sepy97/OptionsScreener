"""Local earnings-calendar store — reads the CSV written by the `refresh-earnings` job.

One row per symbol with its next earnings date. Satisfies the same duck-typed interface
as the live FMP earnings source (``earnings_calendar(start, end) -> {symbol: date}``), so
it drops into the screener's blackout with zero API calls.
"""

from __future__ import annotations

import csv
import os
from datetime import date
from pathlib import Path


class LocalEarningsCalendar:
    def __init__(self, path: str) -> None:
        self._path = Path(os.path.expanduser(path))

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        if not self._path.exists():
            return {}
        out: dict[str, date] = {}
        with open(self._path, newline="") as f:
            for row in csv.DictReader(f):
                sym, raw = row.get("symbol"), row.get("date")
                if not sym or not raw:
                    continue
                try:
                    d = date.fromisoformat(raw[:10])
                except ValueError:
                    continue
                if start <= d <= end and (sym not in out or d < out[sym]):
                    out[sym] = d
        return out
