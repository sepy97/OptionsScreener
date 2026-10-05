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
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel, ValidationError

from wheel_screener import __version__
from wheel_screener.adapters.snaptrade.client import SnapTradeClient
from wheel_screener.api.deps import (
    current_session,
    get_job_runner,
    get_portfolio,
    get_service,
    get_settings,
    snaptrade_user,
)
from wheel_screener.api.expiries import DTE_HORIZON_DAYS, expiry_ladder, next_monthly
from wheel_screener.api.jobs import SOURCE_REFRESH, JobBusyError, JobRunner, JobStore
from wheel_screener.api.passkeys import PasskeyError, Passkeys
from wheel_screener.api.ratelimit import SlidingWindowLimiter, client_ip, is_expensive
from wheel_screener.api.schemas import ScreenRequest
from wheel_screener.api.secretbox import SecretBox
from wheel_screener.api.usercache import PerUserCache
from wheel_screener.api.users import UserStore
from wheel_screener.composition import build_probes, build_service
from wheel_screener.config import Settings
from wheel_screener.core.dividends import DividendImpact
from wheel_screener.core.dividends import impact as dividend_impact
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import (
    CandidateResult,
    Dividend,
    OptionType,
    PositionKind,
    ScreenCriteria,
)
from wheel_screener.core.portfolio import PortfolioService
from wheel_screener.core.service import ScreenerService
from wheel_screener.logging_config import redact_access_log

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


_PORTFOLIO_PREFIX = "/portfolio"


def _under_portfolio(path: str) -> bool:
    """Whether a path belongs to the Portfolio feature.

    `startswith("/portfolio")` alone would also claim /portfoliox and /portfolio-export, so the
    boundary is explicit: the prefix itself, or something beneath it.
    """
    return path == _PORTFOLIO_PREFIX or path.startswith(_PORTFOLIO_PREFIX + "/")


def _auth_covers(path: str, scope: str) -> bool:
    """Whether this path needs the password. See ``AuthSettings.scope``.

    Under the ``portfolio`` scope the screener stays open and everything under /portfolio needs
    credentials — the OAuth connect and callback routes included. Those two are exempt from the
    *session* gate (a visitor cannot have a session before signing in), so without this they would
    be the one way in: connecting is what claims the deployment's single broker slot.
    """
    if _path_exempt(path):
        return False
    return _under_portfolio(path) if scope == "portfolio" else True


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
    app.state.auth_scope = settings.auth.scope
    if app.state.auth is None:
        logger.warning("web auth DISABLED (no AUTH__PASSWORD) — set AUTH__REQUIRED=true in prod")
    elif settings.auth.scope == "portfolio":
        logger.info("web auth covers /portfolio only; the screener is public")
    app.state.rate_limiter = (
        SlidingWindowLimiter(settings.rate_limit.per_minute)
        if settings.rate_limit.enabled else None
    )
    app.state.job_runner = JobRunner(service, JobStore(settings.jobs_db_path))
    # credentialed connections, built once: a probe owns an HTTP client
    app.state.probes = build_probes(settings, service)
    app.state.probe_cache = {}
    # Portfolio: who may use it (users, passkeys, sessions), and the brokers they can link.
    app.state.users = UserStore(settings.portfolio.sessions_db_path)
    app.state.passkeys = Passkeys(
        app.state.users, settings.passkeys.rp_id, settings.passkeys.rp_name,
        settings.passkeys.origin,
    )
    # SnapTrade: how people link their own brokerages. Off unless all three keys are set, and
    # then the Portfolio simply has no "Link a brokerage" button. A malformed encryption key
    # fails here, at startup, rather than at the first person's click.
    app.state.snaptrade = app.state.secretbox = None
    if settings.snaptrade.configured:
        app.state.secretbox = SecretBox(settings.snaptrade.secret_key.get_secret_value())
        app.state.snaptrade = SnapTradeClient(
            settings.snaptrade.client_id, settings.snaptrade.consumer_key.get_secret_value(),
            timeout=settings.snaptrade.timeout_seconds,
        )
    # let pipeline INFO logs through so background jobs can capture stage progress
    logging.getLogger("wheel_screener.core").setLevel(logging.INFO)
    # invite tokens and OAuth codes travel in URLs; keep them out of the request log
    redact_access_log()
    warm = getattr(service.fundamentals, "known_symbols", None)
    if warm is not None:
        try:
            warm()
        except Exception as e:  # noqa: BLE001 - missing store/keys shouldn't crash startup
            logger.warning("startup store warm failed: %s", e)
    yield


app = FastAPI(title="Wheel Screener API", version=__version__, lifespan=lifespan)


# Everything the Portfolio owns lives under /portfolio, so ONE rule gates it, and it has no
# exceptions. There used to be three — the tab itself and the broker's connect and callback routes —
# because the broker sign-in WAS the site sign-in, so a visitor had to reach them without a session.
# That is exactly what let any visitor with a Schwab account of their own claim the deployment's
# broker slot. Signing in is a passkey now, on routes outside this prefix, so linking a broker is
# something only a signed-in person can start.
def _needs_portfolio_session(path: str) -> bool:
    return _under_portfolio(path)


