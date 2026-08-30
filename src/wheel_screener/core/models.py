"""Domain models — the typed contracts between pipeline stages and the JSON the CLI,
the future FastAPI layer, and the Swift app all serialize.

Framework-free: no httpx/typer/fastapi imports. Keep it that way.
"""

from __future__ import annotations

from datetime import date, datetime
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


class EarningsStatus(StrEnum):
    """Whether a report lands inside a contract's life — the assignment-risk verdict.

    Three states, not two: ``UNKNOWN`` is NOT ``CLEAN``. Every US equity reports ~4x/year, so a
    missing calendar entry is far more likely a data gap than a genuine absence — conflating the
    two is what let an earnings-spanning contract reach the results table (see issue #113).
    """

    CLEAN = "clean"  # next report lands after expiration — safe to sell
    SPANS = "spans"  # report on/before expiration (+buffer) — the thing we exclude
    UNKNOWN = "unknown"  # no date found; treated as unsafe under the default policy


class EarningsPolicy(StrEnum):
    """What to do with a contract whose life spans a report."""

    EXCLUDE = "exclude"  # drop it (screen default — the point of the blackout)
    FLAG = "flag"  # keep it, but mark it (search default — never silently hide a typed ticker)
    OFF = "off"  # no earnings handling at all (escape hatch / offline)


class ScreenCriteria(BaseModel):
    """Inputs to a screen run. Mirrors the target CSP/wheel trade profile."""

    # universe / price
    min_price: float = 20.0
    max_price: float = 500.0
    min_market_cap: float = 0.0  # off by default — option open interest is the real liquidity gate
    # skip stocks too thin to have tradeable options (price × avg daily volume); the cheap
    # lever against the chain-pull rate limit — fewer wasted calls on names that can't qualify
    min_dollar_volume: float = 25_000_000.0
    exchanges: list[str] = Field(default_factory=lambda: ["nasdaq", "nyse"])
    # Bounds the DEEP fetch, which is free for the local store but ~5 API calls per name
    # for the live FMP source. Always raised to at least top_n, or top_n would be inert.
    prerank_keep: int = 1000
    universe_limit: int = 50  # deep-fetch cap (by market cap) when bulk pre-rank is unavailable
    # fundamentals
    stock_profile: StockProfile = StockProfile.STALWART
    # None = no cap: pull a chain for every name that clears the fundamental gates. It is a
    # STATE rather than a large number on purpose — any numeric stand-in for "all of them" is a
    # guess about how big the field is, and it silently becomes a real cap the day the universe
    # outgrows it. The cut ranks on FUNDAMENTALS, before any yield is measured, so a cap
    # discards high-yield names sight unseen; a measured full field (817 names) took ~4 min,
    # well inside the 10-minute budget. Setting a number is the speed lever, not a quality one.
    top_n: int | None = None
    min_fundamental_score: float | None = None  # 0..1 absolute-strength floor; None = keep top_n
    max_per_sector: int | None = None  # optional concentration cap on the top-N
    max_leverage: float = 4.0  # hard gate: net-debt/EBITDA ceiling
    min_metrics_present: int = 6  # coverage gate: min core metrics required
    factor_weights: dict[str, float] = Field(
        default_factory=lambda: {"valuation": 0.20, "efficiency": 0.45, "sustainability": 0.35}
    )
    # final rank blends fundamental quality + yield as a weighted GEOMETRIC mean, so a name
    # cannot buy its way up the list by being excellent at one half and poor at the other.
    # The weight is a preference dial (which half leads), not a claim about relative worth.
    fundamental_weight: float = 0.5
    # Bars the annualized yield is graded against, 1.0 at `good` and 0.5 at `satisfactory`.
    # These are the same anchors the results table colours by, so score and colour agree.
    yield_good: float = 0.25
    yield_satisfactory: float = 0.15
    # Absolute floor on the blended score (None = off). Only meaningful because the score is
    # absolute: a threshold on a within-run percentile would filter nothing, since the best of
    # any list always ranks near the top of it.
    min_score: float | None = None
    # options target
    target_delta: float = -0.20
    max_abs_delta: float = 0.30
    # Wide enough to always contain a monthly. Monthlies land on the third Friday, so a narrow
    # band drifts in and out of holding one: on 2026-08-29 the old 21-35 sat between the
    # September monthly (20 days) and October's (48) and admitted no monthly at all, which cost
    # a live screen 54% of its qualifying contracts and every name without weeklys. 14-45
    # always spans one, whatever the day of the month.
    min_dte: int = 14
    max_dte: int = 45
    # 0 = strict: results stay within [min_dte, max_dte]. Set >0 to also accept an expiry up to
    # N days outside the window when none lands in-band (opt-in; may return out-of-window results).
    dte_tolerance: int = 0
    # ranking / liquidity gates
    min_annualized_yield: float | None = None  # e.g. 0.15 == 15%/yr floor
    # Liquidity floor, measured as the strictest per-contract gate in the screen: only 12.6%
    # of contracts cleared 100, and dropping it entirely more than doubled the result count.
    # 50 open contracts is still a real market on the far-OTM strikes this strategy sells.
    min_open_interest: int = 50
    # There is deliberately NO spread cap and NO premium floor here. Both were junk-quote
    # guards, and both were measured to be nearly inert: switching them off changed a live
    # screen by 1 and 3 names respectively. What they were guarding against is already covered
    # — a contract needs a real bid (> 0) to be priceable at all, and `min_annualized_yield`
    # rejects a token premium far more directly than a dollar floor can, because it weighs the
    # credit against the collateral actually tied up. The spread stays a RESULTS COLUMN: it is
    # an exit cost the user should see, not a threshold applied behind their back.
    # optional IV floor on the selected put (None = off). Elevated IV = richer premium; when set,
    # a contract must have a known implied vol at or above this fraction (0.40 == 40%) to qualify.
    min_iv: float | None = None
    # wall-clock budget for the chain-pull stage (None = unbounded); past it, partial results.
    # Default 600s so a screen can't run forever and jam the single in-flight slot.
    max_runtime_seconds: float | None = 600.0
    # ── earnings blackout (also our stand-in for "abnormal IV = event") ──────────────────
    # The rule: never hold a short put across a report. Enforced per CONTRACT (earnings on or
    # before that expiry), not per name — so a name reporting after the near expiry stays
    # sellable on it instead of being dropped wholesale.
    earnings_policy: EarningsPolicy = EarningsPolicy.EXCLUDE
    # Published dates drift (FMP marks unconfirmed rows with epsActual=null), and they drift
    # earlier as often as later — so treat anything within N days after expiry as spanning.
    earnings_buffer_days: int = 2
    # Fail closed: exclude a candidate whose earnings date we could not establish. This should
    # never fire on a screen — the sweep covers the whole window a contract can live in and is
    # verified complete, so absence from it *proves* the name doesn't report before expiry. It is
    # the backstop for a calendar that can't vouch for the range.
    exclude_unknown_earnings: bool = True


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
    """Fundamental scores for one name — the absolute strength rating AND the peer percentile."""

    profile: StockProfile
    category_scores: dict[str, float] = Field(default_factory=dict)  # peer-percentile factors 0..1
    composite: float = 0.0  # peer percentile 0..1
    strength: float | None = None  # absolute financial strength 0..1 (peer-independent)
    strength_scores: dict[str, float] = Field(default_factory=dict)  # absolute factor scores 0..1


