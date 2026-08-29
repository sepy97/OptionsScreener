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
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from wheel_screener import __version__
from wheel_screener.adapters.schwab.link import SchwabOAuthLink
from wheel_screener.api.deps import get_job_runner, get_service, get_settings
from wheel_screener.api.expiries import DTE_HORIZON_DAYS, expiry_ladder, next_monthly
from wheel_screener.api.jobs import JobBusyError, JobRunner, JobStore
from wheel_screener.api.ratelimit import SlidingWindowLimiter, client_ip, is_expensive
from wheel_screener.api.schemas import ScreenRequest
from wheel_screener.api.sessions import SessionStore
from wheel_screener.composition import build_probes, build_service
from wheel_screener.config import Settings
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import OptionType, ScreenCriteria
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
    # credentialed connections, built once: a probe owns an HTTP client
    app.state.probes = build_probes(settings, service)
    app.state.probe_cache = {}
    # Portfolio: one session store, and the brokers that can authenticate a human.
    app.state.sessions = SessionStore(settings.portfolio.sessions_db_path)
    app.state.links = {SchwabOAuthLink(settings.schwab).broker: SchwabOAuthLink(settings.schwab)}
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



# Everything the Portfolio owns lives under /portfolio, so ONE rule gates it. The rule denies by
# default: only the entry points a visitor needs *before* having a session are exempt, and they are
# matched exactly rather than by prefix. An exempt-by-prefix rule is how the callback — which
# carries the authorization code — ends up unprotected by accident.
_PORTFOLIO_PREFIX = "/portfolio"
_PORTFOLIO_OPEN = re.compile(r"^/portfolio$|^/portfolio/oauth/[a-z0-9_-]+/(connect|callback)$")


def _needs_portfolio_session(path: str) -> bool:
    # `startswith("/portfolio")` alone would also claim /portfoliox and /portfolio-export, so the
    # boundary is explicit: the prefix itself, or something beneath it.
    under = path == _PORTFOLIO_PREFIX or path.startswith(_PORTFOLIO_PREFIX + "/")
    return under and not _PORTFOLIO_OPEN.match(path)


def current_session(request: Request):
    """The live session for this request, or None. Never raises."""
    store = getattr(request.app.state, "sessions", None)
    settings = getattr(request.app.state, "settings", None)
    if store is None or settings is None:
        return None
    return store.get(request.cookies.get(settings.portfolio.cookie_name))


@app.middleware("http")
async def _portfolio_session_gate(request: Request, call_next):
    """No session, no account data."""
    if _needs_portfolio_session(request.url.path) and current_session(request) is None:
        return RedirectResponse("/portfolio", status_code=303)
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
    # put/call: two exports of the same ticker are otherwise near-indistinguishable, and the
    # yield column means different things on each side (strike-based vs share-price-based)
    ("option_type", lambda c: (c.get("contract") or {}).get("option_type")),
    ("strike", lambda c: (c.get("contract") or {}).get("strike")),
    ("underlying_price", lambda c: (c.get("contract") or {}).get("underlying_price")),
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
    # clean / spans / unknown for THIS expiry — so an export can be audited at a glance
    ("earnings_status", lambda c: c.get("earnings_status")),
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


def _grade_class(grade: object) -> str:
    """Tier a report cell's grade onto the same green/amber/red language the tables use.
    An ungraded cell gets no class at all -- blank means "no data", not "poor"."""
    if not isinstance(grade, (int, float)) or isinstance(grade, bool):
        return ""
    if grade >= 1.0:
        return "g-hi"
    if grade >= 0.5:
        return "g-mid"
    return "g-lo"


templates.env.filters["grade_class"] = _grade_class


