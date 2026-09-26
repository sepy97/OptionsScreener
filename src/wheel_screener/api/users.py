"""Who may use the Portfolio: users, their passkeys, invites, sessions, and who owns a broker link.

Everything here is a server-side record, for the same reason the original session store was: a
cookie carries nothing but a random identifier, so revoking something deletes a row and it is gone.

What is stored, and why it is safe to store it:

* **users** — an internal id, a display name, whether they may administer, and the random *handle*
  a passkey is registered against. The handle is not the id: the browser and the authenticator
  see it, and it should say nothing about the account.
* **credentials** — each passkey's id and **public** key, and its signature counter. A stolen copy
  of this table cannot sign anybody in: signing needs the private key, which never leaves the
  person's device.
* **invites** — single-use links that create an account, or add a passkey to an existing one
  (recovery, or a second device). A bearer credential, so short-lived and consumed on use.
* **challenges** — the random value a passkey ceremony signs. Single-use and short-lived, like the
  OAuth ``state`` below; replaying a captured response fails because its challenge is gone.
* **user_sessions** — random token → user.
* **oauth_state** — the broker OAuth ``state``, carried over from the previous store unchanged.
* **broker_links** — which user a broker link belongs to. While there is one Schwab token per
  deployment this is at most one row per broker, and it is what stops a signed-in user from being
  shown somebody else's account.

Tables are only ever ADDED here, never altered. The previous release's ``sessions`` table is left
where it is rather than dropped, so rolling back finds the shape it expects.
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TOKEN_BYTES = 32  # 256 bits: session cookies, invite links, OAuth state
_HANDLE_BYTES = 32  # the WebAuthn user handle (the spec allows up to 64)
_CHALLENGE_BYTES = 32

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS users ("
    " id TEXT PRIMARY KEY, handle BLOB NOT NULL UNIQUE, name TEXT NOT NULL,"
    " is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS credentials ("
    " id BLOB PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),"
    " public_key BLOB NOT NULL, sign_count INTEGER NOT NULL,"
    " transports TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, last_used_at TEXT)",
    "CREATE INDEX IF NOT EXISTS credentials_by_user ON credentials(user_id)",
    "CREATE TABLE IF NOT EXISTS invites ("
    " token TEXT PRIMARY KEY, name TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,"
    " for_user TEXT REFERENCES users(id), expires_at TEXT NOT NULL, used_at TEXT)",
    "CREATE TABLE IF NOT EXISTS challenges ("
    " challenge TEXT PRIMARY KEY, purpose TEXT NOT NULL, data TEXT NOT NULL,"
    " expires_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS user_sessions ("
    " token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),"
    " expires_at TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS oauth_state ("
    " state TEXT PRIMARY KEY, broker TEXT NOT NULL, expires_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_links ("
    " broker TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),"
    " created_at TEXT NOT NULL)",
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def new_handle() -> bytes:
    """A fresh WebAuthn user handle: random, and unrelated to the account id."""
    return secrets.token_bytes(_HANDLE_BYTES)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class User:
    id: str
    handle: bytes
    name: str
    is_admin: bool


@dataclass(frozen=True)
class Credential:
    id: bytes
    user_id: str
    public_key: bytes
    sign_count: int
    transports: list[str]


@dataclass(frozen=True)
class Invite:
    token: str
    name: str
    is_admin: bool
    for_user: str | None  # set: adds a passkey to this existing account instead of creating one
    expires_at: datetime


@dataclass(frozen=True)
class Session:
    token: str
    user: User
    expires_at: datetime


class UserStore:
    """SQLite-backed, connection per operation like the job store, so request threads and the
    CLI can share the file safely."""

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            for statement in _SCHEMA:
                con.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # --- users ----------------------------------------------------------------------------

    @staticmethod
    def _user(row) -> User:
        return User(id=row[0], handle=bytes(row[1]), name=row[2], is_admin=bool(row[3]))

    def create_user(
        self, name: str, *, is_admin: bool = False, handle: bytes | None = None
    ) -> User:
        """``handle`` is passed when a passkey was already registered against one — the
        registration ceremony mints it before the account exists."""
        user = User(
            id=secrets.token_hex(16), handle=handle or new_handle(),
            name=name, is_admin=is_admin,
        )
        with self._connect() as con:
            con.execute(
                "INSERT INTO users (id, handle, name, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (user.id, user.handle, user.name, int(user.is_admin), _now().isoformat()),
            )
        return user

    def user(self, user_id: str | None) -> User | None:
        if not user_id:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT id, handle, name, is_admin FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._user(row) if row else None

    def users(self) -> list[User]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, handle, name, is_admin FROM users ORDER BY created_at"
            ).fetchall()
        return [self._user(r) for r in rows]

    # --- passkeys -------------------------------------------------------------------------

    def add_credential(
        self, user_id: str, credential_id: bytes, public_key: bytes, sign_count: int,
        transports: list[str] | None = None,
    ) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO credentials (id, user_id, public_key, sign_count, transports,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (credential_id, user_id, public_key, sign_count,
                 json.dumps(transports or []), _now().isoformat()),
            )

    @staticmethod
    def _credential(row) -> Credential:
        return Credential(id=bytes(row[0]), user_id=row[1], public_key=bytes(row[2]),
                          sign_count=row[3], transports=json.loads(row[4]))

    def credential(self, credential_id: bytes) -> Credential | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT id, user_id, public_key, sign_count, transports FROM credentials"
                " WHERE id = ?", (credential_id,),
            ).fetchone()
        return self._credential(row) if row else None

    def credentials_for(self, user_id: str) -> list[Credential]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, user_id, public_key, sign_count, transports FROM credentials"
                " WHERE user_id = ? ORDER BY created_at", (user_id,),
            ).fetchall()
        return [self._credential(r) for r in rows]

    def credential_used(self, credential_id: bytes, sign_count: int) -> None:
        """Record a successful sign-in: the new counter, and when."""
        with self._connect() as con:
            con.execute(
                "UPDATE credentials SET sign_count = ?, last_used_at = ? WHERE id = ?",
                (sign_count, _now().isoformat(), credential_id),
            )

    # --- invites --------------------------------------------------------------------------

    def create_invite(
        self, name: str, *, is_admin: bool = False, for_user: str | None = None,
        ttl: timedelta = timedelta(hours=72),
    ) -> str:
        if for_user is not None and self.user(for_user) is None:
            raise ValueError(f"no user {for_user!r}")
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as con:
            con.execute(
                "INSERT INTO invites (token, name, is_admin, for_user, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (token, name, int(is_admin), for_user, (_now() + ttl).isoformat()),
            )
        return token

    def invite(self, token: str | None) -> Invite | None:
        """The invite, if it is still usable — unused and unexpired. Does not consume it: the
        invite page is shown first, and only a successful passkey registration uses it up."""
        if not token:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT token, name, is_admin, for_user, expires_at FROM invites"
                " WHERE token = ? AND used_at IS NULL", (token,),
            ).fetchone()
        if row is None:
            return None
        expires = datetime.fromisoformat(row[4])
        if expires <= _now():
            return None
        return Invite(token=row[0], name=row[1], is_admin=bool(row[2]), for_user=row[3],
                      expires_at=expires)

    def use_invite(self, token: str) -> bool:
        """Mark it used. True only for the ONE caller that got there first — the guard against the
        same link being completed twice in parallel, which would otherwise make two accounts."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE invites SET used_at = ? WHERE token = ? AND used_at IS NULL"
                " AND expires_at > ?",
                (_now().isoformat(), token, _now().isoformat()),
            )
            return cur.rowcount == 1

    # --- challenges -----------------------------------------------------------------------

    def issue_challenge(self, purpose: str, data: dict | None = None,
                        ttl: timedelta = timedelta(minutes=5)) -> bytes:
        challenge = secrets.token_bytes(_CHALLENGE_BYTES)
        with self._connect() as con:
            con.execute("DELETE FROM challenges WHERE expires_at <= ?", (_now().isoformat(),))
            con.execute(
                "INSERT INTO challenges (challenge, purpose, data, expires_at) VALUES (?, ?, ?, ?)",
                (_b64(challenge), purpose, json.dumps(data or {}), (_now() + ttl).isoformat()),
            )
        return challenge

    def use_challenge(self, challenge_b64: str | None, purpose: str) -> tuple[bytes, dict] | None:
        """``(challenge, data)`` if this challenge was issued for ``purpose`` and is unexpired.
        SINGLE USE: deleted whatever the outcome, so a captured response cannot be replayed."""
        if not challenge_b64:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT purpose, data, expires_at FROM challenges WHERE challenge = ?",
                (challenge_b64,),
            ).fetchone()
            con.execute("DELETE FROM challenges WHERE challenge = ?", (challenge_b64,))
        if row is None or row[0] != purpose or datetime.fromisoformat(row[2]) <= _now():
            return None
        padded = challenge_b64 + "=" * (-len(challenge_b64) % 4)
        return base64.urlsafe_b64decode(padded), json.loads(row[1])

    # --- sessions -------------------------------------------------------------------------

    def create_session(self, user_id: str, ttl: timedelta) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        expires = _now() + ttl
        with self._connect() as con:
            con.execute(
                "INSERT INTO user_sessions (token, user_id, expires_at, created_at)"
                " VALUES (?, ?, ?, ?)",
                (token, user_id, expires.isoformat(), _now().isoformat()),
            )
        return token, expires

    def session(self, token: str | None) -> Session | None:
        """The live session for this token, or None. Expired rows are deleted on sight."""
        if not token:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT s.token, s.expires_at, u.id, u.handle, u.name, u.is_admin"
                " FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            expires = datetime.fromisoformat(row[1])
            if expires <= _now():
                con.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
                return None
        return Session(token=row[0], user=self._user(row[2:]), expires_at=expires)

    def end_session(self, token: str | None) -> None:
        if token:
            with self._connect() as con:
                con.execute("DELETE FROM user_sessions WHERE token = ?", (token,))

    def end_sessions_for(self, user_id: str) -> None:
        """Every session one person holds — for when an account is compromised. Nobody else's."""
        with self._connect() as con:
            con.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))

    # --- broker OAuth state ---------------------------------------------------------------

    def issue_state(self, broker: str, ttl_seconds: int = 600) -> str:
        state = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as con:
            con.execute("DELETE FROM oauth_state WHERE expires_at <= ?", (_now().isoformat(),))
            con.execute(
                "INSERT INTO oauth_state (state, broker, expires_at) VALUES (?, ?, ?)",
                (state, broker, (_now() + timedelta(seconds=ttl_seconds)).isoformat()),
            )
        return state

    def consume_state(self, state: str | None) -> str | None:
        """The broker this state was issued for, or None. SINGLE USE: consumed even on success,
        so a replayed callback — the same redirect opened twice — cannot complete a second link."""
        if not state:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT broker, expires_at FROM oauth_state WHERE state = ?", (state,)
            ).fetchone()
            con.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
        if row is None or datetime.fromisoformat(row[1]) <= _now():
            return None
        return row[0]

    # --- who owns a broker link -----------------------------------------------------------

    def link_owner(self, broker: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT user_id FROM broker_links WHERE broker = ?", (broker,)
            ).fetchone()
        return row[0] if row else None

    def set_link_owner(self, broker: str, user_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO broker_links (broker, user_id, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT(broker) DO UPDATE SET user_id = excluded.user_id,"
                " created_at = excluded.created_at",
                (broker, user_id, _now().isoformat()),
            )

    def clear_link_owner(self, broker: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM broker_links WHERE broker = ?", (broker,))
