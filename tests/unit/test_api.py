from __future__ import annotations

import asyncio
import re
import threading
import time

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from datetime import UTC, date, datetime  # noqa: E402

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


def _candidate(
    symbol: str = "AAA", yld: float = 0.2, score: float = 0.5, fund: float | None = None
) -> CandidateResult:
    contract = OptionContract(
        underlying_symbol=symbol, option_symbol=f"{symbol}80P", option_type=OptionType.PUT,
        expiration=date(2026, 8, 15), strike=80.0, dte=40, bid=1.0, ask=1.1, raw={"mark": 1.05},
    )
    return CandidateResult(
        symbol=symbol, contract=contract, annualized_yield=yld, premium=1.0,
        collateral=8000.0, score=score, fundamental_score=fund,
    )


def _done_job(runner: JobRunner, *cands: CandidateResult) -> str:
    runner.store.create("j", datetime.now(tz=UTC).isoformat())
    runner.store.finish("j", "done", result=[c.model_dump(mode="json") for c in cands])
    return "j"


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


def test_run_blocking_stores_done_result(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    job_id = runner.run_blocking(ScreenCriteria())  # synchronous (CLI/cron precompute path)
    latest = runner.store.latest_done()
    assert latest is not None and latest["job_id"] == job_id and latest["status"] == "done"
    assert len(latest["result"]) == 1 and latest["result"][0]["symbol"] == "AAA"


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


# --- HTML (HTMX) UI ---

def _job_id_from(fragment_html: str) -> str:
    return re.search(r"/runs/([0-9a-f]+)/progress", fragment_html).group(1)


def test_dashboard_renders_form(tmp_path) -> None:
    r = _client(_runner(_FakeService(result=[]), tmp_path)).get("/")
    assert r.status_code == 200
    assert 'hx-post="/runs"' in r.text and "Run screen" in r.text
    # segmented "Rank by" control (replaced the confusing 0..1 slider)
    assert "Rank by" in r.text and "Higher yield" in r.text and "Better quality" in r.text
    # DTE is now a primary control; names-to-check moved into Advanced
    assert 'name="min_dte"' in r.text and "Advanced filters" in r.text


def test_run_flow_polls_then_renders_results(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    client = _client(runner)
    started = client.post("/runs", data={"top_n": 50})
    assert started.status_code == 200 and "/progress" in started.text and "hx-get" in started.text
    job_id = _job_id_from(started.text)
    runner.wait(job_id)
    page = client.get(f"/runs/{job_id}/progress")
    assert page.status_code == 200 and "AAA" in page.text and "candidate" in page.text.lower()


def test_run_failure_renders_typed_error(tmp_path) -> None:
    runner = _runner(_FakeService(error=AuthExpiredError("token gone")), tmp_path)
    client = _client(runner)
    job_id = _job_id_from(client.post("/runs", data={"top_n": 10}).text)
    runner.wait(job_id)
    assert "AuthExpiredError" in client.get(f"/runs/{job_id}/progress").text


def test_run_busy_renders_409(tmp_path) -> None:
    gate = threading.Event()
    runner = _runner(_FakeService(gate=gate), tmp_path)
    client = _client(runner)
    first = client.post("/runs", data={"top_n": 10})
    assert first.status_code == 200
    busy = client.post("/runs", data={"top_n": 10})
    assert busy.status_code == 409 and "already running" in busy.text
    gate.set()
    runner.wait(_job_id_from(first.text))


def test_invalid_form_renders_422(tmp_path) -> None:
    r = _client(_runner(_FakeService(), tmp_path)).post(
        "/runs", data={"min_dte": 60, "max_dte": 30}
    )
    assert r.status_code == 422 and "invalid input" in r.text


def test_screen_request_maps_min_dollar_volume() -> None:
    from wheel_screener.api.schemas import ScreenRequest

    assert ScreenRequest(min_dollar_volume=10_000_000).to_criteria().min_dollar_volume == 10_000_000


def test_dashboard_form_exposes_min_dollar_volume(tmp_path) -> None:
    r = _client(_runner(_FakeService(result=[]), tmp_path)).get("/")
    assert r.status_code == 200 and 'name="min_dollar_volume"' in r.text


def test_dashboard_shows_latest_results(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    client = _client(runner)
    runner.wait(_job_id_from(client.post("/runs", data={"top_n": 10}).text))
    r = client.get("/")
    assert r.status_code == 200 and "AAA" in r.text and "Latest results" in r.text
    assert "just now" in r.text and "may be stale" not in r.text  # fresh snapshot


def test_results_sort_by_column(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA", yld=0.1), _candidate("BBB", yld=0.9))
    client = _client(runner)
    desc = client.get("/runs/j/results?sort=yield&order=desc")
    assert desc.status_code == 200 and desc.text.index("BBB") < desc.text.index("AAA")
    asc = client.get("/runs/j/results?sort=symbol&order=asc").text
    assert asc.index("AAA") < asc.index("BBB")
    assert "/runs/j/results?sort=" in desc.text  # headers are sortable links


def test_num2_filter_rounds_floats() -> None:
    from wheel_screener.api.app import _num2

    assert _num2(2.8600000000000003) == "2.86"  # no float display artifact
    assert _num2(80.0) == "80.00" and _num2(1) == "1.00"
    assert _num2(None) == "—" and _num2(True) == "—"  # missing / non-number


def test_results_summary_and_emphasis(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA", yld=0.30), _candidate("BBB", yld=0.10))
    r = _client(runner).get("/runs/j/results")
    assert r.status_code == 200
    assert "result-stats" in r.text and "yield" in r.text  # summary stat line
    assert "y-hi" in r.text and "y-lo" in r.text  # yield tiers (0.30 -> hi, 0.10 -> lo)
    assert "score-cell" in r.text and "--pct:" in r.text  # in-cell score bar


def test_export_csv(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA"))
    r = _client(runner).get("/runs/j/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    lines = r.text.splitlines()
    assert lines[0].startswith("symbol,option_symbol,strike")  # header row
    assert any(line.startswith("AAA,") for line in lines[1:])  # data row
    assert _client(runner).get("/runs/nope/export.csv").status_code == 404  # unknown run


def test_results_view_has_export_button(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA"))
    r = _client(runner).get("/runs/j/results")
    assert "/runs/j/export.csv" in r.text and "download" in r.text


def test_candidate_detail_fragment(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA"))
    client = _client(runner)
    r = client.get("/runs/j/candidates/AAA")
    # a detail ROW (inserted after the data row via afterend), with a close control
    assert r.status_code == 200 and "<tr" in r.text and "detail-close" in r.text
    assert "AAA80P" in r.text and "collateral" in r.text.lower()
    assert client.get("/runs/j/candidates/NOPE").status_code == 404


def test_results_table_symbol_click_expands_detail(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA"))
    r = _client(runner).get("/runs/j/results")
    # symbol click inserts the detail as a new sibling row — no fragile hidden-row + :has reveal
    assert "/runs/j/candidates/AAA" in r.text
    assert 'hx-target="closest tr"' in r.text and 'hx-swap="afterend"' in r.text


def test_detail_link_safe_for_dotted_tickers(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("BRK.B"))  # share-class ticker with a dot
    client = _client(runner)
    r = client.get("/runs/j/results")
    # no DOM id/selector is built from the symbol now, so dots can't break anything
    assert "/runs/j/candidates/BRK.B" in r.text  # symbol only in the URL
    assert client.get("/runs/j/candidates/BRK.B").status_code == 200  # route resolves it


def test_sort_handles_nulls_and_nested_columns(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(runner, _candidate("AAA", fund=None), _candidate("BBB", fund=0.9))
    client = _client(runner)
    desc = client.get("/runs/j/results?sort=fund&order=desc")  # _num(-inf) path, no TypeError
    assert desc.status_code == 200 and desc.text.index("BBB") < desc.text.index("AAA")  # null sinks
    assert client.get("/runs/j/results?sort=strike&order=asc").status_code == 200  # nested key


def test_humanize_age() -> None:
    from datetime import timedelta

    from wheel_screener.api.app import _humanize_age

    now = datetime.now(tz=UTC)
    assert _humanize_age(now.isoformat()) == ("just now", False)
    label, stale = _humanize_age((now - timedelta(hours=3)).isoformat())
    assert label == "3h ago" and stale is True
    assert _humanize_age((now - timedelta(days=2)).isoformat())[0] == "2d ago"
    assert _humanize_age("not-a-date") == ("", False)  # never raises


def test_dashboard_flags_stale_snapshot(tmp_path) -> None:
    from datetime import timedelta

    runner = _runner(_FakeService(), tmp_path)
    old = (datetime.now(tz=UTC) - timedelta(hours=5)).isoformat()
    runner.store.create("old", old)
    runner.store.finish("old", "done", result=[_candidate().model_dump(mode="json")])
    r = _client(runner).get("/")
    assert "5h ago" in r.text and "may be stale" in r.text