def _short(text: object, limit: int = 260) -> str:
    """Trim provider prose to a readable blurb, preferring a sentence end over a hard cut."""
    if not isinstance(text, str) or not text.strip():
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if stop >= limit * 0.5:  # a sentence ended late enough to still say something
        return cut[: stop + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(",;:") + "\u2026"


templates.env.filters["short"] = _short


def _money(v: object) -> str:
    """Accountant-style, with an em dash for genuinely unknown — never a misleading $0.00."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


templates.env.filters["money"] = _money


# The pipeline logs one stage line each (captured into job['progress']); we recover the funnel
# counts from those strings so a finished screen can show Universe -> ... -> Candidates, with no
# pipeline instrumentation. %d formatting means no thousands commas, so \d+ matches cleanly.
_FUNNEL_STAGES = (
    ("Universe", re.compile(r"^universe: (\d+) names")),
    # The pre-rank cut used to be invisible here, which is how it went unnoticed that it —
    # not top_n — was deciding how many names ever reached a chain.
    ("Rated", re.compile(r"^prerank: (\d+)/")),
    ("Fundamentals", re.compile(r"^fundamentals: (\d+)/")),
    ("Chains", re.compile(r"^chains: (\d+)/")),
)


def _funnel(job: object) -> list[dict]:
    """Stage counts for a finished screen, parsed from its captured log lines + result length.
    Returns [] when the upstream stage lines aren't present (partial/legacy run) so the funnel is
    simply omitted rather than shown half-empty."""
    if not isinstance(job, dict):
        return []
    counts: dict[str, int] = {}
    for line in job.get("progress") or []:
        for label, pattern in _FUNNEL_STAGES:
            m = pattern.match(str(line))
            if m:
                counts[label] = int(m.group(1))  # last occurrence wins (survives a retry)
    if "Universe" not in counts:
        return []
    result = job.get("result")
    if result is not None:
        counts["Candidates"] = len(result)
    order = [label for label, _ in _FUNNEL_STAGES] + ["Candidates"]
    top = counts["Universe"] or 1
    return [
        {"label": label, "count": counts[label], "pct": round(100 * counts[label] / top, 1)}
        for label in order
        if label in counts
    ]


templates.env.filters["funnel"] = _funnel


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
    # puts: the effective price you'd pay if assigned. calls: the effective price you'd receive
    # if called away — same strike ± the credit, mirrored by side.
    "breakeven": lambda c: (
        c.contract.strike - (c.premium or 0.0)
        if c.contract.option_type is OptionType.PUT
        else c.contract.strike + (c.premium or 0.0)
    ),
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


# A live credential probe costs one upstream call, and /health is polled by the container every
# 30s and in a tight loop during a deploy — so results are cached briefly.
_PROBE_TTL_SECONDS = 60.0


def _probe(request: Request, probe: object) -> str | None:
    """Cached ``check_auth`` for one connection. Returns None when healthy. Never raises."""
    check = getattr(probe.provider, "check_auth", None)
    if check is None:
        return None
    cache = getattr(request.app.state, "probe_cache", None)
    if cache is None:
        cache = request.app.state.probe_cache = {}
    now = time.monotonic()
    hit = cache.get(probe.name)
    if hit is not None and now - hit[0] < _PROBE_TTL_SECONDS:
        return hit[1]
    try:
        detail = check()
    except Exception as e:  # noqa: BLE001 - health must never raise
        detail = f"{probe.name} check failed: {e}"
    cache[probe.name] = (now, detail)
    return detail


@app.get("/health")
def health(
    request: Request,
    service: ScreenerService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Liveness + readiness.

    Readiness ACTUALLY CALLS each credentialed connection rather than checking that a key is
    present: a revoked key is still present, so a presence check reported healthy while every
    chain request returned 401. The HTTP status stays 200 whatever the result — the app is alive
    and its other tabs work — so the container is not restarted and a deploy is not rolled back
    over an expired credential, which redeploying would not fix anyway.
    """
    known = getattr(service.fundamentals, "known_symbols", None)
    try:
        store_loaded = bool(known()) if known is not None else True
    except Exception:  # noqa: BLE001 - health must never raise
        store_loaded = False

    providers = []
    for probe in getattr(request.app.state, "probes", []):
        detail = _probe(request, probe)
        providers.append(
            {"role": probe.role, "name": probe.name, "ready": detail is None, "detail": detail}
        )

    chains = next((p for p in providers if p["role"] == "option chains"), None)
    chain_ready = bool(chains["ready"]) if chains else False
    degraded = [p for p in providers if not p["ready"]]
    return {
        "status": "ok" if (store_loaded and not degraded) else "degraded",
        "store_loaded": store_loaded,
        "chain_source": settings.chain_source,
        "chain_ready": chain_ready,
        "providers": providers,
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
            "expiries": expiry_ladder(date.today(), DTE_HORIZON_DAYS),
            "next_monthly": next_monthly(date.today(), DTE_HORIZON_DAYS),
            "dte_horizon": DTE_HORIZON_DAYS,
            "latest_stale": stale,
            "summary": _results_summary(latest["result"] if latest else None),
        },
    )