def _safe_next(raw: str | None) -> str:
    """Where to go after signing in. Only a path on THIS site: "//evil.example" is a URL to a
    browser, and an open redirect on a sign-in page is a phishing kit's favourite part."""
    if raw and raw.startswith("/") and not raw.startswith(("//", "/\\")):
        return raw
    return "/portfolio"


# Registered BEFORE the password gate on purpose. Starlette runs the last-added middleware
# outermost, so registering this first puts it INSIDE the password check: an unauthenticated
# request is challenged rather than redirected, and never reaches the session store.
@app.middleware("http")
async def _portfolio_session_gate(request: Request, call_next):
    """No session, no account data — and no broker link either."""
    if _needs_portfolio_session(request.url.path) and current_session(request) is None:
        if request.headers.get("HX-Request") == "true":
            # A fragment request (Refresh, a Close? cell) from a page whose session has since
            # ended. A 303 would be followed invisibly by the browser's XHR, and htmx would swap
            # the whole sign-in page into a table cell. HX-Redirect navigates the page instead —
            # and back to the tab, not to a fragment that makes no sense on its own.
            return Response(status_code=401, headers={"HX-Redirect": "/login?next=/portfolio"})
        target = "/portfolio"
        if request.method == "GET":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def _basic_auth_gate(request: Request, call_next):
    """Reject requests without valid Basic-Auth credentials when the gate is enabled."""
    auth = getattr(request.app.state, "auth", None)
    scope = getattr(request.app.state, "auth_scope", "site")
    if auth is not None and _auth_covers(request.url.path, scope):
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
def _viewer(request: Request) -> dict:
    """Who is looking at the page, for every template: the navigation shows the Admin tab to an
    admin and to nobody else. One session lookup per page — a point read on a local file."""
    session = current_session(request)
    return {"viewer": session.user if session is not None else None}


templates = Jinja2Templates(directory=str(_HERE / "templates"), context_processors=[_viewer])
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
    # the first ex-dividend inside this contract's life, the per-share total of all of them,
    # and whether any was estimated from the schedule rather than announced
    ("ex_dividend", lambda c: ((c.get("dividends") or [{}])[0]).get("ex_date")),
    ("dividend", lambda c: _dividend_total(c)),
    ("dividend_estimated", lambda c: (
        any(d.get("estimated") for d in c["dividends"]) if c.get("dividends") else None
    )),
]


def _dividend_total(c: dict) -> float | None:
    divs = c.get("dividends") or []
    return round(sum(d.get("amount") or 0.0 for d in divs), 4) if divs else None


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


def _signed(v: object, places: int = 0) -> str:
    """Compact signed money for a grid cell: +$1,541 / -$229.

    The sign is the first thing read here, so it leads. Jinja's format filter is %-formatting
    and has no thousands flag, which is why this exists rather than a format string.
    """
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "—"
    return f"{'-' if v < 0 else '+'}${abs(v):,.{places}f}"


templates.env.filters["money"] = _money


def _ago(moment) -> str:
    """"12m ago" for a datetime (or an ISO string), in the dashboard's own wording."""
    if isinstance(moment, datetime):
        moment = moment.isoformat()
    return _humanize_age(moment)[0] if moment else ""


templates.env.filters["ago"] = _ago
templates.env.filters["signed"] = _signed


def _dividend_view(c: object) -> DividendImpact | None:
    """The ex-dividend impact for one result row, or None when it lives through none.

    Rows arrive in two shapes — a stored screen result is a plain dict, a live search row is a
    CandidateResult — so both are reduced to the dict form first. Old stored runs predate the
    field and simply have no dividends to show.
    """
    if isinstance(c, BaseModel):
        c = c.model_dump(mode="json")
    if not isinstance(c, dict) or not c.get("dividends"):
        return None
    try:
        divs = [Dividend.model_validate(d) for d in c["dividends"]]
    except ValidationError:
        return None
    k = c.get("contract") or {}
    side = OptionType.CALL if k.get("option_type") == OptionType.CALL.value else OptionType.PUT
    spot = c.get("underlying_price") or k.get("underlying_price")
    return dividend_impact(divs, side, float(k.get("strike") or 0.0), spot, c.get("premium"))


templates.env.filters["dividend_view"] = _dividend_view


def _position_dividend_view(p: object) -> DividendImpact | None:
    """The same impact for a HELD option, priced from the broker's mark."""
    divs = getattr(p, "dividends", None)
    if not divs or getattr(p, "option_type", None) is None or getattr(p, "strike", None) is None:
        return None
    return dividend_impact(divs, p.option_type, p.strike, p.underlying_price, p.mark)


templates.env.filters["position_dividend_view"] = _position_dividend_view


def _squash(text: object) -> Markup:
    """Collapse a macro's output onto one line — for a ``title`` tooltip, where the template's
    line breaks and indentation would otherwise show. Stays Markup: the macro already escaped
    it, and escaping twice would print ``&amp;#39;`` in the tooltip."""
    return Markup(" ".join(str(text).split()))


templates.env.filters["squash"] = _squash


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


