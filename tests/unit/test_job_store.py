"""The job store's schema migrations, and the record of who started each run."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from wheel_screener.api.jobs import SOURCE_REFRESH, SOURCE_WEB, JobStore

# The table exactly as every release before the migrations created it — which is the shape of the
# database sitting on the droplet right now.
_OLD_SCHEMA = (
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
    "progress TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT, created_at TEXT NOT NULL)"
)


def _old_database(path) -> None:
    conn = sqlite3.connect(path)
    # WAL, like the droplet's: journal mode is stored in the file, and every release has set it on
    # open. Left in the default mode, several stores opening at once collide on *switching* to WAL
    # — a real race, but one only a brand-new file can hit, and not the one under test here.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (id, status, progress, result, created_at)"
        " VALUES (?, 'done', '[]', ?, ?)",
        ("before", "[]", datetime.now(tz=UTC).isoformat()),
    )
    conn.commit()
    conn.close()


def _version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _columns(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()


# --- the migration --------------------------------------------------------------------------

def test_a_database_from_before_the_migrations_is_brought_up_to_date(tmp_path) -> None:
    """The case that matters: `CREATE TABLE IF NOT EXISTS` never adds a column to a table that
    already exists, which is how a new column silently failed to reach a live database (#78)."""
    path = tmp_path / "jobs.sqlite"
    _old_database(path)
    assert "source" not in _columns(path)

    store = JobStore(str(path))
    assert "source" in _columns(path) and _version(path) == 1
    old = store.get("before")
    assert old is not None and old["status"] == "done"
    assert old["source"] is None  # unknown, not guessed


def test_a_fresh_database_takes_the_same_path(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite"
    JobStore(str(path))
    assert "source" in _columns(path) and _version(path) == 1


def test_opening_it_again_changes_nothing(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite"
    _old_database(path)
    JobStore(str(path))
    JobStore(str(path))  # would die on "duplicate column" if the version were not recorded
    assert _version(path) == 1


def test_several_processes_opening_it_at_once_all_succeed(tmp_path) -> None:
    """The web app and a cron'd screen can open the file in the same second — a deploy that lands
    on a screen's minute. The version check and the change must be one step.

    Repeated over many fresh files because one round is a poor detector: with the lock replaced
    by a plain deferred transaction, a single round caught the race about one time in five.
    """
    rounds, threads_per_round = 30, 8
    errors: list[BaseException] = []
    for n in range(rounds):
        path = tmp_path / f"jobs-{n}.sqlite"
        _old_database(path)
        gate = threading.Barrier(threads_per_round)

        def open_it(path=path, gate=gate) -> None:
            gate.wait()
            try:
                JobStore(str(path))
            except BaseException as e:  # noqa: BLE001 - collected and asserted on below
                errors.append(e)

        threads = [threading.Thread(target=open_it) for _ in range(threads_per_round)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _version(path) == 1
    assert errors == []


def test_the_previous_release_can_still_write_to_a_migrated_database(tmp_path) -> None:
    """A rollback runs the old code against the new schema. It inserts without naming `source`,
    which works only because the column is nullable."""
    path = tmp_path / "jobs.sqlite"
    JobStore(str(path))
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jobs (id, status, progress, created_at)"
        " VALUES ('old-code', 'running', '[]', ?)",
        (datetime.now(tz=UTC).isoformat(),),
    )
    conn.commit()
    conn.close()


# --- who started it -------------------------------------------------------------------------

def _stored(store: JobStore, job_id: str, source: str | None, minutes_ago: int) -> None:
    at = (datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)).isoformat()
    store.create(job_id, at, source)
    store.finish(job_id, "done", result=[])


def test_the_latest_refresh_ignores_a_newer_run_from_the_button(tmp_path) -> None:
    store = JobStore(str(tmp_path / "jobs.sqlite"))
    _stored(store, "cron", SOURCE_REFRESH, minutes_ago=60)
    _stored(store, "stranger", SOURCE_WEB, minutes_ago=5)
    assert store.latest_done()["job_id"] == "stranger"  # what "latest" used to mean
    assert store.latest_done(source=SOURCE_REFRESH)["job_id"] == "cron"


def test_a_run_of_unknown_origin_is_not_taken_for_a_refresh(tmp_path) -> None:
    """Rows written before the column existed have no source, and are not guessed into one."""
    path = tmp_path / "jobs.sqlite"
    _old_database(path)
    store = JobStore(str(path))
    assert store.latest_done() is not None
    assert store.latest_done(source=SOURCE_REFRESH) is None
