"""Request/response DTOs for the web API — a small, user-facing surface.

`ScreenRequest` exposes only the handful of knobs a user should set and maps them onto the
full `ScreenCriteria` (which has ~30 internal fields). This keeps the public contract small
and lets the engine internals stay private.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from wheel_screener.core.models import ScreenCriteria


class ScreenRequest(BaseModel):
    top_n: int = Field(150, ge=1, le=2000, description="Fundamental survivors to pull chains for.")
    fundamental_weight: float = Field(0.5, ge=0.0, le=1.0, description="1=quality, 0=yield.")
    min_dollar_volume: float = Field(
        25_000_000.0, ge=0.0, description="Skip stocks below this avg daily $-volume (0=off)."
    )
    min_yield: float | None = Field(None, ge=0.0, description="Drop candidates below this yield.")
    min_dte: int = Field(21, ge=1, le=400)  # ~3 weeks
    max_dte: int = Field(35, ge=1, le=400)  # ~5 weeks
    timeout_seconds: float | None = Field(None, gt=0.0, description="Wall-clock budget; partials.")

    @model_validator(mode="after")
    def _check_dte_window(self) -> ScreenRequest:
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be <= max_dte")
        return self

    def to_criteria(self) -> ScreenCriteria:
        return ScreenCriteria(
            top_n=self.top_n,
            prerank_keep=1_000_000,  # local store is free: rank the whole filtered universe
            fundamental_weight=self.fundamental_weight,
            min_dollar_volume=self.min_dollar_volume,
            min_annualized_yield=self.min_yield,
            min_dte=self.min_dte,
            max_dte=self.max_dte,
            max_runtime_seconds=self.timeout_seconds,
        )