def _last_field_size(job: object) -> int | None:
    """How many names cleared fundamentals on the last run.

    The form's "names to check" box defaults to MAX, and MAX is only a meaningful default if you
    can see roughly what it costs. This is the honest source for that: a real number from a real
    run, already parsed out of the funnel, rather than a figure hardcoded into the copy that
    goes stale the first time the universe moves.
    """
    return next((s["count"] for s in _funnel(job) if s["label"] == "Fundamentals"), None)


def _opt_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    return float(raw) if raw else None


def _opt_int(raw: str) -> int | None:
    """Blank (or the word the UI shows for it) means no cap, not zero."""
    raw = (raw or "").strip()
    return None if not raw or raw.upper() == "MAX" else int(raw)


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


def _provider_status(request: Request) -> list[dict]:
    """Each credentialed data connection, actually called (and cached briefly — see _probe)."""
    out = []
    for probe in getattr(request.app.state, "probes", []):
        detail = _probe(request, probe)
        out.append({"role": probe.role, "name": probe.name, "ready": detail is None,
                     "detail": detail})
    return out


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

    providers = _provider_status(request)
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


# The visitor's own latest screen, by job id — so leaving the tab and coming back finds it again.
# The browser already has the id (its progress poll uses it); the cookie only remembers it across
# page loads. Works signed out, since the screener is public.
_MY_SCREEN = "ws_screen"
_MY_SCREEN_SECONDS = 24 * 3600
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def _cookie_secure(request: Request) -> bool:
    """Secure unless the deployment says otherwise (local http development does)."""
    settings = getattr(request.app.state, "settings", None)
    return settings.portfolio.cookie_secure if settings is not None else True


def _my_screen(request: Request, runner: JobRunner) -> dict | None:
    """This visitor's latest screen, if it is still worth showing: running, or finished with
    candidates. A cancel that collected nothing, or a failure, is not something to come back to."""
    job_id = request.cookies.get(_MY_SCREEN) or ""
    if not _JOB_ID.match(job_id):
        return None
    job = runner.get(job_id)
    if job is None:
        return None
    if job["status"] == "running" or (job["status"] in ("done", "cancelled") and job.get("result")):
        return job
    return None


