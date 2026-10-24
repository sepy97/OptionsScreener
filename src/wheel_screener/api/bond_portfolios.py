"""Bond portfolios: each signed-in person's named portfolios and the bonds held in them.

Kept in the jobs database, like the blocklist, so the nightly backup carries them. Every method
takes the person's user id and touches only their rows: a portfolio id from a request is never
enough on its own to reach a portfolio.

Also the person's calendar-feed link. The feed is how a calendar app, which cannot sign in, reads
someone's calendar; the token is a bearer credential to it, so like an invite link it is shown
once, when it is made, and only its hash is stored: neither this file nor a backup of it is a way
to read anyone's calendar. Making a new link replaces the hash, which turns the old link off.

Before portfolios there was one watchlist per person (the ``bond_watchlists`` table). On start,
each watchlist not moved yet becomes a portfolio named "Main", and its feed link keeps working;
the old rows are left as they were — marked moved — so going back a version finds them intact.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wheel_screener.bonds import bond_calendar as bc
from wheel_screener.bonds.holdings import Holding, clean_name

_FEED_TOKEN_BYTES = 32
MAX_BONDS = 500  # per portfolio: a person's bonds, not a market dump — and each costs a request
MAX_PORTFOLIOS = 20
NAME_CHARS = 60
LEGACY_NAME = "Main"  # what a watchlist from before portfolios is called once moved

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bond_portfolios (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bond_portfolios_user ON bond_portfolios (user_id);
CREATE TABLE IF NOT EXISTS bond_holdings (
    portfolio_id TEXT NOT NULL REFERENCES bond_portfolios (id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    matdate TEXT,
    qty REAL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, key)
);
CREATE TABLE IF NOT EXISTS bond_feeds (
    user_id TEXT PRIMARY KEY,
    feed_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""


class PortfolioError(ValueError):
    """A change that can't be made; the message says why, in words for the page."""


@dataclass(frozen=True)
class Portfolio:
    id: str
    name: str
    count: int  # bonds held in it


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _holding(row: tuple) -> Holding:
    pid, portfolio, code, name, matdate, qty = row
    return Holding(pid, portfolio, code, name, bc.parse_date(matdate), qty)


def valid_qty(qty: float | None) -> bool:
    return qty is not None and math.isfinite(qty) and qty > 0