@app.get("/search")
def search_page(request: Request):
    """The Search tab: the single-ticker lookup form (results load via POST /search)."""
    return templates.TemplateResponse(request, "search.html", {"active_tab": "search"})


def _side(raw: str) -> OptionType:
    """Parse the put/call knob; anything unrecognized falls back to puts (the default trade)."""
    return OptionType.CALL if (raw or "").strip().lower() in {"call", "calls"} else OptionType.PUT


def _search(service: ScreenerService, ticker: str, top_n: int, min_dte: int, max_dte: int,
            target_delta: float, side: str = "put"):
    # the form sends a magnitude; select_strike re-signs it for the requested side
    criteria = ScreenCriteria(min_dte=min_dte, max_dte=max_dte, target_delta=abs(target_delta))
    return service.search_ticker(
        (ticker or "").strip().upper(), criteria, date.today(), n=top_n, side=_side(side)
    )


@app.post("/search")
def search_route(
    request: Request,
    ticker: str = Form(...),
    top_n: int = Form(5),
    min_dte: int = Form(7),
    max_dte: int = Form(45),
    target_delta: float = Form(0.20),
    side: str = Form("put"),
    sort: str = Form(""),
    order: str = Form("desc"),
    service: ScreenerService = Depends(get_service),
):
    """Single-ticker search — synchronous (one chain pull) top-N contracts near the target delta.

    ``side`` selects cash-secured puts (default) or covered calls."""
    if not (ticker or "").strip():
        return templates.TemplateResponse(
            request, "_error.html", {"message": "enter a ticker symbol"}, status_code=422
        )
    try:
        result = _search(service, ticker, top_n, min_dte, max_dte, target_delta, side)
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    keyfn = _SEARCH_SORT_KEYS.get(sort)
    if keyfn is not None:
        order = "asc" if order.lower() == "asc" else "desc"
        result.contracts.sort(key=keyfn, reverse=(order != "asc"))
    return templates.TemplateResponse(
        request, "_search.html",
        {"result": result, "top_n": top_n, "sort_key": sort, "sort_order": order,
         "min_dte": min_dte, "max_dte": max_dte, "target_delta": target_delta,
         "side": result.side.value,
         "profile": service.company_profile(result.symbol)},
    )