@app.get("/")
def screener_page(request: Request, runner: JobRunner = Depends(get_job_runner)):
    """The Screener tab (home): the run form + the latest precomputed results."""
    # The precomputed screen, not whatever ran last: the Run button is open to every visitor, and
    # one of them trying a 3-day, 0.50-delta screen should not become what everyone else sees as
    # "the latest results". Falls back to any finished run only when no refresh has ever been
    # stored — a fresh install, or local development — so the tab is never needlessly empty.
    latest = (runner.store.latest_done(source=SOURCE_REFRESH)
              or runner.store.latest_done())
    age, stale = _humanize_age(latest["created_at"]) if latest else ("", False)
    # ...and above it, the visitor's OWN latest screen, so starting one and coming back from
    # another tab finds it — still running (reattached, polling) or finished.
    mine = _my_screen(request, runner)
    if mine is not None and latest is not None and mine["job_id"] == latest["job_id"]:
        mine = None  # already the one shown below
    return templates.TemplateResponse(
        request, "screener.html",
        {
            "active_tab": "screener",
            "mine": mine,
            "mine_age": _humanize_age(mine["created_at"])[0] if mine else "",
            "mine_cancelling": runner.is_cancelling(mine["job_id"]) if mine else False,
            "mine_summary": _results_summary(mine.get("result")) if mine else None,
            "defaults": ScreenRequest(), "latest": latest, "latest_age": age,
            "last_field_size": _last_field_size(latest),
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


def _cache_user(request: Request) -> str | None:
    """Whose cache partition this request reads: the signed-in user's.

    The user rather than the session, so the same person on a phone and a laptop shares one cache.
    A relink clears the partition explicitly (see the callback), which is what the session token
    used to do implicitly by changing. Routes never pass this themselves — every caller gets it
    from here, so a route cannot reach another partition by spelling a key.
    """
    session = current_session(request)
    return session.user.id if session is not None else None


def _balances_cache(request: Request) -> PerUserCache:
    cache = getattr(request.app.state, "balances_cache", None)
    if cache is None:
        cache = request.app.state.balances_cache = PerUserCache(_BALANCES_TTL_SECONDS)
    return cache


def _swap_cache(request: Request) -> PerUserCache:
    cache = getattr(request.app.state, "swap_cache", None)
    if cache is None:
        cache = request.app.state.swap_cache = PerUserCache(_SWAP_TTL_SECONDS)
    return cache


def _cached_balances(request: Request, portfolio: PortfolioService):
    """Accounts for this request, or the recent ones. Returns ``(accounts, error)`` — never raises,
    because a balance we cannot fetch must degrade to a message inside the page, not a 500."""
    user = _cache_user(request)
    hit = _balances_cache(request).get(user)
    if hit is not None:
        return hit, None
    try:
        accounts = portfolio.brokerage_accounts()
    except ProviderError as e:
        return [], str(e)
    _balances_cache(request).put(user, None, accounts)
    return accounts, None


# A keep/swap verdict costs a chain pull per open put, so it is cached per POSITION rather than
# per page: reopening the tab inside the window is free, and only a position the cache has never
# seen (or one that has gone stale) spends a request. The Refresh button clears the lot.
_SWAP_TTL_SECONDS = 600


def _position_key(p) -> tuple:
    """Identity of a held contract for caching. Contracts are part of it: the verdict's dollar
    figures scale with size, so a position that grew is a different question."""
    return (p.symbol, p.quantity, p.strike, p.expiration)


def _latest_candidates(runner: JobRunner) -> tuple[list, dict | None]:
    """The latest PRECOMPUTED screen's candidates, for the fallback and the suggestions.

    Only a refresh run counts, with no fallback. The Run button is open to any visitor, so "the
    most recent screen" could be a stranger's odd criteria — and this list supplies the other
    tickers the Close? panel suggests and the median a ticker without a pick of its own is judged
    against. With no refresh stored the column says so ("no screen yet") rather than borrowing one.
    """
    latest = runner.store.latest_done(source=SOURCE_REFRESH)
    rows = (latest or {}).get("result") or []
    out = []
    for row in rows:
        try:
            out.append(CandidateResult.model_validate(row))
        except ValidationError:  # a run stored by an older version: skip that row, not the lot
            continue
    return out, latest


def _stamp_swaps(request: Request, portfolio: PortfolioService, runner: JobRunner,
                 accounts: list, *, force: bool = False) -> dict:
    """Attach a keep/swap verdict to every open short put, and say how fresh the inputs are.

    Never raises: the verdict is an opinion about a position, and the page showing the position
    is worth more than the opinion.
    """
    user = _cache_user(request)
    cache = _swap_cache(request)
    if force:
        cache.clear(user)  # this person's entries only — never everybody's
    # BOTH short sides: puts get the keep-or-swap verdict, calls the cheaper "are these shares
    # still earning" one. Passing only puts here left every call's cell empty on the live page
    # while the service was perfectly able to answer for them.
    short = (PositionKind.SHORT_PUT, PositionKind.SHORT_CALL)
    puts = [p for a in accounts for p in a.positions if p.kind in short]
    todo = []
    for p in puts:
        hit = cache.get(user, _position_key(p))
        if hit is not None:
            p.swap = hit
        else:
            todo.append(p)
    candidates, latest = _latest_candidates(runner)
    if todo:
        try:
            portfolio.swap_reviews(todo, candidates, date.today())
        except Exception as e:  # noqa: BLE001 - a verdict is never worth a dead tab
            logger.warning("swap review failed: %s", e)
        for p in todo:
            if p.swap is not None:
                cache.put(user, _position_key(p), p.swap)
    screen_age, screen_stale = (
        _humanize_age(latest["created_at"]) if latest else ("", True)
    )
    return {
        "screen_age": screen_age,
        "screen_stale": screen_stale,
        "screen_missing": latest is None,
        "checked": bool(puts),
    }


def _opt_date(raw: str):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@app.get("/portfolio/exits")
def portfolio_exits(
    request: Request,
    symbol: str,
    strike: float,
    expiry: str,
    contracts: float = 1.0,
    option_type: str = "put",
    is_short: bool = True,
    collected: str = "",
    opened: str = "",
    roll_strike: str = "",
    call_strike: str = "",
    min_dte: int = 1,
    max_dte: int = 120,
    service: ScreenerService = Depends(get_service),
):
    """Priced ways out of one open short put.

    No session dependency here on purpose: /portfolio/* is gated by the middleware for every
    path except the OAuth entry points, so adding a second check would be a second thing to
    keep in step with it.
    """
    try:
        expiration = date.fromisoformat(expiry)
    except ValueError:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "invalid expiry"}, status_code=422
        )
    if max_dte < min_dte:
        min_dte, max_dte = max_dte, min_dte
    try:
        kind = OptionType.CALL if option_type.lower() == "call" else OptionType.PUT
        rows, after, grid, spot, early = service.exit_options(
            symbol, strike, expiration, contracts, date.today(),
            option_type=kind, is_short=is_short, collected=_opt_float(collected),
            opened_on=_opt_date(opened),
            min_dte=max(1, min_dte), max_dte=min(400, max_dte),
            roll_strike=_opt_float(roll_strike), call_strike=_opt_float(call_strike),
        )
    except ProviderError as e:
        # a quote failure is a message inside the panel, never a dead tab
        return templates.TemplateResponse(
            request, "_exits.html",
            {"symbol": symbol, "strike": strike, "expiry": expiry, "contracts": contracts,
             "rows": [], "after": [], "spot": None, "error": str(e), "min_dte": min_dte,
             "max_dte": max_dte, "roll_strike": roll_strike, "call_strike": call_strike,
             "option_type": option_type, "is_short": is_short, "expiry_label": expiry,
             "grid": None, "collected": collected,
             "opened": opened, "early": None, "div": None},
        )
    # the dividend's own effect (cushion for a put, the scenario for a call out of the money),
    # alongside the early-assignment verdict that already accounts for it
    div = (
        dividend_impact(early.dividends, kind, strike, spot)
        if early is not None and early.dividends else None
    )
    return templates.TemplateResponse(
        request, "_exits.html",
        {"symbol": symbol, "strike": strike, "expiry": expiry, "contracts": contracts,
         "rows": rows, "after": after, "spot": spot, "error": None,
         "min_dte": min_dte, "max_dte": max_dte,
         "roll_strike": roll_strike, "call_strike": call_strike,
         "option_type": option_type, "is_short": is_short, "collected": collected,
         "opened": opened,
         "grid": grid, "today": date.today(),
         "expiry_label": expiration.strftime('%d %b'),
         "early": early, "div": div},
    )


