"""Bond watchlists: each signed-in person's own list for the Bond calendar tab.

Kept in the jobs database, like the blocklist, so the nightly backup carries them. One row per
person: the watchlist text in the resolved form the script writes, and — if they made one — the
HASH of their calendar-feed token. The feed is how a calendar app, which cannot sign in, reads
someone's calendar; the token is a bearer credential to it, so like an invite link it is shown
once, when it is made, and only its hash is stored: neither this file nor a backup of it is a way
to read anyone's calendar. Making a new link replaces the hash, which turns the old link off.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_FEED_TOKEN_BYTES = 32
MAX_BONDS = 500  # a watchlist is a person's bonds, not a market dump — and each costs a request


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BondWatchlistStore:
    """One connection per call, so request threads can all use it at once."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bond_watchlists ("
                    " user_id TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '',"
                    " updated_at TEXT NOT NULL, feed_hash TEXT UNIQUE)"
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    def text(self, user_id: str) -> str:
        """This person's watchlist ('' when they have none yet)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT text FROM bond_watchlists WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else ""

    def save(self, user_id: str, text: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO bond_watchlists (user_id, text, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(user_id) DO UPDATE SET text = excluded.text,"
                    " updated_at = excluded.updated_at",
                    (user_id, text, now),
                )
        finally:
            conn.close()

    def new_feed(self, user_id: str) -> str:
        """A fresh calendar-feed token for this person; the previous one stops working."""
        token = secrets.token_urlsafe(_FEED_TOKEN_BYTES)
        now = datetime.now(tz=UTC).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO bond_watchlists (user_id, updated_at, feed_hash) VALUES (?, ?, ?)"
                    " ON CONFLICT(user_id) DO UPDATE SET feed_hash = excluded.feed_hash",
                    (user_id, now, _hash(token)),
                )
        finally:
            conn.close()
        return token

    def has_feed(self, user_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT feed_hash FROM bond_watchlists WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])

    def text_for_feed(self, token: str) -> str | None:
        """The watchlist a feed token reads, or None for a token that is not (or no longer) one."""
        if not token:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT text FROM bond_watchlists WHERE feed_hash = ?", (_hash(token),)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def forget(self, user_id: str) -> None:
        """Remove this person's watchlist and feed — when their access is taken away."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM bond_watchlists WHERE user_id = ?", (user_id,))
        finally:
            conn.close()
