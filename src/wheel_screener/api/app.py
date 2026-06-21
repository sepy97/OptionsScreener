"""FastAPI app (scaffold). Serves the core models as JSON for the web UI / Swift app.

Run (after ``uv sync --extra api``): ``uv run uvicorn wheel_screener.api.app:app --reload``.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from wheel_screener.api.deps import get_service
from wheel_screener.core.models import CandidateResult, ScreenCriteria
from wheel_screener.core.service import ScreenerService

app = FastAPI(title="Wheel Screener API", version="0.1.0")


@app.post("/screen", response_model=list[CandidateResult])
def screen(
    criteria: ScreenCriteria,
    service: ScreenerService = Depends(get_service),
) -> list[CandidateResult]:
    """Run a screen and return ranked candidates (same models the CLI emits)."""
    return service.run_screen(criteria)