@app.get("/portfolio")
def portfolio_page(
    request: Request,
    portfolio: PortfolioService = Depends(get_portfolio),
    runner: JobRunner = Depends(get_job_runner),
):
    """The Portfolio tab, for a signed-in person (the gate guarantees one): THEIR brokerages,
    linked through SnapTrade, and the accounts behind the ones that are working.

    Accounts are read only when at least one link works; a broken one is listed with a Reconnect
    button instead, since its numbers would be stale. Every link belongs to exactly one person —
    SnapTrade scopes every call by that person's own secret — so there is no deployment-wide
    broker to share, and nothing to keep a second person away from.
    """
    session = current_session(request)
    brokerages, brokerages_error = _snaptrade_connections(request)
    connected = any(not c["disabled"] for c in brokerages)
    accounts, error = _cached_balances(request, portfolio) if connected else ([], None)
    swaps = _stamp_swaps(request, portfolio, runner, accounts) if accounts else {}
    return templates.TemplateResponse(
        request, "portfolio.html",
        {
            "active_tab": "portfolio",
            "session": session,
            "user": session.user,
            "connected": connected,
            "accounts": accounts,
            "balances_error": error,
            "swaps": swaps,
            "snaptrade_on": request.app.state.snaptrade is not None,
            "brokerages": brokerages,
            "brokerages_error": brokerages_error,
        },
    )


# --- Brokerages linked through SnapTrade --------------------------------------------------------
# The person picks their broker and signs in AT the broker, inside SnapTrade's connection portal;
# this app never sees a broker password. Everything here acts as the signed-in person only: their
# SnapTrade identity comes from the session (deps.snaptrade_user), never from the request.


def _snaptrade_connections(request: Request) -> tuple[list[dict], str | None]:
    """This person's SnapTrade connections, simplified for the page, and any error reading them.

    Cached per person with the balances, because a page load should not spend a SnapTrade call
    the balances just spent. Never raises: a list that cannot be read is a message on the page.
    """
    user = snaptrade_user(request)
    if user is None:
        return [], None
    cache = _balances_cache(request)
    who = _cache_user(request)
    hit = cache.get(who, "snaptrade_connections")
    if hit is not None:
        return hit, None
    try:
        raw = request.app.state.snaptrade.connections(user)
    except ProviderError as e:
        return [], str(e)
    rows = [{
        "id": str(c.get("id") or ""),
        "name": str((c.get("brokerage") or {}).get("display_name")
                     or (c.get("brokerage") or {}).get("name") or c.get("name") or "Brokerage"),
        "disabled": bool(c.get("disabled")),
    } for c in raw if c.get("id")]
    cache.put(who, "snaptrade_connections", rows)
    return rows, None


def _forget_accounts(request: Request) -> None:
    """After a link changes, nothing cached from before it may be shown after it."""
    who = _cache_user(request)
    _balances_cache(request).clear(who)
    _swap_cache(request).clear(who)


def _no_snaptrade(request: Request):
    return templates.TemplateResponse(
        request, "_error.html",
        {"message": "Linking a brokerage is not available on this site yet."}, status_code=404,
    )


def _return_url(request: Request) -> str:
    return f"{request.app.state.settings.passkeys.origin.rstrip('/')}/portfolio/brokerages/return"


@app.post("/portfolio/brokerages/link")
def brokerages_link(request: Request):
    """Register this person with SnapTrade on their first link, then send them to the portal."""
    client, box = request.app.state.snaptrade, request.app.state.secretbox
    if client is None:
        return _no_snaptrade(request)
    user = snaptrade_user(request)
    try:
        if user is None:
            me = current_session(request).user
            secret = client.register_user(me.id)  # our internal id — never an email
            request.app.state.users.set_snaptrade_secret(me.id, box.seal(secret))
            user = snaptrade_user(request)
        url = client.portal_url(user, redirect=_return_url(request))
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    return RedirectResponse(url, status_code=303)


@app.get("/portfolio/brokerages/return")
def brokerages_return(request: Request):
    """Where SnapTrade's portal sends the person back. The connection lives at SnapTrade, so there
    is nothing to exchange — only stale numbers to forget."""
    _forget_accounts(request)
    return RedirectResponse("/portfolio", status_code=303)


def _own_connection(request: Request, connection_id: str) -> bool:
    """Whether this connection is one of THIS person's. SnapTrade would refuse another person's
    id anyway, since every call carries the caller's own secret; checking here as well turns that
    into a clear refusal rather than an upstream error, and costs one call on a rare action."""
    user = snaptrade_user(request)
    if user is None or not connection_id:
        return False
    try:
        return any(str(c.get("id")) == connection_id
                   for c in request.app.state.snaptrade.connections(user))
    except ProviderError:
        return False


