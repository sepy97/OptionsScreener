from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from datetime import date  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from wheel_screener.api.app import _provider_error_handler, app  # noqa: E402
from wheel_screener.api.deps import get_job_runner, get_service, get_settings  # noqa: E402
from wheel_screener.api.jobs import JobRunner, JobStore  # noqa: E402
from wheel_screener.config import Settings  # noqa: E402
from wheel_screener.core.errors import (  # noqa: E402
    AuthExpiredError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import (  # noqa: E402
    CandidateResult,
    OptionContract,
    OptionType,
    ScreenCriteria,
)


class _FakeFundamentals:
    def known_symbols(self) -> set[str]:
        return {"AAA"}


def _candidate() -> CandidateResult:
    contract = OptionContract(
        underlying_symbol="AAA", option_symbol="AAA80P", option_type=OptionType.PUT,
        expiration=date(2026, 8, 15), strike=80.0, dte=40, bid=1.0, ask=1.1, raw={"mark": 1.05},
    )
    return CandidateResult(
        symbol="AAA", contract=contract, annualized_yield=0.2, premium=1.0,
        collateral=8000.0, score=0.5,
    )


class _FakeService:
    """run_screen is configurable: return a result, raise, block on a gate, or honor cancel."""

    def __init__(self, result=None, error=None, gate=None, wait_cancel=False) -> None:
        self.fundamentals = _FakeFundamentals()
        self._result = result if result is not None else []
        self._error = error
        self._gate = gate
        self._wait_cancel = wait_cancel

    def run_screen(self, criteria, today, *, cancel=None):
        if self._gate is not None:
            self._gate.wait(2.0)
        if self._wait_cancel and cancel is not None:
            for _ in range(200):
                if cancel.is_set():
                    break
                time.sleep(0.01)
        if self._error is not None:
            raise self._error
        return self._result


def _runner(service: _FakeService, tmp_path) -> JobRunner:
    return JobRunner(service, JobStore(str(tmp_path / "jobs.sqlite")))


def _client(runner: JobRunner) -> TestClient:
    app.dependency_overrides[get_job_runner] = lambda: runner
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_start_poll_returns_results_with_clean_contract(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    client = _client(runner)
    r = client.post("/screen", json={})
    assert r.status_code == 202  # returns immediately with a job id (no minutes-long block)
    job_id = r.json()["job_id"]
    runner.wait(job_id)
    job = client.get(f"/screen/{job_id}").json()
    assert job["status"] == "done" and len(job["result"]) == 1
    contract = job["result"][0]["contract"]
    assert "spread_pct" in contract and "raw" not in contract  # M3.0 contract over the wire


def test_second_screen_while_running_is_409(tmp_path) -> None:
    gate = threading.Event()
    runner = _runner(_FakeService(gate=gate), tmp_path)
    client = _client(runner)
    first = client.post("/screen", json={})
    assert first.status_code == 202
    assert client.post("/screen", json={}).status_code == 409  # single in-flight
    gate.set()
    runner.wait(first.json()["job_id"])


def test_cancel_marks_job_cancelled(tmp_path) -> None:
    runner = _runner(_FakeService(wait_cancel=True), tmp_path)
    client = _client(runner)
    job_id = client.post("/screen", json={}).json()["job_id"]
    assert client.post(f"/screen/{job_id}/cancel").status_code == 202
    runner.wait(job_id)
    assert client.get(f"/screen/{job_id}").json()["status"] == "cancelled"


def test_failed_job_records_typed_error(tmp_path) -> None:
    runner = _runner(_FakeService(error=AuthExpiredError("token gone")), tmp_path)
    client = _client(runner)
    job_id = client.post("/screen", json={}).json()["job_id"]
    runner.wait(job_id)
    job = client.get(f"/screen/{job_id}").json()
    assert job["status"] == "failed" and job["error"]["type"] == "AuthExpiredError"


def test_unknown_job_returns_404(tmp_path) -> None:
    assert _client(_runner(_FakeService(), tmp_path)).get("/screen/nope").status_code == 404


def test_inverted_dte_window_is_422(tmp_path) -> None:
    client = _client(_runner(_FakeService(), tmp_path))
    assert client.post("/screen", json={"min_dte": 60, "max_dte": 30}).status_code == 422


def test_cancel_after_done_reports_terminal_status(tmp_path) -> None:
    runner = _runner(_FakeService(result=[]), tmp_path)
    client = _client(runner)
    job_id = client.post("/screen", json={}).json()["job_id"]
    runner.wait(job_id)  # already done
    r = client.post(f"/screen/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "done"  # not a false "cancelling"


def test_restart_marks_stuck_running_jobs_failed(tmp_path) -> None:
    path = str(tmp_path / "jobs.sqlite")
    JobStore(path).create("stuck", "2026-01-01T00:00:00Z")  # left 'running'
    job = JobStore(path).get("stuck")  # a fresh store = a restart -> reconciles
    assert job["status"] == "failed" and job["error"]["type"] == "Interrupted"


def test_start_failure_does_not_wedge_runner(tmp_path) -> None:
    class _BadStore(JobStore):
        def __init__(self, p: str) -> None:
            super().__init__(p)
            self.fail_next = True

        def create(self, job_id: str, created_at: str) -> None:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("create boom")
            super().create(job_id, created_at)

    runner = JobRunner(_FakeService(result=[]), _BadStore(str(tmp_path / "j.sqlite")))
    with pytest.raises(RuntimeError):
        runner.start(ScreenCriteria())  # launch fails
    job_id = runner.start(ScreenCriteria())  # not wedged: a second start works
    runner.wait(job_id)
    assert runner.get(job_id)["status"] == "done"


def test_provider_error_handler_maps_status() -> None:
    # the defensive handler still maps typed errors to HTTP codes (for any sync path)
    auth = asyncio.run(_provider_error_handler(None, AuthExpiredError("x")))
    rate = asyncio.run(_provider_error_handler(None, RateLimitedError("x")))
    down = asyncio.run(_provider_error_handler(None, ProviderUnavailableError("x")))
    assert (auth.status_code, rate.status_code, down.status_code) == (401, 429, 503)
    assert rate.headers.get("Retry-After") == "60"


def _health_client(settings: Settings) -> TestClient:
    app.dependency_overrides[get_service] = lambda: _FakeService()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_health_ok_with_store_and_token(tmp_path) -> None:
    settings = Settings()
    token = tmp_path / "token.json"
    token.write_text("{}")
    settings.schwab.token_path = str(token)
    body = _health_client(settings).get("/health").json()
    assert body == {"status": "ok", "store_loaded": True, "schwab_token": True}


def test_health_degraded_without_token(tmp_path) -> None:
    settings = Settings()
    settings.schwab.token_path = str(tmp_path / "missing.json")
    body = _health_client(settings).get("/health").json()
    assert body["schwab_token"] is False and body["status"] == "degraded"
