"""Request/response DTOs for the web API — a small, user-facing surface.

`ScreenRequest` exposes only the handful of knobs a user should set and maps them onto the
full `ScreenCriteria` (which has ~30 internal fields). This keeps the public contract small
and lets the engine internals stay private.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from wheel_screener.core.models import ScreenCriteria


class ScreenRequest(BaseModel):
    # None = every name that passes fundamentals. There is deliberately no upper bound: a
    # ceiling would be a guess about the size of the field, and the day the universe outgrew it
    # the "all names" default would quietly start capping.
    top_n: int | None = Field(None, ge=1, description="Chains to pull; None = every survivor.")
    fundamental_weight: float = Field(0.5, ge=0.0, le=1.0, description="1=quality, 0=yield.")
    min_score: float | None = Field(None, ge=0.0, le=1.0, description="Blended-score floor.")
    min_dollar_volume: float = Field(
        25_000_000.0, ge=0.0, description="Skip stocks below this avg daily $-volume (0=off)."
    )
    # annualized-yield floor; default 10%. Blank in the form -> None (no floor).
    min_yield: float | None = Field(0.10, ge=0.0, description="Drop candidates below this yield.")
    min_dte: int = Field(14, ge=1, le=400)  # wide enough to always contain a monthly
    max_dte: int = Field(45, ge=1, le=400)
    # options-quality knobs (were engine-internal; now user-adjustable)
    min_price: float = Field(20.0, ge=0.0, description="Underlying price floor.")
    max_price: float = Field(500.0, gt=0.0, description="Underlying price ceiling.")
    # entered as a positive magnitude (0.20); negated to the put's signed delta in to_criteria.
    target_delta: float = Field(0.20, gt=0.0, le=1.0, description="Target put |delta|.")
    max_abs_delta: float = Field(0.30, gt=0.0, le=1.0, description="Widest |delta| kept.")
    # Four liquidity measures, applied together and identically whatever the clock says.
    # Open interest and volume are daily aggregates and mean the same at 3am Sunday as at noon
    # Tuesday; spread and bid size read a live book. Switching rules by market hours would make
    # a screen unreproducible, so all four always apply and a weekend simply reads stricter.
    min_open_interest: int = Field(100, ge=0, description="Contract open-interest floor.")
    min_volume: int = Field(1, ge=0, description="Contracts traded in the session.")
    min_bid_size: int = Field(10, ge=0, description="Contracts bid at the top of book.")
    max_spread_pct: float = Field(
        0.30, gt=0.0, le=1.0, description="Bid-ask gap as a fraction of mid."
    )
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
            min_score=self.min_score,
            min_dollar_volume=self.min_dollar_volume,
            min_annualized_yield=self.min_yield,
            min_dte=self.min_dte,
            max_dte=self.max_dte,
            min_price=self.min_price,
            max_price=self.max_price,
            target_delta=-abs(self.target_delta),  # puts have negative delta
            max_abs_delta=self.max_abs_delta,
            min_open_interest=self.min_open_interest,
            min_volume=self.min_volume,
            min_bid_size=self.min_bid_size,
            max_bid_ask_spread_pct=self.max_spread_pct,
            min_iv=self.min_iv,
        )
