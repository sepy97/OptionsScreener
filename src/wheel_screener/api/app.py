"""FastAPI app — serves the core ScreenerService as JSON for the web UI (and a future client).

Run (after ``uv sync --extra api``): ``uv run uvicorn wheel_screener.api.app:app --reload``.

A screen takes minutes, so ``POST /screen`` starts a BACKGROUND job and returns a job id; the
UI polls ``GET /screen/{id}`` for progress + results and can ``POST /screen/{id}/cancel``.
One service + one job runner are built at startup (lifespan) and shared across requests.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from wheel_screener import __version__
from wheel_screener.api.deps import get_job_runner, get_service, get_settings
from wheel_screener.api.jobs import JobBusyError, JobRunner, JobStore
from wheel_screener.api.ratelimit import SlidingWindowLimiter, client_ip, is_expensive
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
from wheel_screener.core.models import ScreenCriteria
from wheel_screener.core.service import ScreenerService

logger = logging.getLogger(__name__)

# typed provider errors -> HTTP status (checked most-specific first)
_ERROR_STATUS: list[tuple[type[ProviderError], int]] = [
    (AuthExpiredError, 401),
    (RateLimitedError, 429),
    (ProviderUnavailableError, 503),
    (ProviderDataError, 422),
]


# ---- HTTP Basic Auth gate --------------------------------------------------
# Single-user gate. Enabled only when a password is configured; /health and /static stay open.


@dataclass(frozen=True)
class _Auth:
    user: str
    password: str


def _auth_from_settings(settings: Settings) -> _Auth | None:
    """The configured credentials, or None when no password is set (gate disabled)."""
    pw = settings.auth.password.get_secret_value()
    return _Auth(settings.auth.user, pw) if pw else None


def _resolve_auth(settings: Settings) -> _Auth | None:
    """Credentials for the gate, or None (open). Fails CLOSED: when ``AUTH__REQUIRED`` is set but
    no password is configured, raise so the app refuses to start unauthenticated (prod safety)."""
    auth = _auth_from_settings(settings)
    if auth is None and settings.auth.required:
        raise RuntimeError(
            "AUTH__REQUIRED=true but AUTH__PASSWORD is empty — refusing to start unauthenticated"
        )
    return auth


def _path_exempt(path: str) -> bool:
    """Liveness probe + static assets bypass auth (so uptime checks and CSS work)."""
    return path == "/health" or path == "/static" or path.startswith("/static/")


def _check_basic_auth(header: str | None, auth: _Auth) -> bool:
    """Constant-time check of an ``Authorization: Basic`` header against the credentials."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, sep, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if not sep:  # no colon = malformed
        return False
    ok_user = secrets.compare_digest(user, auth.user)
    ok_pw = secrets.compare_digest(pw, auth.password)
    return ok_user and ok_pw


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the service + job runner ONCE; warm the store. Requests share them."""
    settings = Settings()
    service = build_service(settings)
    app.state.settings = settings
    app.state.service = service
    app.state.auth = _resolve_auth(settings)  # raises if AUTH__REQUIRED but no password (prod)
    if app.state.auth is None:
        logger.warning("web auth DISABLED (no AUTH__PASSWORD) — set AUTH__REQUIRED=true in prod")
    app.state.rate_limiter = (
        SlidingWindowLimiter(settings.rate_limit.per_minute)
        if settings.rate_limit.enabled else None
    )
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


app = FastAPI(title="Wheel Screener API", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def _basic_auth_gate(request: Request, call_next):
    """Reject requests without valid Basic-Auth credentials when the gate is enabled."""
    auth = getattr(request.app.state, "auth", None)
    if auth is not None and not _path_exempt(request.url.path):
        if not _check_basic_auth(request.headers.get("Authorization"), auth):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="wheel-screener"'},
            )
    return await call_next(request)


_MAX_BODY_BYTES = 1_000_000  # 1 MB — the POST forms are tiny; reject anything absurd


@app.middleware("http")
async def _body_size_gate(request: Request, call_next):
    """Reject oversized request bodies (declared Content-Length) before routing — a cheap OOM
    guard. Caddy's request_body max_size is the real edge enforcement; this is the app backstop."""
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl is not None and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
            return Response("Request body too large.", status_code=413)
    return await call_next(request)


