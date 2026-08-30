"""Parsing the OSI option symbol brokers put in a position row.

A broker reports a held option as one 21-character string — ``"AAPL  260918P00190000"`` — and
everything a wheel view needs is inside it: underlying, expiry, side and strike. There is no
second call that returns those as fields, so this parser is the only way a position row becomes
"a 190 put on AAPL expiring 18 Sep".

Two details make hand-rolling it error-prone, and both are load-bearing here:

* the root is padded to **six** characters with spaces, so the date does not start at a fixed
  offset unless you count from the right — and an adjusted root (``AAPL1``) is a real, common
  thing that a left-anchored parse silently mangles;
* the strike is in **thousandths** of a dollar, so ``00190000`` is $190.00 and a missed divide
  is a thousand-fold error that still looks like a plausible number.

Parsing from the RIGHT sidesteps both: the last 15 characters are fixed-width, and whatever
precedes them is the root, whatever its length.
"""

from __future__ import annotations

import re
from datetime import date

from wheel_screener.core.models import OptionType

# 6 date digits, C/P, 8 strike digits — anchored to the END, so the root may be any length.
_TAIL = re.compile(r"^(?P<root>[A-Z0-9./]+?)\s*(?P<ymd>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")

STRIKE_SCALE = 1000.0  # the strike field is in thousandths of a dollar


class OsiSymbol:
    """An option symbol taken apart. Immutable and cheap; construct via :func:`parse_osi`."""

    __slots__ = ("expiration", "option_type", "raw", "strike", "underlying")

    def __init__(self, raw, underlying, expiration, option_type, strike) -> None:
        self.raw = raw
        self.underlying = underlying
        self.expiration = expiration
        self.option_type = option_type
        self.strike = strike

    def dte(self, today: date) -> int:
        return (self.expiration - today).days

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"OsiSymbol({self.underlying} {self.expiration} "
                f"{self.option_type.value} {self.strike})")


def parse_osi(symbol: str) -> OsiSymbol | None:
    """Take an OSI symbol apart, or return None if it is not one.

    None rather than an exception: a position list mixes options with equities, cash sweeps and
    fund shares, and "this row is not an option" is an ordinary outcome, not a failure.
    """
    m = _TAIL.match((symbol or "").strip().upper())
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        expiration = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None  # a symbol carrying an impossible date is not one we can act on
    return OsiSymbol(
        raw=symbol.strip(),
        underlying=m.group("root"),
        expiration=expiration,
        option_type=OptionType.PUT if m.group("side") == "P" else OptionType.CALL,
        strike=int(m.group("strike")) / STRIKE_SCALE,
    )
