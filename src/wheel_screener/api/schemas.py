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
    # annualized-yield floor; default 10%. Blank in the form -> None (no floor).
    min_yield: float | None = Field(0.10, ge=0.0, description="Drop candidates below this yield.")
    min_dte: int = Field(21, ge=1, le=400)  # ~3 weeks
    max_dte: int = Field(35, ge=1, le=400)  # ~5 weeks
    timeout_seconds: float | None = Field(
        600.0, gt=0.0, description="Wall-clock budget (default 10 min); past it, partial results."
    )
    # options-quality knobs (were engine-internal; now user-adjustable)
    min_price: float = Field(20.0, ge=0.0, description="Underlying price floor.")
    max_price: float = Field(200.0, gt=0.0, description="Underlying price ceiling.")
    # entered as a positive magnitude (0.20); negated to the put's signed delta in to_criteria.
    target_delta: float = Field(0.20, gt=0.0, le=1.0, description="Target put |delta|.")
    max_abs_delta: float = Field(0.30, gt=0.0, le=1.0, description="Widest |delta| kept.")
    min_open_interest: int = Field(100, ge=0, description="Contract open-interest floor.")
    max_spread_pct: float = Field(0.10, gt=0.0, le=1.0, description="Max bid-ask spread fraction.")
    min_iv: float | None = Field(None, ge=0.0, description="Optional IV floor (blank=off).")

    @model_validator(mode="after")
    def _check_ranges(self) -> ScreenRequest:
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be <= max_dte")
        if self.min_price > self.max_price:
            raise ValueError("min_price must be <= max_price")
        if self.target_delta > self.max_abs_delta:
            raise ValueError("target_delta must be <= max_abs_delta")
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
            min_price=self.min_price,
            max_price=self.max_price,
            target_delta=-abs(self.target_delta),  # puts have negative delta
            max_abs_delta=self.max_abs_delta,
            min_open_interest=self.min_open_interest,
            max_bid_ask_spread_pct=self.max_spread_pct,
            min_iv=self.min_iv,
        )
