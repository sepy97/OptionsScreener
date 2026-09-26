"""The nightly backup: what it keeps, what it must not, and whether a restore actually works."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _softkey import SoftKey

from wheel_screener.api.jobs import SOURCE_REFRESH, JobStore
from wheel_screener.api.passkeys import Passkeys
from wheel_screener.api.users import UserStore
from wheel_screener.jobs.backup import ACCOUNT_TABLES, BackupError, run_backup

ORIGIN = "https://steadybull.example"
NOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)


def _live(tmp_path: Path):
    """A deployment's state as it really is: an account with a passkey and a live session, an
    unused invite, a pending challenge, OAuth state, the old release's session table, a screen."""
    users = UserStore(str(tmp_path / "live" / "sessions.sqlite"))
    pk = Passkeys(users, "steadybull.example", "Steady Bull", ORIGIN)
    key = SoftKey(ORIGIN)
    options = json.loads(pk.registration_options(users.create_invite("Sam", is_admin=True)))
    sam = pk.register(key.create(options))
    session, _ = users.create_session(sam.id, timedelta(days=90))
    invite = users.create_invite("Alex")
    pk.login_options()  # leaves a challenge behind
    state = users.issue_state("schwab")
    users.set_link_owner("schwab", sam.id)
    con = sqlite3.connect(tmp_path / "live" / "sessions.sqlite")
    con.execute("CREATE TABLE sessions (token TEXT PRIMARY KEY, broker TEXT, "
                "account_fingerprint TEXT, expires_at TEXT)")  # v3.2.0's, left in place
    con.execute("INSERT INTO sessions VALUES ('LEGACY-TOKEN', 'schwab', 'fp', '2099-01-01')")
    con.commit()
    con.close()
    jobs = JobStore(str(tmp_path / "live" / "jobs.sqlite"))
    jobs.create("screen1", NOW.isoformat(), SOURCE_REFRESH)
    jobs.finish("screen1", "done", result=[])
    overlay = tmp_path / "live" / "overlay_metrics.csv"
    overlay.write_text("symbol,pe\nAAPL,30\n")
    secrets = [session, invite, state, "LEGACY-TOKEN"]
    return pk, key, sam, secrets


def _backup(tmp_path: Path, now=NOW, keep=14):
    live = tmp_path / "live"
    return run_backup(
        accounts_db=live / "sessions.sqlite", jobs_db=live / "jobs.sqlite",
        overlay=live / "overlay_metrics.csv", dest_root=tmp_path / "backups",
        keep=keep, now=now,
    )


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()


# --- what it keeps --------------------------------------------------------------------------

def test_a_restored_backup_lets_you_sign_in_with_the_same_passkey(tmp_path) -> None:
    """The only test that says a backup is worth anything: restore it, and the passkey on the
    person's phone still opens their account, still an admin, still owning the broker link."""
    _, key, sam, _ = _live(tmp_path)
    report = _backup(tmp_path)
    restored = UserStore(str(report.path / "accounts.sqlite"))  # what a restore opens
    pk = Passkeys(restored, "steadybull.example", "Steady Bull", ORIGIN)
    back = pk.login(key.get(json.loads(pk.login_options())))
    assert back.id == sam.id and back.is_admin
    assert restored.link_owner("schwab") == sam.id


def test_it_keeps_the_account_tables_and_nothing_else(tmp_path) -> None:
    _live(tmp_path)
    accounts = _backup(tmp_path).path / "accounts.sqlite"
    assert _tables(accounts) == set(ACCOUNT_TABLES)


def test_no_way_in_survives_into_the_backup_not_even_as_leftover_bytes(tmp_path) -> None:
    """Sessions, invites and OAuth state are bearer credentials. Dropping their tables is not
    enough: SQLite leaves deleted rows in free pages, so the file itself is searched for them."""
    _, _, _, secrets = _live(tmp_path)
    raw = (_backup(tmp_path).path / "accounts.sqlite").read_bytes()
    for secret in secrets:
        assert secret.encode() not in raw, "a credential survived in the backup file"


