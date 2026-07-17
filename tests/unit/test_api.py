from __future__ import annotations

import asyncio
import re
import threading
import time

import pytest

pytest.importorskip("fastapi")  # only runs when the `api` extra is installed

from datetime import UTC, date, datetime, timedelta  # noqa: E402

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
        self.seen_criteria = None  # last criteria run_screen was called with (assert form wiring)

    def run_screen(self, criteria, today, *, cancel=None):
        self.seen_criteria = criteria
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

    def search_ticker(self, symbol, criteria, today, *, n=5):
        from wheel_screener.core.service import TickerSearch

        if self._error is not None:
            raise self._error
        return TickerSearch(
            symbol=symbol.upper(), puts=list(self._result or []),
            passes_fundamentals=True, gate_reasons=[], next_earnings=None,
            fundamental_score=0.7, peer_percentile=0.62,
        )


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


def test_search_route_renders_puts() -> None:
    app.dependency_overrides[get_service] = lambda: _FakeService(result=[_candidate("AAA")])
    try:
        r = TestClient(app).post("/search", data={"ticker": "aaa", "top_n": 5})
        assert r.status_code == 200
        assert "AAA" in r.text and "Breakeven" in r.text
        assert "strength 70/100" in r.text and "62% vs peers" in r.text
        blank = TestClient(app).post("/search", data={"ticker": "", "top_n": 5})
        assert blank.status_code == 422  # empty ticker
    finally:
        app.dependency_overrides.clear()


def test_search_threads_dte_and_delta_into_sort_and_export() -> None:
    app.dependency_overrides[get_service] = lambda: _FakeService(result=[_candidate("AAA")])
    try:
        r = TestClient(app).post("/search", data={
            "ticker": "aaa", "top_n": 5, "min_dte": 14, "max_dte": 60, "target_delta": 0.30,
        })
        assert r.status_code == 200
        # export link + sort hx-vals must carry the user's DTE/delta, not revert to defaults
        assert "min_dte=14" in r.text and "max_dte=60" in r.text and "target_delta=0.3" in r.text
        assert '"min_dte": 14' in r.text and '"target_delta": 0.3' in r.text
    finally:
        app.dependency_overrides.clear()


def test_search_route_sort_and_export() -> None:
    svc = _FakeService(result=[_candidate("AAA", yld=0.10), _candidate("BBB", yld=0.90)])
    app.dependency_overrides[get_service] = lambda: svc
    try:
        client = TestClient(app)
        r = client.post("/search", data={"ticker": "mu", "top_n": 5})
        assert "Export CSV" in r.text and 'hx-post="/search"' in r.text  # export + sortable headers
        asc = client.post("/search", data={"ticker": "mu", "sort": "yield", "order": "asc"})
        assert asc.text.index("10.0%") < asc.text.index("90.0%")  # sorted ascending by yield
        csv = client.get("/search/export.csv?ticker=mu&top_n=5")
        assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
        assert "AAA" in csv.text and "BBB" in csv.text  # symbol column in the export
    finally:
        app.dependency_overrides.clear()


