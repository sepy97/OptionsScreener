"""Background screen jobs for the web API.

A screen takes minutes, so it can't run inside a request. `JobRunner.start` launches it on a
background thread and returns a job id immediately; the UI polls `JobStore.get` for progress
(captured from the pipeline's stage logs) and the final result. State lives in a small SQLite
file so it survives a restart and gives a little history. Single in-flight job (one shared
Schwab rate limiter), with explicit cancellation + the existing time-budget seam.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from wheel_screener.core.errors import ProviderError
from wheel_screener.core.models import ScreenCriteria
from wheel_screener.core.service import ScreenerService

_JOB_RETENTION_DAYS = 30  # prune finished jobs older than this so the table stays bounded


class JobBusyError(Exception):
    """A screen is already running (single in-flight by design)."""

    def __init__(self, active_id: str) -> None:
        super().__init__(f"a screen is already running ({active_id})")
        self.active_id = active_id


class JobStore:
    """Tiny SQLite-backed job table. One connection per operation = thread-safe across the
    background worker thread and the request threads."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # concurrent read while the worker writes
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "id TEXT PRIMARY KEY, status TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '[]', "
                "result TEXT, error TEXT, created_at TEXT NOT NULL)"
            )
            # reconcile jobs left 'running' by a crash/restart (the daemon worker is gone)
            conn.execute(
                "UPDATE jobs SET status='failed', error=? WHERE status='running'",
                (json.dumps({"type": "Interrupted", "detail": "interrupted by a restart"}),),
            )
            # prune old finished jobs so the table can't grow without bound (ISO timestamps sort
            # chronologically, so a string comparison is correct here)
            cutoff = (datetime.now(tz=UTC) - timedelta(days=_JOB_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self, sql: str, params: tuple) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def create(self, job_id: str, created_at: str) -> None:
        self._write(
            "INSERT INTO jobs (id, status, progress, created_at) VALUES (?, 'running', '[]', ?)",
            (job_id, created_at),
        )

    def set_progress(self, job_id: str, progress: list[str]) -> None:
        self._write("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(progress), job_id))

    def finish(
        self, job_id: str, status: str, result: list | None = None, error: dict | None = None
    ) -> None:
        self._write(
            "UPDATE jobs SET status=?, result=?, error=? WHERE id=?",
            (
                status,
                json.dumps(result) if result is not None else None,
                json.dumps(error) if error is not None else None,
                job_id,
            ),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {
            "job_id": row["id"],
            "status": row["status"],
            "progress": json.loads(row["progress"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": json.loads(row["error"]) if row["error"] else None,
            "created_at": row["created_at"],
        }

    def get(self, job_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        return self._row(row) if row is not None else None

    def latest_done(self) -> dict | None:
        """Most recent completed/cancelled job — powers the dashboard's 'latest results'."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('done', 'cancelled') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return self._row(row) if row is not None else None


class _ProgressHandler(logging.Handler):
    """Captures the pipeline's stage INFO lines into the job's progress while it runs."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        super().__init__(level=logging.INFO)
        self._store = store
        self._job_id = job_id
        self._messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self._messages.append(record.getMessage())
        try:
            self._store.set_progress(self._job_id, self._messages)
        except Exception:  # noqa: BLE001 - progress is best-effort; never break the run
            pass


class JobRunner:
    def __init__(self, service: ScreenerService, store: JobStore) -> None:
        self._service = service
        self.store = store
        self._lock = threading.Lock()
        self._active: str | None = None
        self._cancels: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start(self, criteria: ScreenCriteria) -> str:
        with self._lock:
            if self._active is not None:
                raise JobBusyError(self._active)
            job_id = uuid.uuid4().hex
            self._active = job_id
        cancel = threading.Event()
        self._cancels[job_id] = cancel
        try:
            self.store.create(job_id, datetime.now(tz=UTC).isoformat())
            thread = threading.Thread(
                target=self._run_and_release, args=(job_id, criteria, cancel), daemon=True
            )
            self._threads[job_id] = thread
            thread.start()
        except Exception:
            # launch failed (e.g. DB/thread error) — free the slot so the runner isn't wedged
            with self._lock:
                self._active = None
            self._cancels.pop(job_id, None)
            self._threads.pop(job_id, None)
            raise
        return job_id

    def _run_and_release(
        self, job_id: str, criteria: ScreenCriteria, cancel: threading.Event
    ) -> None:
        """Thread target for start(): run the job, then release the single-in-flight slot."""
        try:
            self._run(job_id, criteria, cancel)
        finally:
            with self._lock:
                self._active = None

    def run_blocking(self, criteria: ScreenCriteria) -> str:
        """Run a screen synchronously and store it; returns the job id. For a one-shot caller
        (CLI / cron precompute) that wants the result persisted for the web to serve — unlike
        start(), no thread and no single-in-flight gate (so it never touches ``_active``)."""
        job_id = uuid.uuid4().hex
        cancel = threading.Event()
        self._cancels[job_id] = cancel
        try:
            self.store.create(job_id, datetime.now(tz=UTC).isoformat())
            self._run(job_id, criteria, cancel)  # cleans up _cancels/_threads in its finally
        except Exception:
            self._cancels.pop(job_id, None)  # store.create failed before _run could clean up
            raise
        return job_id

    def cancel(self, job_id: str) -> None:
        event = self._cancels.get(job_id)
        if event is not None:
            event.set()

    def get(self, job_id: str) -> dict | None:
        return self.store.get(job_id)

    def wait(self, job_id: str, timeout: float = 10.0) -> None:
        """Join the worker thread — for deterministic tests."""
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)

    def _run(self, job_id: str, criteria: ScreenCriteria, cancel: threading.Event) -> None:
        handler = _ProgressHandler(self.store, job_id)
        core_logger = logging.getLogger("wheel_screener.core")
        core_logger.addHandler(handler)
        try:
            results = self._service.run_screen(criteria, date.today(), cancel=cancel)
            status = "cancelled" if cancel.is_set() else "done"
            self.store.finish(
                job_id, status, result=[r.model_dump(mode="json") for r in results]
            )
        except ProviderError as e:
            self.store.finish(job_id, "failed", error={"type": type(e).__name__, "detail": str(e)})
        except Exception as e:  # noqa: BLE001 - any failure becomes a recorded job error
            self.store.finish(job_id, "failed", error={"type": "InternalError", "detail": str(e)})
        finally:
            # job-scoped cleanup only; the single-in-flight slot (_active) is the caller's, since
            # only start() holds it (run_blocking is a one-shot that doesn't).
            core_logger.removeHandler(handler)
            self._cancels.pop(job_id, None)
            self._threads.pop(job_id, None)