@app.get("/search/export.csv")
def search_export(
    ticker: str,
    top_n: int = 5,
    min_dte: int = 7,
    max_dte: int = 45,
    target_delta: float = 0.20,
    side: str = "put",
    service: ScreenerService = Depends(get_service),
) -> Response:
    """Download a ticker's top-N contracts as CSV."""
    if not (ticker or "").strip():
        raise HTTPException(status_code=422, detail="no ticker")
    result = _search(service, ticker, top_n, min_dte, max_dte, target_delta, side)
    rows = [c.model_dump(mode="json") for c in result.contracts]
    filename = f"{result.symbol}-{result.side.value}s.csv"
    return Response(
        content=_candidates_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Fundamentals tab -----------------------------------------------------------------------
# Long-form, multi-period analysis of ONE company, from the separate `fundcore` engine. That
# engine is a private package this repo does not depend on, so every failure path here has to
# stay explanatory: "not deployed" must read differently from "that ticker is unknown".

_REPORT_PERIODS = ("annual", "quarter")


def _report_period(raw: str) -> str:
    return raw if raw in _REPORT_PERIODS else "annual"


@app.get("/fundamentals")
def fundamentals_page(request: Request, ticker: str = "",
                      settings: Settings = Depends(get_settings)):
    """The Fundamentals tab. A ``?ticker=`` prefill auto-runs the lookup, so a candidate
    elsewhere in the app can deep-link straight to that company's numbers."""
    return templates.TemplateResponse(
        request, "fundamentals.html",
        {
            "active_tab": "fundamentals",
            "ticker": (ticker or "").strip().upper(),
            "years": settings.fundcore.years,
            "max_years": settings.fundcore.max_years,
        },
    )


@app.post("/fundamentals")
def fundamentals_route(
    request: Request,
    ticker: str = Form(...),
    period: str = Form("annual"),
    years: int = Form(10),
    service: ScreenerService = Depends(get_service),
):
    """Build one company's graded report (synchronous -- it is a handful of upstream calls)."""
    if not (ticker or "").strip():
        return templates.TemplateResponse(
            request, "_error.html", {"message": "enter a ticker symbol"}, status_code=422
        )
    try:
        report = service.fundamental_report(
            ticker, period=_report_period(period), years=years
        )
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    return templates.TemplateResponse(
        request, "_fundamentals.html",
        {"report": report, "period": report.period,
         "profile": service.company_profile(report.symbol)},
    )


# --- Portfolio ------------------------------------------------------------------------------


# Balances are two upstream calls against a ~120/min budget, and a page refresh should not spend
# them again. Short enough that the number still reads as "now".
_BALANCES_TTL_SECONDS = 30.0


def _cached_balances(request: Request, service: ScreenerService):
    """Accounts for this request, or the recent ones. Returns ``(accounts, error)`` — never raises,
    because a balance we cannot fetch must degrade to a message inside the page, not a 500."""
    cache = getattr(request.app.state, "balances_cache", None)
    now = time.monotonic()
    if cache is not None and now - cache[0] < _BALANCES_TTL_SECONDS:
        return cache[1], None
    try:
        accounts = service.brokerage_accounts()
    except ProviderError as e:
        return [], str(e)
    request.app.state.balances_cache = (now, accounts)
    return accounts, None


def _link_for(request: Request, broker: str):
    link = (getattr(request.app.state, "links", None) or {}).get(broker)
    if link is None:
        raise HTTPException(status_code=404, detail="unknown broker")
    return link


@app.get("/portfolio")
def portfolio_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    service: ScreenerService = Depends(get_service),
):
    """The Portfolio tab. Four states, each with a real rendering:

    no session -> connect · session + healthy link -> the account ·
    session + expired link -> reconnect · session + no link -> connect.

    Session and link expire independently, so all four are reachable. "Link expired" in particular
    is the normal weekly condition, not an error.
    """
    session = current_session(request)
    links = getattr(request.app.state, "links", {}) or {}
    status = {name: link.status() for name, link in links.items()}
    connected = session is not None and any(s.connected for s in status.values())
    accounts, error = _cached_balances(request, service) if connected else ([], None)
    return templates.TemplateResponse(
        request, "portfolio.html",
        {
            "active_tab": "portfolio",
            "session": session,
            "links": status,
            "connected": connected,
            "accounts": accounts,
            "balances_error": error,
        },
    )


@app.get("/portfolio/oauth/{broker}/connect")
def portfolio_connect(request: Request, broker: str, settings: Settings = Depends(get_settings)):
    """Send the browser to the broker. The `state` is ours, recorded server-side and single-use."""
    link = _link_for(request, broker)
    state = request.app.state.sessions.issue_state(broker, settings.portfolio.state_ttl_seconds)
    try:
        url = link.authorize_url(state)
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    return RedirectResponse(url, status_code=303)


