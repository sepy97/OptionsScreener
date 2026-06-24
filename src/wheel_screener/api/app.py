"""FastAPI app — serves the core ScreenerService as JSON for the web UI (and a future client).

Run (after ``uv sync --extra api``): ``uv run uvicorn wheel_screener.api.app:app --reload``.

PR-A (serving foundation): one service built at startup (lifespan) and shared across requests,
typed provider errors mapped to HTTP status codes, and a /health probe. The full /screen runs
synchronously for now; M3.1 PR-B moves it behind a background job with progress polling.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from wheel_screener.api.deps import get_service, get_settings
from wheel_screener.composition import build_service
from wheel_screener.config import Settings
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import CandidateResult, ScreenCriteria
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
    """Build the service ONCE and warm the local store, so requests are fast + share one
    rate limiter. Warming is best-effort; /health reports actual readiness."""
    settings = Settings()
    service = build_service(settings)
    app.state.settings = settings
    app.state.service = service
    warm = getattr(service.fundamentals, "known_symbols", None)
    if warm is not None:
        try:
            warm()
        except Exception as e:  # noqa: BLE001 - missing store/keys shouldn't crash startup
            logger.warning("startup store warm failed: %s", e)
    yield


app = FastAPI(title="Wheel Screener API", version="0.1.0", lifespan=lifespan)


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


@app.post("/screen", response_model=list[CandidateResult])
def screen(
    criteria: ScreenCriteria,
    service: ScreenerService = Depends(get_service),
) -> list[CandidateResult]:
    """Run a full screen (synchronous for now — PR-B moves this to a background job)."""
    return service.run_screen(criteria, date.today())
