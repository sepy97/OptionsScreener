"""Nightly backup of the state that cannot be re-derived: accounts, past screens, the overlay.

What each copy is, and what it deliberately is not:

* **accounts** — who can sign in (``users``), their passkeys' PUBLIC keys (``credentials``), and
  who owns a broker link (``broker_links``). Nothing in it can sign anybody in. Live sessions,
  invite links, challenges and OAuth state are left out: each is a bearer credential or worthless
  after a few minutes, and a backup file that leaves the box should not be a way into the site.
  Restored, it means everyone signs in again — one passkey tap — and nobody is re-invited.
  The rule is a list of tables to KEEP, not to drop, so a table added later is left out until
  someone decides it belongs here.
* **jobs** — past screens, including the precomputed ones the dashboard and Close? column read.
* **the fundamentals overlay** — per-symbol metrics refreshed after earnings, which the bulk store
  does not have.

Not the Schwab token (see docs/DEPLOY.md: it lapses in 7 days, it is trading-capable, and one
click replaces it), and not the multi-gigabyte fundamentals store, which is rebuilt from source.

Copies are taken with SQLite's online backup API from a READ-ONLY connection, so they are
consistent while the app is writing and cannot disturb it. Each is integrity-checked, and a
backup is assembled under a temporary name and renamed only once complete, so a crash never
leaves a half-written backup that looks finished.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# The account tables worth restoring. Everything else in that file is dropped from the copy.
ACCOUNT_TABLES = ("users", "credentials", "broker_links")

_PARTIAL = ".partial-"
# Only directories named like this are ever considered for pruning — never anything else that
# happens to be in the backup folder.
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


class BackupError(Exception):
    """A backup that could not be completed. Nothing half-written is left looking complete."""


@dataclass
class BackupReport:
    path: Path
    files: dict[str, str] = field(default_factory=dict)  # file -> one-line summary
    pruned: list[Path] = field(default_factory=list)


def _copy_sqlite(source: Path, target: Path, keep_tables: tuple[str, ...] | None = None) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        # One self-contained file: the live database is in WAL mode, and a copy that carried that
        # flag would sprout -wal/-shm companions the moment anything opened it.
        dst.execute("PRAGMA journal_mode=DELETE")
        if keep_tables is not None:
            dst.execute("PRAGMA secure_delete=ON")
            tables = [r[0] for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )]
            for table in tables:
                if table not in keep_tables:
                    dst.execute(f'DROP TABLE "{table}"')
            dst.commit()
        # Rebuild without free pages. A dropped table's rows otherwise linger in the file as
        # leftovers — which, for sessions, would be exactly the tokens this copy exists to omit.
        dst.execute("VACUUM")
        verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
    if verdict != "ok":
        raise BackupError(f"{target.name} failed its integrity check: {verdict}")


def _rows(path: Path, table: str) -> int:
    """Rows in ``table``, or 0 if the copy has no such table (a store that predates it)."""
    con = sqlite3.connect(path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] if exists else 0
    finally:
        con.close()


def run_backup(
    *,
    accounts_db: str | Path,
    jobs_db: str | Path,
    overlay: str | Path,
    dest_root: str | Path,
    keep: int,
    now: datetime,
) -> BackupReport:
    """Write one dated backup under ``dest_root`` and keep the newest ``keep``.

    A source that does not exist yet is skipped, not an error — a fresh deployment has no overlay
    until the first earnings refresh, and no accounts until the first invite.
    """
    if keep < 1:
        raise BackupError("keep must be at least 1, or every backup would delete itself")
    root = Path(dest_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    partial = root / f"{_PARTIAL}{stamp}"
    final = root / stamp
    if final.exists():
        raise BackupError(f"a backup named {stamp} already exists")
    partial.mkdir()
    report = BackupReport(path=final)
    try:
        accounts = Path(accounts_db).expanduser()
        if accounts.exists():
            target = partial / "accounts.sqlite"
            _copy_sqlite(accounts, target, keep_tables=ACCOUNT_TABLES)
            report.files["accounts.sqlite"] = (
                f"{_rows(target, 'users')} account(s), {_rows(target, 'credentials')} passkey(s)"
            )
        jobs = Path(jobs_db).expanduser()
        if jobs.exists():
            target = partial / "jobs.sqlite"
            _copy_sqlite(jobs, target)
            report.files["jobs.sqlite"] = f"{_rows(target, 'jobs')} screen(s)"
        over = Path(overlay).expanduser()
        if over.exists():
            shutil.copy2(over, partial / over.name)
            report.files[over.name] = f"{over.stat().st_size:,} bytes"
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise

    # Keep the newest `keep` finished backups, and clear any half-written ones a crash left.
    finished = sorted(
        (p for p in root.iterdir() if p.is_dir() and _STAMP.match(p.name)),
        key=lambda p: p.name,
    )
    for old in finished[:-keep]:
        shutil.rmtree(old)
        report.pruned.append(old)
    for stale in root.glob(f"{_PARTIAL}*"):
        shutil.rmtree(stale, ignore_errors=True)
        report.pruned.append(stale)
    return report
