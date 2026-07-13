"""FastAPI app — serves the core ScreenerService as JSON for the web UI (and a future client).

Run (after ``uv sync --extra api``): ``uv run uvicorn wheel_screener.api.app:app --reload``.

A screen takes minutes, so ``POST /screen`` starts a BACKGROUND job and returns a job id; the
UI polls ``GET /screen/{id}`` for progress + results and can ``POST /screen/{id}/cancel``.
One service + one job runner are built at startup (lifespan) and shared across requests.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from wheel_screener.api.deps import get_job_runner, get_service, get_settings
from wheel_screener.api.jobs import JobBusyError, JobRunner, JobStore
from wheel_screener.api.schemas import ScreenRequest
from wheel_screener.composition import build_service
from wheel_screener.config import Settings
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.service import ScreenerService

logger = logging.getLogger(__name__)

# typed provider errors -> HTTP status (checked most-specific first)
_ERROR_STATUS: list[tuple[type[ProviderError], int]] = [
    (AuthExpiredError, 401),
    (RateLimitedError, 429),
    (ProviderUnavailableError, 503),
    (ProviderDataError, 422),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the service + job runner ONCE; warm the store. Requests share them."""
    settings = Settings()
    service = build_service(settings)
    app.state.settings = settings
    app.state.service = service
    app.state.job_runner = JobRunner(service, JobStore(settings.jobs_db_path))
    # let pipeline INFO logs through so background jobs can capture stage progress
    logging.getLogger("wheel_screener.core").setLevel(logging.INFO)
    warm = getattr(service.fundamentals, "known_symbols", None)
    if warm is not None:
        try:
            warm()
        except Exception as e:  # noqa: BLE001 - missing store/keys shouldn't crash startup
            logger.warning("startup store warm failed: %s", e)
    yield


app = FastAPI(title="Wheel Screener API", version="0.1.0", lifespan=lifespan)

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


def _opt_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    return float(raw) if raw else None


# option prices/IV move intraday, so a precomputed snapshot older than this is flagged stale
_STALE_AFTER_SECONDS = 3600


def _humanize_age(created_at: str) -> tuple[str, bool]:
    """(human age, is_stale) for a stored run's UTC ISO timestamp — so the dashboard can show
    how old the precomputed snapshot is and warn when it's worth re-running."""
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return ("", False)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    secs = max((datetime.now(tz=UTC) - created).total_seconds(), 0.0)
    if secs < 90:
        label = "just now"
    elif secs < 3600:
        label = f"{int(secs // 60)}m ago"
    elif secs < 86400:
        label = f"{int(secs // 3600)}h ago"
    else:
        label = f"{int(secs // 86400)}d ago"
    return (label, secs > _STALE_AFTER_SECONDS)


def _results_summary(results: list | None) -> dict | None:
    """Yield/DTE range across a result set — a compact 'what am I looking at' line."""
    if not results:
        return None
    ys = [c["annualized_yield"] for c in results if c.get("annualized_yield") is not None]
    dtes = [
        c["contract"]["dte"]
        for c in results
        if c.get("contract") and c["contract"].get("dte") is not None
    ]
    return {
        "yield_min": min(ys) if ys else None, "yield_max": max(ys) if ys else None,
        "dte_min": min(dtes) if dtes else None, "dte_max": max(dtes) if dtes else None,
    }


def _num(v: object) -> float:
    # non-numeric/missing -> -inf: clusters nulls at the bottom under the default desc sort
    # (and at the top when a column is toggled ascending). The point is a stable, no-TypeError key.
    return float(v) if isinstance(v, (int, float)) else float("-inf")


# sort key -> accessor over a serialized CandidateResult dict (for the sortable results table)
_SORT_KEYS = {
    "symbol": lambda c: c.get("symbol") or "",
    "strike": lambda c: _num(c.get("contract", {}).get("strike")),
    "exp": lambda c: c.get("contract", {}).get("expiration") or "",
    "dte": lambda c: _num(c.get("contract", {}).get("dte")),
    "delta": lambda c: _num(c.get("contract", {}).get("delta")),
    "iv": lambda c: _num(c.get("contract", {}).get("implied_volatility")),
    "bid": lambda c: _num(c.get("contract", {}).get("bid")),
    "mid": lambda c: _num(c.get("contract", {}).get("mid")),
    "oi": lambda c: _num(c.get("contract", {}).get("open_interest")),
    "yield": lambda c: _num(c.get("annualized_yield")),
    "fund": lambda c: _num(c.get("fundamental_score")),
    "score": lambda c: _num(c.get("score")),
}


@app.exception_handler(ProviderError)
async def _provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    status = 502  # an unclassified provider failure
    for cls, code in _ERROR_STATUS:
        if isinstance(exc, cls):
            status = code
            break
    headers = {"Retry-After": "60"} if isinstance(exc, RateLimitedError) else None
    return JSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "detail": str(exc)},
        headers=headers,
    )