@app.post("/portfolio/brokerages/reconnect")
def brokerages_reconnect(request: Request, connection_id: str = Form("")):
    """Repair a connection the broker has cut off, in place — same account, same history."""
    if request.app.state.snaptrade is None:
        return _no_snaptrade(request)
    if not _own_connection(request, connection_id):
        return templates.TemplateResponse(
            request, "_error.html", {"message": "There is no such brokerage link."},
            status_code=404)
    try:
        url = request.app.state.snaptrade.portal_url(
            snaptrade_user(request), redirect=_return_url(request), reconnect=connection_id)
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    return RedirectResponse(url, status_code=303)


@app.post("/portfolio/brokerages/remove")
def brokerages_remove(request: Request, connection_id: str = Form("")):
    if request.app.state.snaptrade is None:
        return _no_snaptrade(request)
    if not _own_connection(request, connection_id):
        return templates.TemplateResponse(
            request, "_error.html", {"message": "There is no such brokerage link."},
            status_code=404)
    try:
        request.app.state.snaptrade.remove_connection(snaptrade_user(request), connection_id)
    except ProviderError as e:
        return templates.TemplateResponse(request, "_error.html", {"message": str(e)})
    _forget_accounts(request)
    return RedirectResponse("/portfolio", status_code=303)


# --- Admin -------------------------------------------------------------------------------------
# One tab, for admin accounts only: the people who can sign in, invites, and how the deployment is
# doing. To anyone else — signed in or not — every route here answers exactly what an unknown
# address answers, so the page does not announce that it exists.

# A nightly backup older than this is a backup job that is not running.
_BACKUP_OVERDUE = timedelta(hours=36)


def _require_admin(request: Request):
    """The signed-in admin, or a 404 indistinguishable from an address that does not exist."""
    session = current_session(request)
    if session is None or not session.user.is_admin:
        raise HTTPException(status_code=404, detail="Not Found")
    return session.user


def _admin_people_context(request: Request, created: dict | None = None,
                          error: str | None = None, notice: str | None = None) -> dict:
    users = request.app.state.users
    people = users.people()
    return {
        "active_tab": "admin",
        "me": current_session(request).user,
        "people": people,
        "names": {row["user"].id: row["user"].name for row in people},
        "pending": users.pending_invites(),
        "created": created,
        "error": error,
        "notice": notice,
    }


def _admin_status(request: Request, runner: JobRunner) -> dict:
    """How the deployment is doing, in the terms an operator acts on."""
    from wheel_screener.jobs.backup import backup_time, list_backups

    settings = request.app.state.settings
    screen = runner.store.latest_done(source=SOURCE_REFRESH)
    screen_age, screen_stale = _humanize_age(screen["created_at"]) if screen else ("", True)
    backups = list_backups(settings.backup_dir)
    latest = backups[-1] if backups else None
    taken = backup_time(latest) if latest else None
    return {
        "version": __version__,
        "providers": _provider_status(request),
        "screen": screen,
        "screen_age": screen_age,
        "screen_stale": screen_stale,
        "screen_count": len((screen or {}).get("result") or []),
        "backup": latest,
        "backup_age": _humanize_age(taken.isoformat())[0] if taken else "",
        "backup_overdue": taken is None or datetime.now(tz=UTC) - taken > _BACKUP_OVERDUE,
        "backup_files": sorted(f.name for f in latest.iterdir()) if latest else [],
        "backups_kept": len(backups),
        "snaptrade_on": request.app.state.snaptrade is not None,
    }


@app.get("/admin")
def admin_page(request: Request, runner: JobRunner = Depends(get_job_runner)):
    _require_admin(request)
    return templates.TemplateResponse(
        request, "admin.html",
        {**_admin_people_context(request), "status": _admin_status(request, runner)},
    )


def _people(request: Request, **context):
    return templates.TemplateResponse(
        request, "_admin_people.html", _admin_people_context(request, **context))


@app.post("/admin/invites")
def admin_invite(
    request: Request, name: str = Form(""), admin: str = Form(""), for_user: str = Form(""),
):
    me = _require_admin(request)
    users = request.app.state.users
    settings = request.app.state.settings
    target = users.user(for_user) if for_user else None
    if for_user and target is None:
        return _people(request, error="That account no longer exists.")
    name = (target.name if target else name).strip()
    if not name or len(name) > 60:
        return _people(request, error="Give the invite a name, up to 60 characters.")
    token = users.create_invite(
        name, is_admin=bool(admin) and target is None,
        for_user=target.id if target else None,
        ttl=timedelta(hours=settings.passkeys.invite_hours), created_by=me.id,
    )
    return _people(request, created={
        "name": name,
        "link": f"{settings.passkeys.origin.rstrip('/')}/invite/{token}",
        "for_user": target is not None,
        "admin": bool(admin) and target is None,
        "hours": settings.passkeys.invite_hours,
    })


@app.post("/admin/invites/cancel")
def admin_invite_cancel(request: Request, ref: str = Form("")):
    _require_admin(request)
    request.app.state.users.cancel_invite(ref)
    return _people(request)