@app.middleware("http")
async def _rate_limit_gate(request: Request, call_next):
    """Per-IP throttle on the expensive endpoints (screen starts + search); cheap reads pass."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None and is_expensive(request.method, request.url.path):
        ip = client_ip(
            request.headers.get("x-forwarded-for"),
            request.client.host if request.client else "",
        )
        if not limiter.allow(ip, time.monotonic()):
            return Response(
                "Rate limit exceeded — please slow down.",
                status_code=429,
                headers={"Retry-After": "60"},
            )
    return await call_next(request)

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


# CSV export columns: (header, accessor over a serialized CandidateResult dict)
_EXPORT_COLUMNS: list[tuple[str, object]] = [
    ("symbol", lambda c: c.get("symbol")),
    ("option_symbol", lambda c: (c.get("contract") or {}).get("option_symbol")),
    ("strike", lambda c: (c.get("contract") or {}).get("strike")),
    ("expiration", lambda c: (c.get("contract") or {}).get("expiration")),
    ("dte", lambda c: (c.get("contract") or {}).get("dte")),
    ("delta", lambda c: (c.get("contract") or {}).get("delta")),
    ("iv", lambda c: (c.get("contract") or {}).get("implied_volatility")),
    ("bid", lambda c: (c.get("contract") or {}).get("bid")),
    ("ask", lambda c: (c.get("contract") or {}).get("ask")),
    ("mid", lambda c: (c.get("contract") or {}).get("mid")),
    ("spread_pct", lambda c: (c.get("contract") or {}).get("spread_pct")),
    ("open_interest", lambda c: (c.get("contract") or {}).get("open_interest")),
    ("annualized_yield", lambda c: c.get("annualized_yield")),
    ("premium", lambda c: c.get("premium")),
    ("collateral", lambda c: c.get("collateral")),
    ("strength", lambda c: c.get("fundamental_score")),
    ("peer_percentile", lambda c: c.get("peer_percentile")),
    ("score", lambda c: c.get("score")),
    ("next_earnings", lambda c: c.get("next_earnings")),
]


def _candidates_csv(results: list | None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in _EXPORT_COLUMNS])
    for c in results or []:
        writer.writerow([fn(c) for _, fn in _EXPORT_COLUMNS])
    return buf.getvalue()


def _num2(v: object) -> str:
    """Render a number to 2 decimals ('—' if missing) — avoids float artifacts like 2.860000003."""
    return f"{v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "—"


templates.env.filters["num2"] = _num2


def _usd(v: object) -> str:
    """Accountant-style thousands separators (25000000 -> '25,000,000')."""
    return f"{v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


templates.env.filters["usd"] = _usd


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
    "strength": lambda c: _num(c.get("fundamental_score")),
    "peers": lambda c: _num(c.get("peer_percentile")),
    "score": lambda c: _num(c.get("score")),
}

# sort key -> accessor over a CandidateResult OBJECT (the ticker-search table works on objects)
_SEARCH_SORT_KEYS = {
    "strike": lambda c: c.contract.strike,
    "exp": lambda c: c.contract.expiration.isoformat(),
    "dte": lambda c: c.contract.dte,
    "delta": lambda c: _num(c.contract.delta),
    "iv": lambda c: _num(c.contract.implied_volatility),
    "bid": lambda c: _num(c.contract.bid),
    "mid": lambda c: _num(c.contract.mid),
    "spread": lambda c: _num(c.contract.spread_pct),
    "oi": lambda c: _num(c.contract.open_interest),
    "yield": lambda c: _num(c.annualized_yield),
    "breakeven": lambda c: c.contract.strike - (c.premium or 0.0),
    "collateral": lambda c: _num(c.collateral),
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
    """Liveness + readiness: is the local store loaded, and is the ACTIVE chain source ready?"""
    known = getattr(service.fundamentals, "known_symbols", None)
    try:
        store_loaded = bool(known()) if known is not None else True
    except Exception:  # noqa: BLE001 - health must never raise
        store_loaded = False
    source = settings.chain_source
    if source == "alpaca":  # key/secret auth — ready when both are configured
        chain_ready = bool(
            settings.alpaca.api_key.get_secret_value()
            and settings.alpaca.api_secret.get_secret_value()
        )
    else:  # schwab — ready when the OAuth token file is present
        chain_ready = Path(settings.schwab.token_path).expanduser().exists()
    return {
        "status": "ok" if (store_loaded and chain_ready) else "degraded",
        "store_loaded": store_loaded,
        "chain_source": source,
        "chain_ready": chain_ready,
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
def screener_page(request: Request, runner: JobRunner = Depends(get_job_runner)):
    """The Screener tab (home): the run form + the latest precomputed results."""
    latest = runner.store.latest_done()
    age, stale = _humanize_age(latest["created_at"]) if latest else ("", False)
    return templates.TemplateResponse(
        request, "screener.html",
        {
            "active_tab": "screener",
            "defaults": ScreenRequest(), "latest": latest, "latest_age": age,
            "latest_stale": stale,
            "summary": _results_summary(latest["result"] if latest else None),
        },
    )


@app.get("/search")
def search_page(request: Request):
    """The Search tab: the single-ticker lookup form (results load via POST /search)."""
    return templates.TemplateResponse(request, "search.html", {"active_tab": "search"})


def _search(service: ScreenerService, ticker: str, top_n: int, min_dte: int, max_dte: int,
            target_delta: float):
    criteria = ScreenCriteria(min_dte=min_dte, max_dte=max_dte, target_delta=-abs(target_delta))
    return service.search_ticker((ticker or "").strip().upper(), criteria, date.today(), n=top_n)


@app.post("/search")
def search_route(
    request: Request,
    ticker: str = Form(...),
    top_n: int = Form(5),
    min_dte: int = Form(7),
    max_dte: int = Form(45),
    target_delta: float = Form(0.20),
    sort: str = Form(""),
    order: str = Form("desc"),
    service: ScreenerService = Depends(get_service),
):
    """Single-ticker CSP search — synchronous (one chain pull) top-N puts near the target delta."""
    if not (ticker or "").strip():
        return templates.TemplateResponse(
            request, "_error.html", {"message": "enter a ticker symbol"}, status_code=422
        )
    try:
        result = _search(service, ticker, top_n, min_dte, max_dte, target_delta)
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    keyfn = _SEARCH_SORT_KEYS.get(sort)
    if keyfn is not None:
        order = "asc" if order.lower() == "asc" else "desc"
        result.puts.sort(key=keyfn, reverse=(order != "asc"))
    return templates.TemplateResponse(
        request, "_search.html",
        {"result": result, "top_n": top_n, "sort_key": sort, "sort_order": order,
         "min_dte": min_dte, "max_dte": max_dte, "target_delta": target_delta},
    )


@app.get("/search/export.csv")
def search_export(
    ticker: str,
    top_n: int = 5,
    min_dte: int = 7,
    max_dte: int = 45,
    target_delta: float = 0.20,
    service: ScreenerService = Depends(get_service),
) -> Response:
    """Download a ticker's top-N puts as CSV."""
    if not (ticker or "").strip():
        raise HTTPException(status_code=422, detail="no ticker")
    result = _search(service, ticker, top_n, min_dte, max_dte, target_delta)
    rows = [c.model_dump(mode="json") for c in result.puts]
    return Response(
        content=_candidates_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{result.symbol}-puts.csv"'},
    )