class AccountType(StrEnum):
    """How the account is funded, which changes which balance fields the broker reports."""

    CASH = "cash"
    MARGIN = "margin"


class AccountBalances(BaseModel):
    """What an account is worth, normalised across brokers.

    ``invested`` is DERIVED as ``total_value - cash`` rather than summed from the broker's asset
    buckets. Summing means enumerating every bucket a broker might populate — Schwab alone splits
    long stock, bonds, mutual funds and long/short options — and silently under-reports the moment
    one is missed. Deriving is robust to whichever buckets a given account happens to use.
    """

    total_value: float | None = None  # what the account is worth, all in
    cash: float | None = None  # the collateral pool, for a wheel
    # Derived (see above), and DIAGNOSTIC rather than display. It was shown on the Portfolio tab
    # and removed: for a wheel account it nets an obligation against an asset — short puts carry a
    # negative market value, so a growing put book made "invested" shrink, which reads as owning
    # less rather than owing more. It stays here because it is what the cash-double-counting check
    # below is computed from.
    invested: float | None = None
    buying_power: float | None = None  # margin accounts; None on a cash account
    equity: float | None = None


# Broker asset classes, in words. Schwab's own strings on the left.
_ASSET_LABELS = {
    "EQUITY": "Stock",
    "COLLECTIVE_INVESTMENT": "ETF",
    "MUTUAL_FUND": "Mutual fund",
    "FIXED_INCOME": "Bond",
    "OPTION": "Option",
    "INDEX": "Index",
    "CURRENCY": "Currency",
}

