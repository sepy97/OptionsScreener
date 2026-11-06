"""Call ladders: each signed-in person's plans, one per holding — what the Call ladder tab prices.

Kept in the jobs database, like bond portfolios, so the nightly backup carries them. Every method
takes the person's user id and touches only their rows: a plan id from a request is never enough
on its own to reach a plan.

A plan is what the person types (``core.ladder.Plan``): the holding, what it cost, the premiums
collected, the rungs and the call open on it. Nothing here is fetched from a broker; the page
prices it against the chain each time it is opened.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from wheel_screener.core.ladder import MAX_RUNGS, SHARES_PER_CONTRACT, Plan

MAX_PLANS = 20
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ladder_plans (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL,
    cost REAL NOT NULL,
    rungs TEXT NOT NULL,
    put_premium REAL NOT NULL DEFAULT 0,
    call_premiums REAL NOT NULL DEFAULT 0,
    call_strike REAL,
    call_expiry TEXT,
    call_sold_for REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ladder_plans_user ON ladder_plans (user_id);
"""


class PlanError(ValueError):
    """A plan that can't be saved; the message says why, in words for the page."""


@dataclass(frozen=True)
class SavedPlan:
    id: str
    plan: Plan
    updated_at: str = ""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


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


def rungs_text(rungs: tuple[float, ...]) -> str:
    return ", ".join(f"{r:g}" for r in rungs)


