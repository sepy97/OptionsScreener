"""Sessions and OAuth `state`, both as server-side records.

A session cookie carries nothing but a random identifier: the store is authoritative, so there is
no signing secret to configure or rotate. A 256-bit random id is unguessable, and because every
request looks the record up anyway, an HMAC would verify something the lookup already proves.
Server-side records also make revocation real — Disconnect deletes a row, and the session is gone,
rather than remaining valid until some embedded expiry passes.

The same store issues the OAuth ``state``: single-use, short-lived, and consumed on callback so a
replayed redirect cannot mint a second session.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TOKEN_BYTES = 32  # 256 bits of entropy; the cookie value is only ever this


@dataclass(frozen=True)
class Session:
    token: str
    broker: str
    account_fingerprint: str
    expires_at: datetime


class SessionStore:
    """SQLite-backed sessions and OAuth state. Connection per operation, like the job store."""

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " token TEXT PRIMARY KEY, broker TEXT NOT NULL,"
                " account_fingerprint TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS oauth_state ("
                " state TEXT PRIMARY KEY, broker TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # --- sessions -------------------------------------------------------------------------

    def create(self, broker: str, account_fingerprint: str, expires_at: datetime) -> str:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as con:
            con.execute(
                "INSERT INTO sessions (token, broker, account_fingerprint, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token, broker, account_fingerprint, expires_at.isoformat()),
            )
        return token

    def get(self, token: str | None) -> Session | None:
        """The live session for this token, or None. Expired rows are deleted on sight."""
        if not token:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT token, broker, account_fingerprint, expires_at FROM sessions"
                " WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            expires = datetime.fromisoformat(row[3])
            if expires <= datetime.now(tz=UTC):
                con.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
        return Session(token=row[0], broker=row[1], account_fingerprint=row[2], expires_at=expires)

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as con:
            con.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def revoke_broker(self, broker: str) -> None:
        """Every session minted by one broker — used when its link is disconnected."""
        with self._connect() as con:
            con.execute("DELETE FROM sessions WHERE broker = ?", (broker,))

    # --- OAuth state ----------------------------------------------------------------------

    def issue_state(self, broker: str, ttl_seconds: int = 600) -> str:
        state = secrets.token_urlsafe(_TOKEN_BYTES)
        expires = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        with self._connect() as con:
            con.execute("DELETE FROM oauth_state WHERE expires_at <= ?",
                        (datetime.now(tz=UTC).isoformat(),))
            con.execute("INSERT INTO oauth_state (state, broker, expires_at) VALUES (?, ?, ?)",
                        (state, broker, expires.isoformat()))
        return state

    def consume_state(self, state: str | None) -> str | None:
        """The broker this state was issued for, or None. SINGLE USE: consumed even on success,
        so a replayed callback — the same redirect opened twice — cannot mint a second session."""
        if not state:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT broker, expires_at FROM oauth_state WHERE state = ?", (state,)
            ).fetchone()
            con.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
        if row is None or datetime.fromisoformat(row[1]) <= datetime.now(tz=UTC):
            return None
        return row[0]