@app.post("/admin/people/signout")
def admin_sign_out(request: Request, user_id: str = Form("")):
    """End every session someone holds — a lost phone, say. Their account and passkeys stay."""
    me = _require_admin(request)
    target = request.app.state.users.user(user_id)
    if target is None:
        return _people(request, error="That account no longer exists.")
    request.app.state.users.end_sessions_for(target.id)
    if target.id == me.id:
        # including this one: the next page load will ask for a passkey
        return Response(status_code=204, headers={"HX-Redirect": "/login?next=/admin"})
    return _people(request, notice=f"{target.name} is signed out everywhere.")


@app.post("/admin/people/remove")
def admin_remove(request: Request, user_id: str = Form("")):
    """Take someone's access away: sessions, passkeys, and their brokerage links at SnapTrade —
    which frees those links from the plan's count. SnapTrade goes first, and if it cannot be
    reached nothing is removed, so the admin can simply try again rather than leave links behind
    that this site can no longer reach to delete."""
    me = _require_admin(request)
    users = request.app.state.users
    target = users.user(user_id)
    if target is None:
        return _people(request, error="That account no longer exists.")
    if target.id == me.id:
        return _people(request, error="You can't remove your own access.")
    snaptrade = request.app.state.snaptrade
    note = ""
    if users.snaptrade_secret(target.id) is not None:
        if snaptrade is None:
            note = (" Their brokerage links are still held at SnapTrade, since this site no longer"
                    " has keys to reach it; remove them from the SnapTrade dashboard.")
        else:
            try:
                snaptrade.delete_user(target.id)
            except ProviderError as e:
                return _people(request, error=(
                    f"Could not remove {target.name}'s brokerage links at SnapTrade ({e}). "
                    "Nothing was changed — try again."))
    users.remove_user(target.id)
    _balances_cache(request).clear(target.id)
    _swap_cache(request).clear(target.id)
    return _people(request, notice=f"{target.name} no longer has access.{note}")


@app.get("/portfolio/invites")
def old_invites_page():
    """Where the Invites page used to be, for bookmarks."""
    return RedirectResponse("/admin", status_code=303)


# --- Signing in: passkeys --------------------------------------------------------------------
# The pages are plain; the ceremony runs in /static/passkey.js, which fetches options from one
# endpoint, hands them to the browser's passkey prompt, and posts the result to the other. Every
# one of these routes is outside /portfolio on purpose — they are how a visitor gets a session.


def _start_session(request: Request, user, response: Response) -> None:
    settings = request.app.state.settings
    token, expires = request.app.state.users.create_session(
        user.id, timedelta(days=settings.portfolio.session_days)
    )
    response.set_cookie(
        settings.portfolio.cookie_name, token, expires=expires, path="/",
        httponly=True, secure=settings.portfolio.cookie_secure, samesite="lax",
    )


def _refusal(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@app.get("/login")
def login_page(request: Request, next: str = "/portfolio"):  # noqa: A002 - the query param's name
    target = _safe_next(next)
    if current_session(request) is not None:
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"active_tab": "portfolio", "next": target}
    )


@app.get("/invite/{token}")
def invite_page(request: Request, token: str):
    """Where an invite link lands. Showing the page does not use the invite up — only a passkey
    that is actually registered does, so a link opened on the wrong device still works later."""
    invite = request.app.state.users.invite(token)
    return templates.TemplateResponse(
        request, "invite.html",
        {"active_tab": "portfolio", "invite": invite, "token": token},
        status_code=200 if invite is not None else 404,
    )


@app.post("/auth/register/options")
def register_options(request: Request, payload: dict = Body(...)):
    try:
        options = request.app.state.passkeys.registration_options(str(payload.get("invite", "")))
    except PasskeyError as e:
        return _refusal(str(e))
    return Response(options, media_type="application/json")


@app.post("/auth/register/verify")
def register_verify(request: Request, payload: dict = Body(...)):
    credential = payload.get("credential")
    if not isinstance(credential, dict):
        return _refusal("The passkey could not be verified. Please try again.")
    try:
        user = request.app.state.passkeys.register(credential)
    except PasskeyError as e:
        return _refusal(str(e))
    response = JSONResponse({"redirect": "/portfolio"})
    _start_session(request, user, response)
    return response


@app.post("/auth/login/options")
def login_options(request: Request):
    return Response(request.app.state.passkeys.login_options(), media_type="application/json")


@app.post("/auth/login/verify")
def login_verify(request: Request, payload: dict = Body(...)):
    credential = payload.get("credential")
    if not isinstance(credential, dict):
        return _refusal("That passkey was not recognised. Please try again.")
    try:
        user = request.app.state.passkeys.login(credential)
    except PasskeyError as e:
        return _refusal(str(e))
    response = JSONResponse({"redirect": _safe_next(payload.get("next"))})
    _start_session(request, user, response)
    return response


@app.post("/auth/logout")
def logout(request: Request):
    settings = request.app.state.settings
    request.app.state.users.end_session(request.cookies.get(settings.portfolio.cookie_name))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(settings.portfolio.cookie_name, path="/")
    return response