def test_a_table_added_later_is_left_out_until_someone_decides(tmp_path) -> None:
    _live(tmp_path)
    con = sqlite3.connect(tmp_path / "live" / "sessions.sqlite")
    con.execute("CREATE TABLE api_keys (key TEXT)")
    con.execute("INSERT INTO api_keys VALUES ('NEW-SECRET')")
    con.commit()
    con.close()
    accounts = _backup(tmp_path).path / "accounts.sqlite"
    assert "api_keys" not in _tables(accounts)
    assert b"NEW-SECRET" not in accounts.read_bytes()


def test_screens_and_the_overlay_come_along(tmp_path) -> None:
    _live(tmp_path)
    report = _backup(tmp_path)
    restored = JobStore(str(report.path / "jobs.sqlite"))
    assert restored.latest_done(source=SOURCE_REFRESH)["job_id"] == "screen1"
    assert (report.path / "overlay_metrics.csv").read_text().startswith("symbol,pe")
    assert report.files == {
        "accounts.sqlite": "1 account(s), 1 passkey(s)",
        "jobs.sqlite": "1 screen(s)",
        "overlay_metrics.csv": "18 bytes",
    }


def test_each_copy_is_one_self_contained_file(tmp_path) -> None:
    """The live databases are in WAL mode; a copy that kept the flag would need companions."""
    _live(tmp_path)
    report = _backup(tmp_path)
    assert sorted(p.name for p in report.path.iterdir()) == [
        "accounts.sqlite", "jobs.sqlite", "overlay_metrics.csv"]
    for name in ("accounts.sqlite", "jobs.sqlite"):
        con = sqlite3.connect(report.path / name)
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        con.close()


# --- while the app is running ---------------------------------------------------------------

def test_a_write_in_progress_is_neither_blocked_nor_half_copied(tmp_path) -> None:
    _live(tmp_path)
    writer = sqlite3.connect(tmp_path / "live" / "jobs.sqlite", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO jobs (id, status, progress, created_at) "
                   "VALUES ('uncommitted', 'running', '[]', '2026-09-26')")
    try:
        report = _backup(tmp_path)  # must not wait on the open write
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    copy = sqlite3.connect(report.path / "jobs.sqlite")
    ids = {r[0] for r in copy.execute("SELECT id FROM jobs")}
    copy.close()
    assert ids == {"screen1"}


def test_the_live_files_are_not_written_to(tmp_path) -> None:
    _live(tmp_path)
    live = tmp_path / "live" / "sessions.sqlite"
    before = live.read_bytes()
    _backup(tmp_path)
    assert live.read_bytes() == before


# --- keeping a few ---------------------------------------------------------------------------

def test_the_newest_are_kept_and_nothing_else_in_the_folder_is_touched(tmp_path) -> None:
    _live(tmp_path)
    mine = tmp_path / "backups" / "notes-from-a-human"
    mine.mkdir(parents=True)
    for day in range(5):
        _backup(tmp_path, now=NOW + timedelta(days=day), keep=3)
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert names == ["2026-09-28T043000", "2026-09-29T043000", "2026-09-30T043000",
                     "notes-from-a-human"]


def test_a_crashed_run_leaves_nothing_that_looks_finished(tmp_path, monkeypatch) -> None:
    import wheel_screener.jobs.backup as backup

    _live(tmp_path)

    def boom(*args, **kwargs):
        raise BackupError("disk full")

    monkeypatch.setattr(backup, "_copy_sqlite", boom)
    with pytest.raises(BackupError):
        _backup(tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_a_fresh_deployment_backs_up_what_exists(tmp_path) -> None:
    (tmp_path / "live").mkdir()
    JobStore(str(tmp_path / "live" / "jobs.sqlite"))
    report = _backup(tmp_path)
    assert set(report.files) == {"jobs.sqlite"}


def test_keeping_none_is_refused(tmp_path) -> None:
    with pytest.raises(BackupError, match="at least 1"):
        _backup(tmp_path, keep=0)