def test_run_blocking_stores_done_result(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    job_id = runner.run_blocking(ScreenCriteria())  # synchronous (CLI/cron precompute path)
    latest = runner.store.latest_done()
    assert latest is not None and latest["job_id"] == job_id and latest["status"] == "done"
    assert len(latest["result"]) == 1 and latest["result"][0]["symbol"] == "AAA"


def test_run_blocking_does_not_hold_or_clobber_the_slot(tmp_path) -> None:
    # a one-shot precompute must NOT hold the single-in-flight slot or leak cancel/thread state,
    # and must leave a subsequent web start() able to run (not wedged).
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    runner.run_blocking(ScreenCriteria())
    assert runner._active is None and runner._cancels == {} and runner._threads == {}
    jid = runner.start(ScreenCriteria())
    runner.wait(jid)
    assert runner.get(jid)["status"] == "done"


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
    JobStore(path).create("stuck", datetime.now(tz=UTC).isoformat())  # recent, left 'running'
    job = JobStore(path).get("stuck")  # a fresh store = a restart -> reconciles
    assert job["status"] == "failed" and job["error"]["type"] == "Interrupted"


def test_old_finished_jobs_are_pruned_on_restart(tmp_path) -> None:
    path = str(tmp_path / "jobs.sqlite")
    s = JobStore(path)
    s.create("ancient", (datetime.now(tz=UTC) - timedelta(days=40)).isoformat())
    s.finish("ancient", "done", result=[])
    assert JobStore(path).get("ancient") is None  # older than retention -> pruned at startup


def test_screen_request_and_criteria_default_to_a_timeout() -> None:
    from wheel_screener.api.schemas import ScreenRequest

    assert ScreenRequest().timeout_seconds == 600.0
    assert ScreenRequest().to_criteria().max_runtime_seconds == 600.0
    assert ScreenCriteria().max_runtime_seconds == 600.0  # CLI paths (refresh-screen) bounded too


def test_to_criteria_maps_options_knobs_and_negates_delta() -> None:
    from wheel_screener.api.schemas import ScreenRequest

    c = ScreenRequest(
        min_price=30, max_price=150, target_delta=0.25, max_abs_delta=0.35,
        min_open_interest=250, max_spread_pct=0.08, min_iv=0.4,
    ).to_criteria()
    assert (c.min_price, c.max_price) == (30.0, 150.0)
    assert c.target_delta == -0.25  # entered as a magnitude; stored as the put's signed delta
    assert c.max_abs_delta == 0.35 and c.min_open_interest == 250
    assert c.max_bid_ask_spread_pct == 0.08 and c.min_iv == 0.4
    d = ScreenRequest().to_criteria()  # defaults preserve prior behavior
    assert (d.min_price, d.max_price, d.target_delta, d.min_iv) == (20.0, 200.0, -0.20, None)


def test_screen_request_rejects_inverted_ranges() -> None:
    import pytest
    from pydantic import ValidationError

    from wheel_screener.api.schemas import ScreenRequest

    with pytest.raises(ValidationError):
        ScreenRequest(min_price=200, max_price=20)          # inverted price band
    with pytest.raises(ValidationError):
        ScreenRequest(target_delta=0.5, max_abs_delta=0.3)  # target beyond the |delta| cap
    with pytest.raises(ValidationError):
        ScreenRequest(min_dte=40, max_dte=20)               # inverted DTE window


def test_body_size_gate_rejects_oversized_post() -> None:
    # a body over the 1MB cap is rejected before routing (declared Content-Length)
    r = TestClient(app).post("/search", data={"ticker": "a" * 2_000_000})
    assert r.status_code == 413


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


def test_health_ok_when_chain_source_ready(tmp_path) -> None:
    # schwab source is ready when its OAuth token file exists (pin the source; dev .env may differ)
    settings = Settings(chain_source="schwab")
    token = tmp_path / "token.json"
    token.write_text("{}")
    settings.schwab.token_path = str(token)
    body = _health_client(settings).get("/health").json()
    assert body == {
        "status": "ok", "store_loaded": True, "chain_source": "schwab", "chain_ready": True,
    }


def test_health_degraded_when_chain_source_unready(tmp_path) -> None:
    settings = Settings(chain_source="schwab")
    settings.schwab.token_path = str(tmp_path / "missing.json")
    body = _health_client(settings).get("/health").json()
    assert body["chain_ready"] is False and body["status"] == "degraded"


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
    # options-quality knobs (#90) are adjustable in Advanced
    for name in ("min_price", "max_price", "target_delta", "max_abs_delta",
                 "min_open_interest", "max_spread_pct", "min_iv"):
        assert f'name="{name}"' in r.text


def test_nav_has_both_tabs_and_marks_active(tmp_path) -> None:
    r = _client(_runner(_FakeService(result=[]), tmp_path)).get("/")
    assert 'href="/"' in r.text and 'href="/search"' in r.text  # both tabs present
    assert 'class="nav-tab active"' in r.text  # the Screener tab is active on /


def test_search_page_renders_its_own_form() -> None:
    # the ticker search now lives on its own tab, not crammed onto the screener
    r = TestClient(app).get("/search")
    assert r.status_code == 200
    assert "Search a ticker" in r.text and 'hx-post="/search"' in r.text
    assert 'name="ticker"' in r.text
    # the search pane exposes target delta + DTE too (#90)
    for name in ("target_delta", "min_dte", "max_dte"):
        assert f'name="{name}"' in r.text


def test_run_flow_polls_then_renders_results(tmp_path) -> None:
    runner = _runner(_FakeService(result=[_candidate()]), tmp_path)
    client = _client(runner)
    started = client.post("/runs", data={"top_n": 50})
    assert started.status_code == 200 and "/progress" in started.text and "hx-get" in started.text
    job_id = _job_id_from(started.text)
    runner.wait(job_id)
    page = client.get(f"/runs/{job_id}/progress")
    assert page.status_code == 200 and "AAA" in page.text and "candidate" in page.text.lower()


def test_run_form_wires_options_knobs_into_criteria(tmp_path) -> None:
    svc = _FakeService(result=[_candidate()])
    runner = _runner(svc, tmp_path)
    started = _client(runner).post("/runs", data={
        "top_n": 10, "min_dte": 21, "max_dte": 35, "min_price": 30, "max_price": 150,
        "target_delta": 0.25, "max_abs_delta": 0.35, "min_open_interest": 250,
        "max_spread_pct": 0.08, "min_iv": "0.4",
    })
    assert started.status_code == 200  # accepted (the poller fragment), not a 422 validation error
    runner.wait(_job_id_from(started.text))
    c = svc.seen_criteria  # the form field names reached ScreenCriteria intact
    assert c is not None and (c.min_price, c.max_price) == (30.0, 150.0)
    assert c.target_delta == -0.25 and c.max_abs_delta == 0.35
    assert c.min_open_interest == 250 and c.max_bid_ask_spread_pct == 0.08 and c.min_iv == 0.4


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


def test_run_form_accepts_comma_formatted_dollar_volume(tmp_path) -> None:
    runner = _runner(_FakeService(result=[]), tmp_path)
    # the accountant-formatted field submits "50,000,000"; commas must be stripped, not 422
    r = _client(runner).post("/runs", data={"top_n": 10, "min_dollar_volume": "50,000,000"})
    assert r.status_code == 200 and "/progress" in r.text


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


def test_app_version_tracks_single_source() -> None:
    # the FastAPI version must derive from the package's __version__ (one source of truth),
    # never a hardcoded string that can drift.
    from wheel_screener import __version__

    assert app.version == __version__


def test_check_basic_auth_pure() -> None:
    import base64

    from wheel_screener.api.app import _Auth, _check_basic_auth, _path_exempt

    auth = _Auth("admin", "s3cret")
    hdr = lambda u, p: "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()  # noqa: E731
    assert _check_basic_auth(hdr("admin", "s3cret"), auth) is True
    assert _check_basic_auth(hdr("admin", "wrong"), auth) is False  # wrong password
    assert _check_basic_auth(hdr("root", "s3cret"), auth) is False  # wrong user
    assert _check_basic_auth(None, auth) is False  # no header
    assert _check_basic_auth("Bearer abc", auth) is False  # wrong scheme
    assert _check_basic_auth("Basic @@notb64@@", auth) is False  # undecodable
    assert _check_basic_auth("Basic " + base64.b64encode(b"nocolon").decode(), auth) is False
    # exemptions: liveness probe + static assets bypass the gate
    assert _path_exempt("/health") and _path_exempt("/static/custom.css")
    assert not _path_exempt("/") and not _path_exempt("/runs/j/results")


def test_rate_limit_throttles_expensive_endpoints() -> None:
    from wheel_screener.api.ratelimit import SlidingWindowLimiter

    app.dependency_overrides[get_service] = lambda: _FakeService(result=[_candidate("AAA")])
    app.state.rate_limiter = SlidingWindowLimiter(per_window=2)  # budget of 2 searches
    try:
        c = TestClient(app)
        assert c.post("/search", data={"ticker": "aaa", "top_n": 5}).status_code == 200
        assert c.post("/search", data={"ticker": "aaa", "top_n": 5}).status_code == 200
        r = c.post("/search", data={"ticker": "aaa", "top_n": 5})  # 3rd -> throttled
        assert r.status_code == 429 and r.headers.get("retry-after") == "60"
    finally:
        app.state.rate_limiter = None
        app.dependency_overrides.clear()


def test_health_reflects_active_chain_source() -> None:
    from wheel_screener.api.deps import get_service, get_settings
    from wheel_screener.config import AlpacaSettings, Settings

    app.dependency_overrides[get_service] = lambda: _FakeService()
    try:
        # Alpaca configured -> ready via key/secret (NOT gated on a Schwab token that never exists)
        app.dependency_overrides[get_settings] = lambda: Settings(
            chain_source="alpaca", alpaca=AlpacaSettings(api_key="k", api_secret="s")
        )
        j = TestClient(app).get("/health").json()
        assert j["chain_source"] == "alpaca" and j["chain_ready"] is True and j["status"] == "ok"
        # Alpaca with no creds -> degraded (explicit empty AlpacaSettings so dev .env can't leak in)
        app.dependency_overrides[get_settings] = lambda: Settings(
            chain_source="alpaca", alpaca=AlpacaSettings()
        )
        j2 = TestClient(app).get("/health").json()
        assert j2["chain_ready"] is False and j2["status"] == "degraded"
    finally:
        app.dependency_overrides.clear()


def test_resolve_auth_fails_closed_when_required() -> None:
    import pytest

    from wheel_screener.api.app import _resolve_auth
    from wheel_screener.config import AuthSettings, Settings

    def _s(**kw) -> Settings:
        return Settings(auth=AuthSettings(**kw))

    # required + no password -> refuse to start (fail closed)
    with pytest.raises(RuntimeError, match="AUTH__PASSWORD"):
        _resolve_auth(_s(required=True))
    # required + password -> gate enabled
    assert _resolve_auth(_s(required=True, password="pw")) is not None
    # not required + no password -> open (dev), no raise
    assert _resolve_auth(_s(required=False)) is None


def test_basic_auth_gate_enforced() -> None:
    from wheel_screener.api.app import _Auth

    app.state.auth = _Auth("admin", "s3cret")  # enable the gate
    try:
        c = TestClient(app)
        r = c.get("/nope")  # protected path, no creds
        assert r.status_code == 401 and r.headers.get("www-authenticate", "").startswith("Basic")
        assert c.get("/nope", auth=("admin", "bad")).status_code == 401  # wrong creds
        assert c.get("/nope", auth=("admin", "s3cret")).status_code != 401  # passes gate (then 404)
        assert c.get("/static/nope.css").status_code != 401  # /static is exempt
    finally:
        app.state.auth = None  # disable again so other tests are unaffected


def test_results_render_legacy_snapshot_missing_new_fields(tmp_path) -> None:
    # a snapshot stored before strength/peer_percentile existed lacks those keys; the results
    # table must still render (Jinja yields Undefined for a missing dict key, not None).
    runner = _runner(_FakeService(), tmp_path)
    runner.store.create("j", datetime.now(tz=UTC).isoformat())
    legacy = _candidate("AAA").model_dump(mode="json")
    legacy.pop("peer_percentile", None)
    legacy.pop("fundamental_score", None)
    runner.store.finish("j", "done", result=[legacy])
    r = _client(runner).get("/runs/j/results")
    assert r.status_code == 200 and "AAA" in r.text and "—" in r.text  # graceful blanks
    # the detail fragment for the same legacy row must not crash either, and renders the card with
    # the absent strength/peers as graceful blanks (not "None/100")
    d = _client(runner).get("/runs/j/candidates/AAA")
    assert d.status_code == 200 and "card-grid" in d.text and "—" in d.text
    assert "/100" not in d.text  # missing strength shows a blank, not a partial value


def test_num2_filter_rounds_floats() -> None:
    from wheel_screener.api.app import _num2

    assert _num2(2.8600000000000003) == "2.86"  # no float display artifact
    assert _num2(80.0) == "80.00" and _num2(1) == "1.00"
    assert _num2(None) == "—" and _num2(True) == "—"  # missing / non-number


def test_results_summary_and_emphasis(tmp_path) -> None:
    runner = _runner(_FakeService(), tmp_path)
    _done_job(
        runner, _candidate("AAA", yld=0.30, score=0.8), _candidate("BBB", yld=0.10, score=0.3)
    )
    r = _client(runner).get("/runs/j/results")
    assert r.status_code == 200
    assert "result-stats" in r.text and "yield" in r.text  # summary stat line
    assert "y-hi" in r.text and "y-lo" in r.text  # yield tiers (0.30 -> hi, 0.10 -> lo)
    assert "score-cell" in r.text and "--pct:" in r.text  # in-cell score bar
    assert "sc-hi" in r.text and "sc-lo" in r.text  # score green→red tiers (0.8 -> hi, 0.3 -> lo)
    assert "$80.00" in r.text  # strike rendered as currency


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
    # structured card (#101): a dl grid with the four mental-model sections, one valid table row
    assert "card-grid" in r.text
    for label in ("Contract", "Market", "Return", "Fundamentals"):
        assert f"<dt>{label}</dt>" in r.text
    assert r.text.count("</tr>") == 1
    assert client.get("/runs/j/candidates/NOPE").status_code == 404


def test_candidate_card_earnings_flag(tmp_path) -> None:
    from datetime import date

    runner = _runner(_FakeService(), tmp_path)
    # _candidate's contract expires 2026-08-15; earnings before that is the real risk (amber badge),
    # earnings after is a quiet note.
    before = _candidate("AAA").model_copy(update={"next_earnings": date(2026, 8, 10)})
    after = _candidate("BBB").model_copy(update={"next_earnings": date(2026, 9, 1)})
    runner.store.create("j", datetime.now(tz=UTC).isoformat())
    runner.store.finish("j", "done", result=[c.model_dump(mode="json") for c in (before, after)])
    client = _client(runner)
    rb = client.get("/runs/j/candidates/AAA").text
    assert "before expiry" in rb and "badge" in rb          # earnings-before-expiry risk flag
    ra = client.get("/runs/j/candidates/BBB").text
    assert "after expiry" in ra and "detail-note" in ra     # quiet note, not the amber risk badge


def test_candidate_card_moneyness(tmp_path) -> None:
    # _candidate's strike is 80; classify vs the underlying and guard divide-by-zero.
    def _spot(sym, price):
        c = _candidate(sym)
        k = c.contract.model_copy(update={"underlying_price": price})
        return c.model_copy(update={"contract": k})

    runner = _runner(_FakeService(), tmp_path)
    otm = _spot("AAA", 100.0)   # strike 80 < spot -> put OTM (the safe zone)
    itm = _spot("BBB", 60.0)    # strike 80 > spot -> put ITM (assignment risk)
    zero = _spot("CCC", 0.0)    # underlying 0 must not divide-by-zero
    runner.store.create("j", datetime.now(tz=UTC).isoformat())
    runner.store.finish("j", "done", result=[c.model_dump(mode="json") for c in (otm, itm, zero)])
    client = _client(runner)
    ao = client.get("/runs/j/candidates/AAA").text
    assert "OTM" in ao and "fund-ok" in ao
    ai = client.get("/runs/j/candidates/BBB").text
    assert "ITM" in ai and "fund-bad" in ai
    az = client.get("/runs/j/candidates/CCC")  # guarded: no divide-by-zero, no moneyness shown
    assert az.status_code == 200 and "OTM" not in az.text and "ITM" not in az.text


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
    desc = client.get("/runs/j/results?sort=strength&order=desc")  # _num(-inf) path, no TypeError
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