@app.post("/runs")
def start_run(
    request: Request,
    top_n: int = Form(150),
    fundamental_weight: float = Form(0.5),
    min_dollar_volume: str = Form("25,000,000"),   # accountant-formatted; commas stripped below
    min_yield: str = Form("0.10"),
    min_dte: int = Form(21),
    max_dte: int = Form(35),
    timeout_seconds: str = Form(""),
    min_price: float = Form(20.0),
    max_price: float = Form(200.0),
    target_delta: float = Form(0.20),
    max_abs_delta: float = Form(0.30),
    min_open_interest: int = Form(100),
    max_spread_pct: float = Form(0.10),
    min_iv: str = Form(""),
    runner: JobRunner = Depends(get_job_runner),
):
    try:
        req = ScreenRequest(
            top_n=top_n, fundamental_weight=fundamental_weight,
            min_dollar_volume=float((min_dollar_volume or "").replace(",", "").strip() or 0),
            min_yield=_opt_float(min_yield),
            min_dte=min_dte, max_dte=max_dte, timeout_seconds=_opt_float(timeout_seconds),
            min_price=min_price, max_price=max_price,
            target_delta=target_delta, max_abs_delta=max_abs_delta,
            min_open_interest=min_open_interest, max_spread_pct=max_spread_pct,
            min_iv=_opt_float(min_iv),
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


@app.get("/runs/{job_id}/export.csv")
def export_run(job_id: str, runner: JobRunner = Depends(get_job_runner)) -> Response:
    """Download a run's candidates as a CSV file."""
    job = runner.get(job_id)
    if job is None or job.get("result") is None:
        raise HTTPException(status_code=404, detail="no results to export")
    stamp = (job.get("created_at") or "screen")[:16].replace(":", "").replace("T", "_")
    return Response(
        content=_candidates_csv(job["result"]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="wheel-candidates-{stamp}.csv"'},
    )


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