@app.post("/portfolio/swaps/refresh")
def portfolio_swaps_refresh(
    request: Request,
    portfolio: PortfolioService = Depends(get_portfolio),
    runner: JobRunner = Depends(get_job_runner),
):
    """Re-price every open put against live quotes and the latest screen.

    Balances are re-read too: the verdict's dollars are per contract, so a position that changed
    since the cached read would be judged at the wrong size.
    """
    _balances_cache(request).clear(_cache_user(request))
    accounts, error = _cached_balances(request, portfolio)
    swaps = _stamp_swaps(request, portfolio, runner, accounts, force=True)
    return templates.TemplateResponse(
        request, "_positions.html",
        {"accounts": accounts, "balances_error": error, "swaps": swaps},
    )


@app.get("/portfolio/swap")
def portfolio_swap_detail(
    request: Request,
    position: str,
    portfolio: PortfolioService = Depends(get_portfolio),
    runner: JobRunner = Depends(get_job_runner),
):
    """The reasoning behind one position's Close? verdict: the numbers, the rules, the
    alternatives. Served from the cache the column was rendered from, so the panel can never
    disagree with the cell that opened it.

    ``position`` is the contract's own symbol, and is deliberately NOT called ``symbol``: the
    table row this link lives in carries hx-vals with a ``symbol`` of its own (the underlying,
    for the ways-out panel), htmx merges an ancestor's hx-vals into the child's request, and the
    duplicate that arrives last is the one Starlette hands over.
    """
    accounts, _error = _cached_balances(request, portfolio)
    swaps = _stamp_swaps(request, portfolio, runner, accounts)
    held = next(
        (p for a in accounts for p in a.positions
         if p.kind in (PositionKind.SHORT_PUT, PositionKind.SHORT_CALL)
         and p.symbol == position), None,
    )
    if held is None or held.swap is None:
        return templates.TemplateResponse(
            request, "_error.html", {"message": "unknown position"}, status_code=404
        )
    return templates.TemplateResponse(
        request, "_swap.html", {"p": held, "r": held.swap, "swaps": swaps},
    )


@app.post("/runs")
def start_run(
    request: Request,
    top_n: str = Form(""),  # blank = MAX (no cap)
    fundamental_weight: float = Form(0.5),
    include_etfs: bool = Form(True),
    min_dollar_volume: str = Form("25,000,000"),   # accountant-formatted; commas stripped below
    min_yield: str = Form("0.10"),
    min_dte: int = Form(14),
    max_dte: int = Form(45),
    min_price: float = Form(20.0),
    max_price: float = Form(500.0),
    target_delta: float = Form(0.20),
    max_abs_delta: float = Form(0.30),
    min_open_interest: int = Form(100),
    min_volume: int = Form(1),
    min_bid_size: int = Form(10),
    max_spread_pct: float = Form(0.30),
    min_iv: str = Form(""),
    min_score: str = Form(""),
    runner: JobRunner = Depends(get_job_runner),
):
    try:
        req = ScreenRequest(
            top_n=_opt_int(top_n), fundamental_weight=fundamental_weight,
            include_etfs=include_etfs,
            min_dollar_volume=float((min_dollar_volume or "").replace(",", "").strip() or 0),
            min_yield=_opt_float(min_yield),
            min_dte=min_dte, max_dte=max_dte,
            min_price=min_price, max_price=max_price,
            target_delta=target_delta, max_abs_delta=max_abs_delta,
            min_open_interest=min_open_interest, min_volume=min_volume,
            min_bid_size=min_bid_size, max_spread_pct=max_spread_pct,
            min_iv=_opt_float(min_iv),
            min_score=_opt_float(min_score),
        )
    except (ValidationError, ValueError) as e:
        # 200, not 422: this answers an htmx form, and htmx shows only successful responses — a
        # 4xx here left the page silently unchanged. The JSON API (/screen) keeps its codes.
        return templates.TemplateResponse(
            request, "_error.html", {"message": f"invalid input: {e}"}
        )
    try:
        job_id = runner.start(req.to_criteria())
    except JobBusyError as e:
        # One screen at a time, site-wide. Was a 409 that htmx dropped, so a second Run — the
        # natural thing to press after a Cancel — did nothing at all.
        mine = _my_screen(request, runner)
        if mine is not None and mine["job_id"] == e.active_id:
            # their own screen, still running or still stopping after a cancel: show it
            return templates.TemplateResponse(
                request, "_progress.html",
                {"job": mine, "cancelling": runner.is_cancelling(e.active_id)},
            )
        return templates.TemplateResponse(request, "_error.html", {"message": (
            "Another screen is running right now — one runs at a time, and it usually takes a "
            "few minutes. Try again shortly.")})
    job = {"job_id": job_id, "status": "running", "progress": []}
    response = templates.TemplateResponse(request, "_progress.html", {"job": job})
    response.set_cookie(
        _MY_SCREEN, job_id, max_age=_MY_SCREEN_SECONDS, path="/", httponly=True,
        samesite="lax", secure=_cookie_secure(request),
    )
    return response


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
    # WHEN the screen ran, so "spot when screened" names a moment rather than gesturing at
    # one. A stored run is read hours later and the stock moves under it.
    age, _stale = _humanize_age(job.get("created_at") or "")
    return templates.TemplateResponse(
        request, "_candidate.html",
        {"c": cand, "profile": service.company_profile(symbol), "run_age": age},
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