@app.get("/health")
def health(
    service: ScreenerService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Liveness + readiness: is the local store loaded, and is a Schwab token present?"""
    known = getattr(service.fundamentals, "known_symbols", None)
    try:
        store_loaded = bool(known()) if known is not None else True
    except Exception:  # noqa: BLE001 - health must never raise
        store_loaded = False
    token_present = Path(settings.schwab.token_path).expanduser().exists()
    return {
        "status": "ok" if (store_loaded and token_present) else "degraded",
        "store_loaded": store_loaded,
        "schwab_token": token_present,
    }


@app.post("/screen", status_code=202)
def start_screen(req: ScreenRequest, runner: JobRunner = Depends(get_job_runner)) -> dict:
    """Start a screen as a background job; returns a job id to poll. 409 if one is running."""
    try:
        job_id = runner.start(req.to_criteria())
    except JobBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"job_id": job_id, "status": "running", "poll": f"/screen/{job_id}"}


@app.get("/screen/{job_id}")
def get_screen(job_id: str, runner: JobRunner = Depends(get_job_runner)) -> dict:
    """Poll a screen job: status (running/done/failed/cancelled), progress, result/error."""
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job


@app.post("/screen/{job_id}/cancel")
def cancel_screen(
    job_id: str, response: Response, runner: JobRunner = Depends(get_job_runner)
) -> dict:
    """Request cancellation; the run stops and returns whatever it collected (partial)."""
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job["status"] != "running":  # already terminal — report its real status, don't pretend
        response.status_code = 200
        return {"job_id": job_id, "status": job["status"]}
    runner.cancel(job_id)
    response.status_code = 202
    return {"job_id": job_id, "status": "cancelling"}


# --- HTML (HTMX) UI -------------------------------------------------------------------------


@app.get("/")
def dashboard(request: Request, runner: JobRunner = Depends(get_job_runner)):
    latest = runner.store.latest_done()
    age, stale = _humanize_age(latest["created_at"]) if latest else ("", False)
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "defaults": ScreenRequest(), "latest": latest, "latest_age": age,
            "latest_stale": stale,
            "summary": _results_summary(latest["result"] if latest else None),
        },
    )


@app.post("/runs")
def start_run(
    request: Request,
    top_n: int = Form(150),
    fundamental_weight: float = Form(0.5),
    min_dollar_volume: float = Form(25_000_000.0),
    min_yield: str = Form(""),
    min_dte: int = Form(21),
    max_dte: int = Form(35),
    timeout_seconds: str = Form(""),
    runner: JobRunner = Depends(get_job_runner),
):
    try:
        req = ScreenRequest(
            top_n=top_n, fundamental_weight=fundamental_weight,
            min_dollar_volume=min_dollar_volume, min_yield=_opt_float(min_yield),
            min_dte=min_dte, max_dte=max_dte, timeout_seconds=_opt_float(timeout_seconds),
        )
    except (ValidationError, ValueError) as e:
        return templates.TemplateResponse(
            request, "_error.html", {"message": f"invalid input: {e}"}, status_code=422
        )
    try:
        job_id = runner.start(req.to_criteria())
    except JobBusyError as e:
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(e)}, status_code=409
        )
    job = {"job_id": job_id, "status": "running", "progress": []}
    return templates.TemplateResponse(request, "_progress.html", {"job": job})


@app.get("/runs/{job_id}/progress")
def run_progress(request: Request, job_id: str, runner: JobRunner = Depends(get_job_runner)):
    job = runner.get(job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "unknown run"}, status_code=404
        )
    if job["status"] == "running":
        return templates.TemplateResponse(request, "_progress.html", {"job": job})
    if job["status"] == "failed":
        err = job.get("error") or {}
        message = f"{err.get('type', 'error')}: {err.get('detail', '')}"
        return templates.TemplateResponse(request, "_error.html", {"message": message})
    return templates.TemplateResponse(  # done / cancelled
        request, "_results.html", {"job": job, "summary": _results_summary(job.get("result"))}
    )


@app.get("/runs/{job_id}/results")
def run_results(
    request: Request, job_id: str, sort: str = "score", order: str = "desc",
    runner: JobRunner = Depends(get_job_runner),
):
    """Re-render the results table sorted by a column (HTMX swaps it in place)."""
    job = runner.get(job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "unknown run"}, status_code=404
        )
    order = "asc" if order.lower() == "asc" else "desc"  # normalize so the arrow can't desync
    results = list(job.get("result") or [])
    keyfn = _SORT_KEYS.get(sort)
    if keyfn is not None:
        results.sort(key=keyfn, reverse=(order != "asc"))
    return templates.TemplateResponse(
        request, "_results.html",
        {
            "job": {**job, "result": results}, "sort_key": sort, "sort_order": order,
            "summary": _results_summary(results),
        },
    )


@app.get("/runs/{job_id}/candidates/{symbol}")
def run_candidate(
    request: Request, job_id: str, symbol: str, runner: JobRunner = Depends(get_job_runner)
):
    """Candidate detail fragment (row-expand) — keyed by symbol so it survives re-sorting."""
    job = runner.get(job_id)
    cand = None
    if job is not None:
        cand = next((c for c in (job.get("result") or []) if c.get("symbol") == symbol), None)
    if cand is None:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "unknown candidate"}, status_code=404
        )
    return templates.TemplateResponse(request, "_candidate.html", {"c": cand})


@app.post("/runs/{job_id}/cancel")
def cancel_run(request: Request, job_id: str, runner: JobRunner = Depends(get_job_runner)):
    job = runner.get(job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "unknown run"}, status_code=404
        )
    if job["status"] == "running":
        runner.cancel(job_id)
    return templates.TemplateResponse(request, "_progress.html", {"job": runner.get(job_id)})