@app.get("/portfolio/oauth/{broker}/callback")
def portfolio_callback(request: Request, broker: str, settings: Settings = Depends(get_settings)):
    """Exchange the code and mint the session.

    The incoming URL carries the authorization code, so it is never logged or echoed back.
    """
    link = _link_for(request, broker)
    store = request.app.state.sessions
    if store.consume_state(request.query_params.get("state")) != broker:
        # unknown, expired, replayed, or issued for a different broker
        return templates.TemplateResponse(
            request, "_error.html",
            {"message": "That sign-in link has expired or was already used. Please try again."},
            status_code=400,
        )
    try:
        status = link.complete(str(request.url), state=request.query_params.get("state") or "")
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})

    # A relink may be a different account, so previous sessions for this broker are ended first —
    # cheaper and more certain than re-checking an account fingerprint on every later request.
    store.revoke_broker(broker)
    request.app.state.balances_cache = None  # a relink may be a different account
    expires = status.expires_at or (datetime.now(tz=UTC) + timedelta(days=1))
    token = store.create(broker, status.account_fingerprint or "unknown", expires)

    response = RedirectResponse("/portfolio", status_code=303)
    response.set_cookie(
        settings.portfolio.cookie_name, token,
        httponly=True, secure=settings.portfolio.cookie_secure, samesite="lax",
        path="/portfolio", expires=expires,
    )
    return response


@app.post("/portfolio/oauth/{broker}/disconnect")
def portfolio_disconnect(request: Request, broker: str, settings: Settings = Depends(get_settings)):
    """End the session AND delete the credential. Either alone would leave a way back in."""
    link = _link_for(request, broker)
    store = request.app.state.sessions
    store.revoke(request.cookies.get(settings.portfolio.cookie_name))
    store.revoke_broker(broker)
    request.app.state.balances_cache = None
    link.revoke()
    response = RedirectResponse("/portfolio", status_code=303)
    response.delete_cookie(settings.portfolio.cookie_name, path="/portfolio")
    return response


@app.post("/runs")
def start_run(
    request: Request,
    top_n: int = Form(2000),
    fundamental_weight: float = Form(0.5),
    min_dollar_volume: str = Form("25,000,000"),   # accountant-formatted; commas stripped below
    min_yield: str = Form("0.10"),
    min_dte: int = Form(14),
    max_dte: int = Form(45),
    min_price: float = Form(20.0),
    max_price: float = Form(500.0),
    target_delta: float = Form(0.20),
    max_abs_delta: float = Form(0.30),
    min_open_interest: int = Form(50),
    min_iv: str = Form(""),
    min_score: str = Form(""),
    runner: JobRunner = Depends(get_job_runner),
):
    try:
        req = ScreenRequest(
            top_n=top_n, fundamental_weight=fundamental_weight,
            min_dollar_volume=float((min_dollar_volume or "").replace(",", "").strip() or 0),
            min_yield=_opt_float(min_yield),
            min_dte=min_dte, max_dte=max_dte,
            min_price=min_price, max_price=max_price,
            target_delta=target_delta, max_abs_delta=max_abs_delta,
            min_open_interest=min_open_interest,
            min_iv=_opt_float(min_iv),
            min_score=_opt_float(min_score),
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
        return templates.TemplateResponse(
            request, "_progress.html",
            {"job": job, "cancelling": runner.is_cancelling(job_id)},
        )
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
    request: Request, job_id: str, symbol: str,
    runner: JobRunner = Depends(get_job_runner),
    service: ScreenerService = Depends(get_service),
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
    return templates.TemplateResponse(
        request, "_candidate.html",
        {"c": cand, "profile": service.company_profile(symbol)},
    )


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
    fresh = runner.get(job_id)
    if fresh is not None and fresh["status"] != "running":
        # it finished between the click and the request — show the outcome, not a dead spinner
        return templates.TemplateResponse(
            request, "_results.html",
            {"job": fresh, "summary": _results_summary(fresh.get("result"))},
        )
    return templates.TemplateResponse(
        request, "_progress.html", {"job": fresh, "cancelling": runner.is_cancelling(job_id)},
    )
