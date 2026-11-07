"""A holding's ladder, as its panel on the Portfolio tab sends it: the shares and the call open on
them from the broker (``core.ladder.for_holding``), and what the person adjusts — the cost, what
the call sold for, the premiums the cost doesn't count, the growth a month. Nothing is saved: the
broker's positions are the record, and each panel prices them against the chain when it opens.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date

from wheel_screener.core.ladder import SHARES_PER_CONTRACT, Plan

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class PlanError(ValueError):
    """A ladder that can't be priced; the message says why, in words for the panel."""


def _number(raw: str | None, what: str, *, required: bool = False) -> float | None:
    """A number as typed — "6,00" and "$340" too — or None when the field is empty."""
    text = (raw or "").strip().replace("$", "").replace(" ", "")
    if not text:
        if required:
            raise PlanError(f"Enter {what}.")
        return None
    text = text.replace(",", ".") if text.count(",") == 1 and "." not in text else \
        text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        raise PlanError(f"{what[0].upper()}{what[1:]} should be a number.") from None
    if not math.isfinite(value):
        raise PlanError(f"{what[0].upper()}{what[1:]} should be a number.")
    return value


def plan_from_form(form: Mapping[str, str]) -> Plan:
    """The ladder the panel's fields describe, or a PlanError saying what to fix. Its rungs are
    empty, for the chain to plan."""
    symbol = (form.get("symbol") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise PlanError("No ticker for the shares.")
    shares = _number(form.get("shares"), "how many shares you hold", required=True)
    if shares < SHARES_PER_CONTRACT or shares != int(shares):
        raise PlanError(f"A covered call needs {SHARES_PER_CONTRACT} shares or more, a whole"
                        " number of them.")
    cost = _number(form.get("cost"), "what the shares cost", required=True)
    if cost <= 0:
        raise PlanError("What the shares cost should be above 0.")
    premiums = _number(form.get("premiums"), "the other premiums") or 0.0
    if premiums < 0:
        raise PlanError("The other premiums can't be below 0.")

    strike = _number(form.get("call_strike"), "the open call's strike")
    raw_expiry = (form.get("call_expiry") or "").strip()
    expiry = None
    if raw_expiry:
        try:
            expiry = date.fromisoformat(raw_expiry)
        except ValueError:
            raise PlanError("The open call's expiry should be a date.") from None
    if (strike is None) != (expiry is None):
        raise PlanError("For the open call, give both its strike and its expiry, or neither.")
    if strike is not None and strike <= 0:
        raise PlanError("The open call's strike should be above 0.")
    sold_for = _number(form.get("sold_for"), "what the call sold for")
    if sold_for is not None and sold_for < 0:
        raise PlanError("What the call sold for can't be below 0.")
    if strike is None:
        sold_for = None
    growth = _number((form.get("growth") or "").replace("%", ""), "the growth a month")
    if growth is not None and not -10 <= growth <= 10:
        raise PlanError("Keep the growth between -10% and 10% a month.")
    return Plan(symbol, int(shares), cost, (), premiums, sold_for or 0.0, strike, expiry,
                sold_for, None if growth is None else growth / 100)


def form_values(plan: Plan) -> dict[str, str]:
    """A ladder as its panel's fields show it, and as the row that opens the panel sends it."""
    def num(v: float | None) -> str:
        return "" if v is None else f"{v:g}"
    return {
        "symbol": plan.symbol, "shares": str(plan.shares), "cost": num(plan.cost),
        "premiums": num(plan.premiums) if plan.premiums else "",
        "call_strike": num(plan.call_strike),
        "call_expiry": plan.call_expiry.isoformat() if plan.call_expiry else "",
        "sold_for": num(plan.call_sold_for),
        "growth": "" if plan.growth is None else f"{plan.growth * 100:g}",
    }
