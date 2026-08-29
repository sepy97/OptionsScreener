"""The option-expiry calendar, for drawing the DTE window against reality.

A DTE range is only meaningful relative to the days options actually expire on, and those are
not evenly spread: every optionable name lists the MONTHLY (the third Friday), while weeklies
are listed by a liquid subset. A window can therefore look reasonable and still contain no
monthly at all — which is not hypothetical. On 2026-08-29 the default 21-35 window sat exactly
between the September monthly (20 days) and the October one (48), so 54% of the qualifying
contracts in a live screen were excluded by a single day, and every name without weeklys was
unscreenable. Drawing the ladder under the slider makes that visible before the run, not
inferrable afterwards from a thin table.
"""

from __future__ import annotations

from datetime import date, timedelta

_FRIDAY = 4

# How far the DTE slider reaches. A cash-secured put sold much beyond three months collects
# premium too slowly to be the trade this screener is for, and a slider stretched to a year
# would spend most of its width on expiries nobody sells.
DTE_HORIZON_DAYS = 90


def is_monthly(day: date) -> bool:
    """True for the third Friday of the month — the expiry every optionable name lists."""
    return day.weekday() == _FRIDAY and 15 <= day.day <= 21


def expiry_ladder(today: date, horizon_days: int) -> list[dict]:
    """Each Friday in ``(today, today + horizon]``, nearest first.

    ``weight`` is how broadly the expiry is listed rather than a measured contract count:
    monthlies exist on every optionable name, weeklies only on the liquid subset. Bar height
    encodes that difference and nothing more — it is a calendar, not a histogram of quotes.
    """
    first = today + timedelta(days=1)
    first += timedelta(days=(_FRIDAY - first.weekday()) % 7)
    out = []
    day = first
    while (day - today).days <= horizon_days:
        monthly = is_monthly(day)
        out.append({
            "dte": (day - today).days,
            "date": day,
            "monthly": monthly,
            "weight": 1.0 if monthly else 0.45,
        })
        day += timedelta(days=7)
    return out


def next_monthly(today: date, horizon_days: int) -> dict | None:
    """The nearest monthly expiry, for the one-line note above the slider."""
    return next((e for e in expiry_ladder(today, horizon_days) if e["monthly"]), None)