# Asset classes a covered call can actually be written against. Options exist on stocks and on
# exchange-traded funds; they do not on a bond or an open-ended mutual fund.
_OPTIONABLE_ASSETS = frozenset({"EQUITY", "COLLECTIVE_INVESTMENT"})


class PositionKind(StrEnum):
    """What a held row IS, from a wheel's point of view rather than the broker's.

    A broker reports assetType (EQUITY / OPTION / ...) and a signed quantity. Those two together
    mean different things to this strategy: a SHORT PUT is an obligation with cash committed
    against it, a SHORT CALL is usually covered by shares you hold, and 100+ shares are a
    covered-call candidate. Naming the wheel meaning here keeps that judgement in one place
    instead of re-deriving it in every template.
    """

    SHORT_PUT = "short_put"
    SHORT_CALL = "short_call"
    LONG_OPTION = "long_option"
    SHARES = "shares"
    OTHER = "other"


class Position(BaseModel):
    """One holding, normalised across brokers.

    ``quantity`` is a positive magnitude; direction lives in ``kind``, because a signed quantity
    invites arithmetic that silently flips a short obligation into a long asset.
    """

    symbol: str  # the broker's own string, kept verbatim so a row is always traceable
    underlying: str  # the ticker a human recognises; equals ``symbol`` for shares
    kind: PositionKind
    quantity: float
    # What the broker called it (EQUITY, FIXED_INCOME, ...), kept raw so an asset class this code
    # has never seen still renders as itself instead of vanishing into "other".
    asset_type: str | None = None
    # The broker's prose name. For a bond the `symbol` IS the CUSIP — "912810FB9" tells a human
    # nothing — so a description is the only readable label such a row has.
    description: str | None = None
    # True when the symbol is the CUSIP rather than a ticker, which is how a bond arrives.
    symbol_is_cusip: bool = False
    market_value: float | None = None
    average_price: float | None = None
    unrealized_pl: float | None = None
    # option rows only
    option_type: OptionType | None = None
    strike: float | None = None
    expiration: date | None = None
    dte: int | None = None
    # Cash a short put has spoken for: strike x 100 x contracts. Not reported by the broker as a
    # per-position figure, so it is derived — and it is the number that answers "how much more
    # can I sell?", which is the whole point of the capacity line.
    collateral: float | None = None
    # Spot at render time, when a quote source is available. Only used to say whether a short put
    # is in the money; None simply hides the assignment column rather than guessing.
    underlying_price: float | None = None

    @property
    def label(self) -> str:
        """What to print in the symbol column: a ticker if there is one, else the prose name."""
        if self.symbol_is_cusip and self.description:
            return self.description
        return self.symbol

    @property
    def asset_label(self) -> str:
        """The asset class, in words. Unknown classes fall back to the broker's own string
        rather than a bucket named "other", so a new one is legible the day it appears."""
        raw = (self.asset_type or "").upper()
        return _ASSET_LABELS.get(raw, raw.replace("_", " ").title() or "—")

    @property
    def position_label(self) -> str:
        """"short put", "long call", ... — direction and side in the words a trader uses.

        Direction lives in ``kind`` and side in ``option_type`` because they answer different
        questions: only a SHORT position is an obligation, and only a PUT commits cash. Joining
        them for display keeps that distinction intact in the model.
        """
        if not self.is_option:
            return ""
        side = self.option_type.value if self.option_type else "option"
        direction = "short" if self.kind in (
            PositionKind.SHORT_PUT, PositionKind.SHORT_CALL
        ) else "long"
        return f"{direction} {side}"

    @property
    def is_option(self) -> bool:
        return self.kind in (
            PositionKind.SHORT_PUT, PositionKind.SHORT_CALL, PositionKind.LONG_OPTION
        )

    @property
    def covered_call_lots(self) -> int | None:
        """How many covered calls this lot could support, or None if the asset has no options.

        Bonds and mutual funds are holdings but not writable, so the answer there is "not
        applicable" — which is a different statement from "zero".
        """
        if (self.asset_type or "").upper() not in _OPTIONABLE_ASSETS:
            return None
        return int(self.quantity // 100)

    @property
    def in_the_money(self) -> bool | None:
        """For a short put: is spot below the strike? None when spot is unknown."""
        if self.kind is not PositionKind.SHORT_PUT or self.underlying_price is None:
            return None
        return self.underlying_price < (self.strike or 0.0)


class BrokerageAccount(BaseModel):
    """One account at one broker. ``account_id`` is the broker's opaque handle for API calls and
    is never displayed; ``display_name`` is the masked number a human recognises."""

    broker: str
    account_id: str
    display_name: str
    account_type: AccountType | None = None
    balances: AccountBalances = Field(default_factory=AccountBalances)
    positions: list[Position] = Field(default_factory=list)

    @property
    def committed_collateral(self) -> float:
        """Cash already spoken for by open short puts."""
        return sum(p.collateral or 0.0 for p in self.positions
                   if p.kind is PositionKind.SHORT_PUT)

    @property
    def capacity(self) -> float | None:
        """What is left to sell against: the cash pool minus collateral already committed.

        Cash rather than buying power on purpose — a cash-secured put is secured by CASH, and
        showing margin buying power here would invite selling puts this account cannot cover.
        """
        cash = self.balances.cash
        return None if cash is None else cash - self.committed_collateral


class BrokerLinkStatus(BaseModel):
    """Whether a broker is connected, and until when.

    ``expires_at`` is the credential's own deadline — Schwab refresh tokens last 7 days — and is
    what a session's lifetime is capped to, so one clock governs both.
    """

    broker: str
    # Whether this deployment could connect at all — the broker's app credentials are present.
    # Distinct from `connected`: "nobody has signed in yet" and "there is nothing to sign in to"
    # are different answers, and only the second is the operator's problem rather than the
    # visitor's.
    configured: bool = False
    connected: bool = False
    expires_at: datetime | None = None
    account_fingerprint: str | None = None


class CompanyProfile(BaseModel):
    """Who a ticker actually is — the context a symbol alone doesn't give.

    Deliberately small: enough to answer "what does this company do" beside a contract, not a
    dossier. ``description`` is the provider's own prose and can run to several paragraphs, so
    callers truncate for display rather than storing a shortened copy.
    """

    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    description: str | None = None
    website: str | None = None
    country: str | None = None
    employees: int | None = None


class ReportCell(BaseModel):
    """One metric in one period. ``grade`` is 1.0 great / 0.5 ok / 0.0 poor, or None when the
    provider had no value — blank, which is a different statement from a failing score."""

    value: float | None = None
    grade: float | None = None


class ReportRow(BaseModel):
    """One metric across every period, aligned to ``FundamentalReport.periods``."""

    key: str
    label: str
    description: str = ""
    cells: list[ReportCell] = Field(default_factory=list)


class ReportGroup(BaseModel):
    """A themed block of metrics (valuation, efficiency, growth, liquidity, risk)."""

    key: str
    label: str
    rows: list[ReportRow] = Field(default_factory=list)


class FundamentalReport(BaseModel):
    """A multi-period graded fundamental analysis of ONE company.

    This is the screener's own view of a report, deliberately independent of whichever engine
    produced it: the core never imports the analysis library, the adapter maps into this.

    Distinct from :class:`FundamentalRating`, which is the screener's own single-number
    snapshot used to RANK a universe. This is the long-form, per-metric history of one name.
    """

    symbol: str
    period: str  # "annual" | "quarter"
    periods: list[str] = Field(default_factory=list)  # statement dates, newest first
    groups: list[ReportGroup] = Field(default_factory=list)
    partial: bool = False  # the source had less history than was requested


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
    # the primary rating: absolute financial strength (0..1), independent of the peer set
    fundamental_score: float | None = None
    # secondary: cross-sectional percentile vs the screened field (0..1)
    peer_percentile: float | None = None
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
    fundamental_score: float | None = None  # absolute financial strength 0..1 (primary rating)
    peer_percentile: float | None = None  # percentile vs the screened field 0..1 (secondary)
    annualized_yield: float | None = None
    premium: float | None = None  # conservative credit (the bid)
    collateral: float | None = None
    next_earnings: date | None = None
    # the verdict for THIS contract's expiry (not the name) — carried to the UI/CSV so a
    # blackout miss is visible instead of silent
    earnings_status: EarningsStatus = EarningsStatus.UNKNOWN
    has_weeklys: bool | None = None
    score: float | None = None
    notes: list[str] = Field(default_factory=list)


class ProviderCaps(BaseModel):
    """What a chain provider can do — lets the scan scheduler adapt."""

    name: str
    supports_batch_underlyings: bool = False
    # The provider can fetch MANY underlyings' chains in a handful of requests
    # (:class:`BatchChainProvider`), instead of one request per name.
    supports_batch_chains: bool = False
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