def plan_from_form(form: Mapping[str, str]) -> Plan:
    """The plan a form describes, or a PlanError saying what to fix."""
    symbol = (form.get("symbol") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise PlanError("Enter the ticker of the shares, e.g. TER.")
    shares = _number(form.get("shares"), "how many shares you hold", required=True)
    if shares < SHARES_PER_CONTRACT or shares != int(shares):
        raise PlanError(f"A covered call needs {SHARES_PER_CONTRACT} shares or more, a whole"
                        " number of them.")
    cost = _number(form.get("cost"), "what the shares cost", required=True)
    if cost <= 0:
        raise PlanError("What the shares cost should be above 0.")
    put_premium = _number(form.get("put_premium"), "the put's premium") or 0.0
    if put_premium < 0:
        raise PlanError("The put's premium can't be below 0.")
    call_premiums = _number(form.get("call_premiums"), "the call premiums") or 0.0

    parts = [p for p in re.split(r"[,;\s]+", form.get("rungs") or "") if p]
    rungs = sorted({_number(p, "each rung") for p in parts})
    if not rungs:
        raise PlanError("Enter the rungs, the strikes the call steps up, e.g. 340, 355, 370, 390.")
    if len(rungs) > MAX_RUNGS:
        raise PlanError(f"Keep it to {MAX_RUNGS} rungs or fewer.")
    if rungs[0] <= 0:
        raise PlanError("Every rung should be above 0.")

    strike = _number(form.get("call_strike"), "the open call's strike")
    raw_expiry = (form.get("call_expiry") or "").strip()
    expiry = None
    if raw_expiry:
        try:
            expiry = date.fromisoformat(raw_expiry)
        except ValueError:
            raise PlanError("The open call's expiry should be a date.") from None
    if (strike is None) != (expiry is None):
        raise PlanError("For the open call, enter both its strike and its expiry, or neither.")
    if strike is not None and strike <= 0:
        raise PlanError("The open call's strike should be above 0.")
    sold_for = _number(form.get("call_sold_for"), "what the call was sold for")
    if sold_for is not None and sold_for < 0:
        raise PlanError("What the call was sold for can't be below 0.")
    if strike is None:
        sold_for = None
    return Plan(symbol, int(shares), cost, tuple(rungs), put_premium, call_premiums, strike,
                expiry, sold_for)


def form_values(plan: Plan) -> dict[str, str]:
    """A plan as its form shows it, to be edited."""
    def num(v: float | None) -> str:
        return "" if v is None else f"{v:g}"
    return {
        "symbol": plan.symbol, "shares": str(plan.shares), "cost": num(plan.cost),
        "put_premium": num(plan.put_premium), "call_premiums": num(plan.call_premiums),
        "rungs": rungs_text(plan.rungs), "call_strike": num(plan.call_strike),
        "call_expiry": plan.call_expiry.isoformat() if plan.call_expiry else "",
        "call_sold_for": num(plan.call_sold_for),
    }


def _plan(row: tuple) -> SavedPlan:
    (pid, symbol, shares, cost, rungs, put_premium, call_premiums, strike, expiry, sold_for,
     updated_at) = row
    return SavedPlan(pid, Plan(
        symbol, int(shares), float(cost), tuple(float(r) for r in rungs.split(",") if r),
        float(put_premium), float(call_premiums), strike,
        date.fromisoformat(expiry) if expiry else None, sold_for,
    ), updated_at)


_COLUMNS = ("id, symbol, shares, cost, rungs, put_premium, call_premiums, call_strike,"
            " call_expiry, call_sold_for, updated_at")


class LadderPlanStore:
    """One connection per call, so request threads can all use it at once."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, taken up front, so a check and the change it guards (a ticker
        without a plan yet) see the same data."""
        conn = self._connect()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def plans(self, user_id: str) -> list[SavedPlan]:
        """This person's plans, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM ladder_plans WHERE user_id = ?"
                                " ORDER BY created_at, rowid", (user_id,)).fetchall()
        finally:
            conn.close()
        return [_plan(r) for r in rows]

    def plan(self, user_id: str, plan_id: str) -> SavedPlan | None:
        return next((p for p in self.plans(user_id) if p.id == plan_id), None)

    @staticmethod
    def _symbol_taken(conn: sqlite3.Connection, user_id: str, symbol: str,
                      other_than: str = "") -> bool:
        return conn.execute("SELECT 1 FROM ladder_plans WHERE user_id = ? AND symbol = ?"
                            " AND id != ?", (user_id, symbol, other_than)).fetchone() is not None

    @staticmethod
    def _values(plan: Plan) -> tuple:
        return (plan.symbol, plan.shares, plan.cost, rungs_text(plan.rungs).replace(" ", ""),
                plan.put_premium, plan.call_premiums, plan.call_strike,
                plan.call_expiry.isoformat() if plan.call_expiry else None, plan.call_sold_for)

    def create(self, user_id: str, plan: Plan) -> SavedPlan:
        with self._tx() as conn:
            count = conn.execute("SELECT COUNT(*) FROM ladder_plans WHERE user_id = ?",
                                 (user_id,)).fetchone()[0]
            if count >= MAX_PLANS:
                raise PlanError(f"You can keep at most {MAX_PLANS} ladders.")
            if self._symbol_taken(conn, user_id, plan.symbol):
                raise PlanError(f"You already have a ladder for {plan.symbol}.")
            pid, now = uuid.uuid4().hex, _now()
            conn.execute(
                "INSERT INTO ladder_plans (id, user_id, symbol, shares, cost, rungs, put_premium,"
                " call_premiums, call_strike, call_expiry, call_sold_for, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, user_id, *self._values(plan), now, now))
        return SavedPlan(pid, plan, now)

    def update(self, user_id: str, plan_id: str, plan: Plan) -> None:
        with self._tx() as conn:
            if self._symbol_taken(conn, user_id, plan.symbol, other_than=plan_id):
                raise PlanError(f"You already have a ladder for {plan.symbol}.")
            conn.execute(
                "UPDATE ladder_plans SET symbol = ?, shares = ?, cost = ?, rungs = ?,"
                " put_premium = ?, call_premiums = ?, call_strike = ?, call_expiry = ?,"
                " call_sold_for = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (*self._values(plan), _now(), plan_id, user_id))

    def delete(self, user_id: str, plan_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM ladder_plans WHERE id = ? AND user_id = ?",
                         (plan_id, user_id))
