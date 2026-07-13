"""Domain models — the typed contracts between pipeline stages and the JSON the CLI,
the future FastAPI layer, and the Swift app all serialize.

Framework-free: no httpx/typer/fastapi imports. Keep it that way.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class StockProfile(StrEnum):
    """Fundamental rating profile (thresholds reused from pythonBot STOCK_CRITERIA)."""

    STALWART = "stalwart"  # stable, ownable — default for the wheel
    GROWTH = "growth"


class GreeksSource(StrEnum):
    """How a contract's greeks/IV were obtained — lets ranking be freshness-aware."""

    VENDOR_DEFAULT = "vendor_default"  # returned in-band by the chain provider
    REQUIRES_FLAG = "requires_flag"  # only on request (e.g. Tradier greeks=true)
    COMPUTED = "computed"  # we computed them locally (py_vollib fallback)
    UNAVAILABLE = "unavailable"


class ScreenCriteria(BaseModel):
    """Inputs to a screen run. Mirrors the target CSP/wheel trade profile."""

    # universe / price
    min_price: float = 20.0
    max_price: float = 200.0
    min_market_cap: float = 0.0  # off by default — option open interest is the real liquidity gate
    # skip stocks too thin to have tradeable options (price × avg daily volume); the cheap
    # lever against the chain-pull rate limit — fewer wasted calls on names that can't qualify
    min_dollar_volume: float = 25_000_000.0
    exchanges: list[str] = Field(default_factory=lambda: ["nasdaq", "nyse"])
    prerank_keep: int = 150  # names kept after the cheap bulk pre-rank, for the deep fetch
    universe_limit: int = 50  # deep-fetch cap (by market cap) when bulk pre-rank is unavailable
    # fundamentals
    stock_profile: StockProfile = StockProfile.STALWART
    top_n: int = 50  # keep the top N (by cross-sectional rank) for the chain pull
    min_fundamental_score: float | None = None  # 0..1 composite floor; None = keep top_n
    max_per_sector: int | None = None  # optional concentration cap on the top-N
    max_leverage: float = 4.0  # hard gate: net-debt/EBITDA ceiling
    min_metrics_present: int = 6  # coverage gate: min core metrics required
    factor_weights: dict[str, float] = Field(
        default_factory=lambda: {"valuation": 0.20, "efficiency": 0.45, "sustainability": 0.35}
    )
    # final rank blends fundamental quality + yield (1 = all fundamentals, 0 = all yield)
    fundamental_weight: float = 0.5
    # options target
    target_delta: float = -0.20
    max_abs_delta: float = 0.30
    min_dte: int = 21  # ~3 weeks
    max_dte: int = 35  # ~5 weeks
    # 0 = strict: results stay within [min_dte, max_dte]. Set >0 to also accept an expiry up to
    # N days outside the window when none lands in-band (opt-in; may return out-of-window results).
    dte_tolerance: int = 0
    # ranking / liquidity gates
    min_annualized_yield: float | None = None  # e.g. 0.15 == 15%/yr floor
    min_open_interest: int = 100
    max_bid_ask_spread_pct: float = 0.10
    # wall-clock budget for the chain-pull stage (None = unbounded); past it, partial results
    max_runtime_seconds: float | None = None
    # earnings blackout (also our stand-in for "abnormal IV = event")
    exclude_earnings_in_window: bool = True


class FundamentalMetrics(BaseModel):
    """Raw fundamental inputs to the rating (TTM-level).

    Scope is evaluation + efficiency + liquidity (growth/risk excluded for v1).
    Sourced from FMP (ratios-ttm / key-metrics-ttm / DCF), the provider pythonBot uses.
    """

    # evaluation
    pe: float | None = None
    ps: float | None = None
    pb: float | None = None
    peg: float | None = None
    dcf: float | None = None  # intrinsic value per share
    price: float | None = None
    # efficiency
    roe: float | None = None
    roa: float | None = None
    ros: float | None = None  # net profit margin
    roi: float | None = None  # roic
    debt_to_equity: float | None = None
    net_debt_to_ebitda: float | None = None
    fcf_yield: float | None = None  # TTM free-cash-flow yield (gate requires > 0)
    # liquidity
    current_ratio: float | None = None
    quick_ratio: float | None = None
    cash_ratio: float | None = None
    # sign inputs for sanitize/gates (not scored directly)
    eps: float | None = None
    total_equity: float | None = None
    ebitda: float | None = None


class FundamentalRating(BaseModel):
    """Composite fundamental score for one name."""

    profile: StockProfile
    category_scores: dict[str, float] = Field(default_factory=dict)  # per-category 0..1
    composite: float = 0.0  # 0..1


class Underlying(BaseModel):
    """A stock in the universe / surviving the fundamental rating."""

    symbol: str
    name: str | None = None
    price: float | None = None
    market_cap: float | None = None
    sector: str | None = None
    # fundamentals
    metrics: FundamentalMetrics | None = None
    rating: FundamentalRating | None = None
    fundamental_score: float | None = None
    # calendar
    next_earnings: date | None = None
    has_weeklys: bool | None = None


class OptionContract(BaseModel):
    """Provider-agnostic contract — the clean intersection across Schwab,
    marketdata.app, Tradier, Polygon/Massive, and IBKR."""

    underlying_symbol: str
    option_symbol: str
    option_type: OptionType
    expiration: date
    strike: float
    dte: int

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mid: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    open_interest: int | None = None

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_volatility: float | None = None  # per-contract IV (shown as a column)

    underlying_price: float | None = None
    greeks_source: GreeksSource = GreeksSource.VENDOR_DEFAULT
    # vendor-specific extras kept for internal/debug use; excluded from the serialized contract
    raw: dict = Field(default_factory=dict, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float | None:
        """Bid/ask spread as a fraction of the mid, or None if unpriced.

        A real liquidity signal (also a selection gate), so it's part of the serialized output."""
        if self.bid and self.ask and (self.ask + self.bid) > 0:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2)
        return None


class ChainSnapshot(BaseModel):
    underlying_symbol: str
    underlying_price: float | None = None
    contracts: list[OptionContract] = Field(default_factory=list)


class CandidateResult(BaseModel):
    """One ranked row of screener output."""

    symbol: str
    contract: OptionContract
    fundamental_score: float | None = None
    annualized_yield: float | None = None
    premium: float | None = None  # conservative credit (the bid)
    collateral: float | None = None
    next_earnings: date | None = None
    has_weeklys: bool | None = None
    score: float | None = None
    notes: list[str] = Field(default_factory=list)


class ProviderCaps(BaseModel):
    """What a chain provider can do — lets the scan scheduler adapt."""

    name: str
    supports_batch_underlyings: bool = False
    max_concurrency: int = 1
    server_side_filters: list[str] = Field(default_factory=list)
    realtime: bool = False


class ChainFilter(BaseModel):
    """Normalized chain-pull filter; adapters translate or fall back to client-side."""

    option_type: OptionType = OptionType.PUT
    min_dte: int | None = None
    max_dte: int | None = None
    min_open_interest: int | None = None
    target_delta: float | None = None
    strike_count: int | None = None