class BondPortfolioStore:
    """One connection per call, so request threads can all use it at once."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)  # its own transaction (it commits what is pending)
        finally:
            conn.close()
        self._move_watchlists()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, taken up front: a check and the change it guards (a name not
        taken yet, a bond not held yet) see the same data, whoever else is writing."""
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

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ── moving the watchlists from before portfolios ──

    def _move_watchlists(self) -> None:
        with self._tx() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                                " AND name = 'bond_watchlists'").fetchone():
                return
            columns = {r[1] for r in conn.execute("PRAGMA table_info(bond_watchlists)")}
            if "moved_at" not in columns:
                conn.execute("ALTER TABLE bond_watchlists ADD COLUMN moved_at TEXT")
            rows = conn.execute("SELECT user_id, text, feed_hash FROM bond_watchlists"
                                " WHERE moved_at IS NULL").fetchall()
            for user_id, text, feed_hash in rows:
                items = bc.parse_watchlist(text or "")
                if items:
                    pid = self._insert_portfolio(conn, user_id, LEGACY_NAME)
                    self._insert_items(conn, pid, items)
                if feed_hash:
                    conn.execute("INSERT OR IGNORE INTO bond_feeds (user_id, feed_hash,"
                                 " created_at) VALUES (?, ?, ?)", (user_id, feed_hash, _now()))
                conn.execute("UPDATE bond_watchlists SET moved_at = ? WHERE user_id = ?",
                             (_now(), user_id))

    # ── portfolios ──

    def portfolios(self, user_id: str) -> list[Portfolio]:
        """This person's portfolios, oldest first."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT p.id, p.name, COUNT(h.key) FROM bond_portfolios p"
                " LEFT JOIN bond_holdings h ON h.portfolio_id = p.id"
                " WHERE p.user_id = ? GROUP BY p.id ORDER BY p.created_at, p.rowid",
                (user_id,)).fetchall()
        return [Portfolio(*r) for r in rows]

    def portfolio(self, user_id: str, portfolio_id: str) -> Portfolio | None:
        return next((p for p in self.portfolios(user_id) if p.id == portfolio_id), None)

    @staticmethod
    def _clean_portfolio_name(name: str) -> str:
        name = " ".join((name or "").split())[:NAME_CHARS]
        if not name:
            raise PortfolioError("Give the portfolio a name.")
        return name

    @staticmethod
    def _name_taken(conn: sqlite3.Connection, user_id: str, name: str,
                    other_than: str = "") -> bool:
        names = conn.execute("SELECT name FROM bond_portfolios WHERE user_id = ? AND id != ?",
                             (user_id, other_than)).fetchall()
        return any(n.casefold() == name.casefold() for (n,) in names)

    @staticmethod
    def _insert_portfolio(conn: sqlite3.Connection, user_id: str, name: str) -> str:
        pid = uuid.uuid4().hex
        conn.execute("INSERT INTO bond_portfolios (id, user_id, name, created_at)"
                     " VALUES (?, ?, ?, ?)", (pid, user_id, name, _now()))
        return pid

    def create(self, user_id: str, name: str) -> Portfolio:
        name = self._clean_portfolio_name(name)
        with self._tx() as conn:
            count = conn.execute("SELECT COUNT(*) FROM bond_portfolios WHERE user_id = ?",
                                 (user_id,)).fetchone()[0]
            if count >= MAX_PORTFOLIOS:
                raise PortfolioError(f"You can keep at most {MAX_PORTFOLIOS} portfolios.")
            if self._name_taken(conn, user_id, name):
                raise PortfolioError(f"You already have a portfolio called {name}.")
            pid = self._insert_portfolio(conn, user_id, name)
        return Portfolio(pid, name, 0)

    def rename(self, user_id: str, portfolio_id: str, name: str) -> None:
        name = self._clean_portfolio_name(name)
        with self._tx() as conn:
            if self._name_taken(conn, user_id, name, other_than=portfolio_id):
                raise PortfolioError(f"You already have a portfolio called {name}.")
            conn.execute("UPDATE bond_portfolios SET name = ? WHERE id = ? AND user_id = ?",
                         (name, portfolio_id, user_id))

    def delete(self, user_id: str, portfolio_id: str) -> None:
        """The portfolio and the bonds in it."""
        with self._tx() as conn:
            conn.execute("DELETE FROM bond_portfolios WHERE id = ? AND user_id = ?",
                         (portfolio_id, user_id))

    # ── holdings ──

    def holdings(self, user_id: str, portfolio_id: str | None = None) -> list[Holding]:
        """The bonds in one of this person's portfolios — or in all of them (None), portfolio by
        portfolio. Each portfolio's bonds are in the order they were added."""
        query = ("SELECT h.portfolio_id, p.name, h.code, h.name, h.matdate, h.qty"
                 " FROM bond_holdings h JOIN bond_portfolios p ON p.id = h.portfolio_id"
                 " WHERE p.user_id = ?")
        args: tuple = (user_id,)
        if portfolio_id is not None:
            query += " AND p.id = ?"
            args += (portfolio_id,)
        with self._read() as conn:
            rows = conn.execute(query + " ORDER BY p.created_at, p.rowid, h.rowid",
                                args).fetchall()
        return [_holding(r) for r in rows]

    @staticmethod
    def _owns(conn: sqlite3.Connection, user_id: str, portfolio_id: str) -> None:
        if not conn.execute("SELECT 1 FROM bond_portfolios WHERE id = ? AND user_id = ?",
                            (portfolio_id, user_id)).fetchone():
            raise PortfolioError("That portfolio is gone.")

    @staticmethod
    def _insert_items(conn: sqlite3.Connection, portfolio_id: str, items: list[dict]) -> None:
        now = _now()
        conn.executemany(
            "INSERT INTO bond_holdings (portfolio_id, key, code, name, matdate, qty, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (portfolio_id, key) DO UPDATE SET"
            " qty = CASE WHEN excluded.qty IS NULL THEN bond_holdings.qty"
            " ELSE COALESCE(bond_holdings.qty, 0) + excluded.qty END",
            [(portfolio_id, bc.item_key(it["code"], it["name"]), it["code"], it["name"],
              it["matdate"].isoformat() if it["matdate"] else None, it["qty"], now)
             for it in items])

    def add(self, user_id: str, portfolio_id: str, item: dict, *,
            aliases: tuple[str, ...] = ()) -> Holding:
        """Hold a bond (``item``: code, name, matdate, qty) in a portfolio. ``aliases`` are the
        bond's other codes, so a bond already held under either is not added twice."""
        item = {**item, "code": str(item["code"] or "").strip().upper(),
                "name": clean_name(item["name"])}
        key = bc.item_key(item["code"], item["name"])
        with self._tx() as conn:
            self._owns(conn, user_id, portfolio_id)
            keys = {k for (k,) in conn.execute(
                "SELECT key FROM bond_holdings WHERE portfolio_id = ?", (portfolio_id,))}
            if key in keys or any(a and a in keys for a in aliases):
                raise PortfolioError(
                    f"{item['name'] or item['code']} is already in this portfolio.")
            if len(keys) >= MAX_BONDS:
                raise PortfolioError(
                    f"A portfolio holds at most {MAX_BONDS} bonds — remove one first.")
            self._insert_items(conn, portfolio_id, [item])
            name = conn.execute("SELECT name FROM bond_portfolios WHERE id = ?",
                                (portfolio_id,)).fetchone()[0]
        return Holding(portfolio_id, name, item["code"], item["name"], item["matdate"],
                       item["qty"])

    def remove(self, user_id: str, portfolio_id: str, key: str) -> Holding | None:
        """Stop holding a bond; returns it (to put back if the removal is undone), or None if it
        was not there."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT h.portfolio_id, p.name, h.code, h.name, h.matdate, h.qty"
                " FROM bond_holdings h JOIN bond_portfolios p ON p.id = h.portfolio_id"
                " WHERE p.user_id = ? AND p.id = ? AND h.key = ?",
                (user_id, portfolio_id, key)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM bond_holdings WHERE portfolio_id = ? AND key = ?",
                         (portfolio_id, key))
        return _holding(row)

    def set_qty(self, user_id: str, portfolio_id: str, key: str, qty: float) -> bool:
        """How many of a bond this portfolio holds; False if the bond is not in it."""
        if not valid_qty(qty):
            raise PortfolioError("a quantity is a number above 0")
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE bond_holdings SET qty = ? WHERE key = ? AND portfolio_id = ("
                " SELECT id FROM bond_portfolios WHERE id = ? AND user_id = ?)",
                (qty, key, portfolio_id, user_id))
            return cur.rowcount > 0

    def replace(self, user_id: str, portfolio_id: str, items: list[dict]) -> None:
        """A portfolio's bonds, all at once — an imported list in place of what it held."""
        if len(items) > MAX_BONDS:
            raise PortfolioError(f"A portfolio holds at most {MAX_BONDS} bonds.")
        items = [{**it, "name": clean_name(it["name"])} for it in items]
        with self._tx() as conn:
            self._owns(conn, user_id, portfolio_id)
            conn.execute("DELETE FROM bond_holdings WHERE portfolio_id = ?", (portfolio_id,))
            self._insert_items(conn, portfolio_id, items)

    # ── the calendar feed ──

    def new_feed(self, user_id: str) -> str:
        """A fresh calendar-feed token for this person; the previous one stops working."""
        token = secrets.token_urlsafe(_FEED_TOKEN_BYTES)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO bond_feeds (user_id, feed_hash, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET feed_hash = excluded.feed_hash,"
                " created_at = excluded.created_at", (user_id, _hash(token), _now()))
        return token

    def has_feed(self, user_id: str) -> bool:
        with self._read() as conn:
            return conn.execute("SELECT 1 FROM bond_feeds WHERE user_id = ?",
                                (user_id,)).fetchone() is not None

    def user_for_feed(self, token: str) -> str | None:
        """Whose calendar a feed token reads, or None for a token that is not (or no longer)
        one."""
        if not token:
            return None
        with self._read() as conn:
            row = conn.execute("SELECT user_id FROM bond_feeds WHERE feed_hash = ?",
                               (_hash(token),)).fetchone()
        return row[0] if row else None

    def forget(self, user_id: str) -> None:
        """Remove this person's portfolios, bonds and feed — when their access is taken away."""
        with self._tx() as conn:
            conn.execute("DELETE FROM bond_portfolios WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM bond_feeds WHERE user_id = ?", (user_id,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                            " AND name = 'bond_watchlists'").fetchone():
                conn.execute("DELETE FROM bond_watchlists WHERE user_id = ?", (user_id,))
