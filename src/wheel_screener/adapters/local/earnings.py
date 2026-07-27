"""Local earnings-calendar store — reads the CSV written by the `refresh-earnings` job.

One row per symbol with its next earnings date. Satisfies the same duck-typed interface as the
live FMP earnings source (``earnings_calendar(start, end) -> {symbol: date}``), so it drops into
the screener's blackout with zero API calls.

The file carries its own provenance (`# covers=<from>..<to> fetched=<date>` header) because a
calendar that silently under-reports is worse than no calendar at all: every missing symbol reads
as "no earnings scheduled" and sails through the blackout (issue #113). A window this file cannot
vouch for raises rather than answering with a confident-looking subset.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from wheel_screener.core.errors import ProviderDataError

logger = logging.getLogger(__name__)

_META_PREFIX = "#"
_MAX_STALE_DAYS = 3  # dates get confirmed/moved constantly; an old file is a silent blackout hole


class LocalEarningsCalendar:
    def __init__(self, path: str, *, max_stale_days: int = _MAX_STALE_DAYS) -> None:
        self._path = Path(os.path.expanduser(path))
        self._max_stale_days = max_stale_days

    def _read(self) -> tuple[dict[str, date], dict[str, str]]:
        """Return (symbol -> date, metadata). Metadata comes from the leading `#` comment line."""
        out: dict[str, date] = {}
        meta: dict[str, str] = {}
        with open(self._path, newline="") as f:
            lines = f.read().splitlines()
        body: list[str] = []
        for line in lines:
            if line.startswith(_META_PREFIX):
                for part in line.lstrip(_META_PREFIX).strip().split():
                    key, _, value = part.partition("=")
                    if value:
                        meta[key] = value
                continue
            body.append(line)
        for row in csv.DictReader(body):
            sym, raw = row.get("symbol"), row.get("date")
            if not sym or not raw:
                continue
            try:
                d = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            if sym not in out or d < out[sym]:
                out[sym] = d
        return out, meta

    def _covers(self, meta: dict[str, str], start: date, end: date, today: date) -> None:
        """Raise unless the file vouches for [start, end] and was fetched recently enough."""
        fetched_raw, covers = meta.get("fetched"), meta.get("covers", "")
        frm, _, to = covers.partition("..")
        try:
            covers_from, covers_to = date.fromisoformat(frm), date.fromisoformat(to)
            fetched = date.fromisoformat(fetched_raw) if fetched_raw else None
        except ValueError:
            fetched, covers_from, covers_to = None, None, None
        if fetched is None or covers_from is None or covers_to is None:
            raise ProviderDataError(
                f"{self._path} has no coverage header — it predates the provenance format and "
                "cannot be trusted for the earnings blackout. Re-run `refresh-earnings`."
            )
        if (today - fetched).days > self._max_stale_days:
            raise ProviderDataError(
                f"{self._path} was fetched {fetched} ({(today - fetched).days}d ago) — too stale "
                "for the earnings blackout. Re-run `refresh-earnings`."
            )
        if covers_from > start or covers_to < end:
            raise ProviderDataError(
                f"{self._path} covers {covers_from}..{covers_to}, which does not span the "
                f"requested {start}..{end}. Re-run `refresh-earnings` with a wider --days."
            )

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        if not self._path.exists():
            raise ProviderDataError(
                f"no earnings calendar at {self._path} — the blackout cannot run blind. "
                "Run `refresh-earnings`, or set an FMP key to fetch it live."
            )
        found, meta = self._read()
        self._covers(meta, start, end, date.today())
        return {s: d for s, d in found.items() if start <= d <= end}

    def next_earnings(self, symbol: str, on_or_after: date) -> date | None:
        """Best effort for one symbol — the file holds only the next date per symbol, so this
        can answer 'when' but never proves absence (the live provider is authoritative)."""
        if not self._path.exists():
            return None
        found, _ = self._read()
        when = found.get(symbol)
        return when if when is not None and when >= on_or_after else None


def write_calendar(
    path: str, calendar: dict[str, date], *, covers_from: date, covers_to: date, fetched: date
) -> Path:
    """Write the CSV atomically with its provenance header (see the module docstring).

    Atomic because a half-written calendar is indistinguishable from a sparse one, and the
    consumer's whole job is to trust it.
    """
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        f.write(
            f"{_META_PREFIX} covers={covers_from.isoformat()}..{covers_to.isoformat()} "
            f"fetched={fetched.isoformat()} symbols={len(calendar)}\n"
        )
        writer = csv.writer(f)
        writer.writerow(["symbol", "date"])
        for symbol, when in sorted(calendar.items()):
            writer.writerow([symbol, when.isoformat()])
    os.replace(tmp, target)
    return target


def assert_near_term_coverage(calendar: dict[str, date], today: date, days: int = 21) -> None:
    """Sanity-check a freshly fetched calendar before it is written.

    A calendar with no reporters at all in the next few weeks is the exact shape of the #113
    truncation — during any normal period hundreds of companies report in that span.
    """
    horizon = today + timedelta(days=days)
    near = sum(1 for when in calendar.values() if today <= when <= horizon)
    if near == 0:
        raise ProviderDataError(
            f"refusing to write an earnings calendar with 0 reporters in the next {days} days "
            f"({len(calendar)} symbols total) — that is what a truncated upstream response looks "
            "like, not a real market."
        )
    logger.info("earnings calendar: %d symbols, %d reporting within %dd", len(calendar), near, days)
