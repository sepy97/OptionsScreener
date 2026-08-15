# Cash-Secured-Put / Wheel Screener — Build Plan

*Derived from `csp-wheel-screener-data-sources.md`, a provider research pass (June 2026), and the
fundamentals criteria reused from the `~/dev/pythonBot` project. Re-verify provider specifics
against live (authenticated) docs before coding each adapter — Schwab and FMP doc portals are
gated/JS-rendered.*

## 1. Target trade profile (what we select for)

- Short puts at **≤ 0.20 delta** (up to 0.25, never > 0.30), **30–45 DTE**
- **Strong fundamentals** — financially sustainable, "happy to be assigned" names (the gate)
- Pick the **richest contract by annualized yield**; per-contract IV is shown as a column
- **No earnings inside the contract's life** — checked per expiry (report on/before expiration,
  plus a small drift buffer), so a name reporting after the near expiry stays sellable on it.
  Also our stand-in for "abnormal IV = event"
- Liquid options (tight bid/ask, real OI); avoid names within 5% of the high
- Diversified (< 10% per name, ≤ 3 per sector) — *portfolio-level, applied at decision time*

Output = **candidates to verify**, not signals.

## 2. Locked decisions

| Area | Decision | Rationale |
|---|---|---|
| Language | **Python ≥ 3.11**, uv-managed, `src/` layout | quant ecosystem; pydantic v2 / `StrEnum` |
| Order | **Fundamentals first → contract by yield** | select sustainable names, then the richest CSP on them |
| Fundamentals | Reuse **pythonBot `STOCK_CRITERIA`**, **Stalwart** profile | don't reinvent; Stalwart = ownable names |
| Universe/scale | FMP screener → **cheap TTM bulk pre-rank → deep-rate top N** | scales to a nightly screen within rate limits |
| **IV rank** | **Dropped for v1** | yield already encodes IV at fixed delta; earnings-blackout catches event spikes (see §3) |
| Chain provider | **Schwab** `/marketdata/v1/chains` | greeks + IV + OI + bid/ask in one response |
| Fundamentals/price/earnings | **FMP** `/stable/` (same as pythonBot) | screener + ratios/key-metrics/growth/scores/DCF + earnings |
| Provider model | **Ports & adapters** | Schwab/FMP now; marketdata.app/Tradier/Polygon/IBKR drop in |
| Interface | **CLI + CSV now**; FastAPI + Swift later on the *same core* | one service, multiple front doors |

## 3. Why no IV rank for v1

Schwab's API exposes only **raw point-in-time IV** (per-contract `volatility`); IV **rank/percentile**
are thinkorswim *platform* studies, not API fields — so an IV-rank filter would require a
self-maintained ~1-year IV history (SQLite cron) or a paid feed (ORATS/FlashAlpha). We don't need it:

- At a **fixed ~0.20 delta**, premium is a function of IV → higher IV → **higher annualized yield**.
  Ranking the 30–45 DTE candidates by yield ranks them by richness; raw IV adds little ordering
  information. **→ Yield is the contract selector; IV is an informative column.**
- The one thing raw IV can't tell you (is premium *abnormally* high for this name → event/blow-up)
  is dominated by **earnings**, which the **earnings-blackout** already removes.

> This makes the blackout **load-bearing for the ranking, not just for risk**: pre-earnings IV
> inflates the premium, so at a fixed delta the earnings-spanning contracts are systematically the
> highest-yielding ones. Measured on the 2026-07-25 screen, rows whose life spanned a report
> averaged 49% annualized yield vs 21% for clean ones — the top four by yield were all straddles.
> A yield selector with the blackout broken doesn't merely miss the filter; it actively promotes
> the trades it exists to avoid. Hence: fail **closed** (unknown ≠ clean), verify the calendar's
> coverage, and never let a truncated fetch pass for an empty one.

IV rank is therefore deferred to **v2** as an optional timing overlay. If revived: roll-your-own
30-day-ATM-IV SQLite store (Schwab IV is free per chain pull), or ORATS `/datav2/ivrank`
(`ivRank1y`/`ivPct1y`, history to 2007), behind a new `IvRankProvider` port.

## 4. Fundamental rating (reused from pythonBot, simplified)

Every name carries **two** fundamental numbers, computed on the same sanitized metrics:

- **Strength** (the primary rating, `fundamental_score`, shown as *N*/100) — an **absolute**
  financial-strength score against fixed good/satisfactory bars (the pythonBot `STOCK_CRITERIA`),
  graded 1.0 / 0.5 / 0 per metric, averaged per factor over the *present* metrics, and blended by
  the durability tilt. Peer-independent: a name scores the same in a market screen and a
  single-ticker search, and works even outside the $20–200 screened field. `score_strength()`.
- **Peer percentile** (`peer_percentile`, shown beside it) — the **relative** cross-sectional
  rank below, i.e. how the name stacks up against the screened field.

The percentile is also the **funnel** — it picks the top-N names for the expensive chain pull; the
user-facing sort blends **strength × yield**, and a human verifies. So the funnel objective is
**recall (don't drop good names), not precision.** The current absolute strength score is the
*correct* revival of an earlier absolute-threshold bucket scorer that was replaced (it had
sign-inversion bugs — negative PE / negative-equity ratios scored as "good" — and let data
*coverage* drive the rank); strength scores the **sanitized** metrics and averages only present
ones, so neither bug can recur. The percentile methodology, in `core/fundamentals.py` + the
`rate_fundamentals` stage:

1. **Sanitize** — domain guards: drop PE/PEG when EPS≤0, PB & Debt/Equity when equity≤0,
   NetDebt/EBITDA when EBITDA≤0 (net cash retained); compute the DCF gap (price/intrinsic).
2. **Hard gates** (never-trade kills, not averaged): negative equity, loss-maker (EPS≤0 / negative
   margin), negative ROE, excess leverage (NetDebt/EBITDA > `max_leverage`, default 4), insufficient
   coverage (< `min_metrics_present`). Plus the earnings blackout. Lenient by design — gates protect
   recall into the funnel, they don't pick winners. (current_ratio is a Safety *ranking* factor, not
   a hard gate — verified live that <1 over-filters strong names like WMT/CSCO.)
3. **Cross-sectional percentile** of survivors per metric, collapsed into three factors named in
   fundamental-analysis terms: **valuation** {PE, PS, PB, PEG, DCF-gap} (universe-wide so absolute
   cheapness survives), **efficiency** {ROE, ROA, ROIC, net margin}, **sustainability**
   {leverage + liquidity} (both sector-neutral, with a universe-wide fallback for thin sectors).
4. **Weighted composite** with a **durability tilt** (default efficiency 0.45 / sustainability 0.35 /
   valuation 0.20, DCF≈0): the wheel's real risk is being assigned and holding a value trap / blowup,
   so durability beats cheapness. Weights are a config knob (`ScreenCriteria.factor_weights`;
   equal-weight is one flag away).
5. **Median-impute** residual missing metrics to the 0.5 (neutral) percentile, never 0.
6. **Truncate** to `top_n` (optional per-sector cap to bound assignment clustering) → chain pull.

Deterministic, reproducible, sub-second, free (computed on the FMP `*-ttm-bulk` universe before the
rate-limited Schwab stage), and property-tested (monotonicity, sign-trap exclusion, determinism).
Metrics: FMP `ratios-ttm`, `key-metrics-ttm` (+ `*-ttm-bulk`), DCF. Sector data drives the
neutralization; metrics scope is Evaluation/Value, Efficiency/Quality, Liquidity+leverage/Safety
(growth and Altman/Piotroski excluded for v1).

### Verdict on LLM-agent ranking (5-expert panel, unanimous)
- **Design the methodology with agents (one-time): yes** — that debate caught the sign bugs and set
  the weights/gates, then froze into reviewed deterministic code.
- **Rank stocks with agents at runtime (nightly "discuss to consensus"): no** — non-deterministic /
  non-reproducible breaks the "candidates to verify" contract (can't diff/explain/backtest); numeric
  ratio comparison is the LLM's worst task vs a free percentile sort; costly and slow.
- **Endorsed v2 overlay:** a bounded, cached, **single-agent** advisory red-flag annotator over the
  final ~10–20 yield-ranked survivors → qualitative assignment-risk notes into
  `CandidateResult.notes`. Advisory only; never re-orders the numbers.

## 5. Architecture — hexagonal (ports & adapters)

The **core** knows nothing about Schwab, HTTP, the CLI, or FastAPI. CLI and FastAPI both call the
same `ScreenerService.run_screen(...)`; the Swift app talks to FastAPI and deserializes the same
pydantic models.

```
        delivery (thin)                core (framework-free)            adapters (infra)
   ┌──────────────────────┐      ┌───────────────────────────┐    ┌──────────────────────────┐
   │ cli/   (Typer) ──────┼─────►│  ScreenerService           │◄───┤ FMP    → FundamentalsPort │
   │ api/   (FastAPI)─────┼─────►│   run_screen(criteria)     │    │ Schwab → ChainPort        │
   │   ▲                  │      │   ├─ pipeline (5 stages)   │    │ http: cache+retry+        │
   │   └─ Swift app hits  │      │   ├─ fundamentals (pure)   │    │      ratelimit            │
   │      REST/JSON       │      │   ├─ ranking (pure)        │    └──────────────────────────┘
   │                      │      │   └─ pydantic models       │
   └──────────────────────┘      └───────────────────────────┘
        same JSON models ◄────────── ports.py (Protocols) ──────► composition.py wires adapters (DI)
```

### Ports

```python
class FundamentalsProvider(Protocol):   # FMP
    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]: ...
    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]: ...
    def earnings_calendar(self, start: date, end: date) -> dict[str, date]: ...

class ChainProvider(Protocol):          # Schwab; marketdata.app/Tradier/Polygon/IBKR later
    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot: ...
    def capabilities(self) -> ProviderCaps: ...
```

**Normalized `OptionContract`** (clean intersection across all five vendors): `..., delta, ...,
implied_volatility, ...` + `greeks_source` enum + `raw: dict`. Adapters map native IV fields (Schwab
`volatility`, Tradier `mid_iv`, Polygon `iv`); when a source lacks greeks, fill via
`py_vollib_vectorized`.

## 6. Pipeline (5 stages)

| # | Stage | Module | Provider | Does |
|---|---|---|---|---|
| 1 | Universe | `universe.py` | FMP `/company-screener` | price $20–200, mkt-cap, exchange |
| 2 | Fundamental ranking | `rate_fundamentals.py` | FMP bulk + earnings | sanitize → gate → cross-sectional percentile composite → drop names with no clean expiry → top N |
| 3 | Chain pull | `pull_chains.py` | Schwab (survivors only, throttled) | 30–45 DTE put chains |
| 4 | Strike select | `select_strike.py` | — (`nearest_to_delta`) | ~−0.20Δ put per expiry; annualized yield |
| 5 | Rank + output | `rank.py` + export | — | order by yield (IV column) → CSV |

Pure & tested today: `core/fundamentals` (`sanitize_metrics`, `gate_reasons`, `rank_by_fundamentals`),
`rate_fundamentals` (`select_top`, `apply_earnings_blackout`), `ranking.annualized_csp_yield`,
`select_strike.nearest_to_delta`.

### Covered calls (search only)

The **screen is cash-secured-puts only** and stays that way: stages 1–2 exist to answer *"what am
I willing to acquire?"*, and a covered call presupposes shares you already hold — the underlying is
given, not screened. So the put/call knob lives on the **single-ticker search**, whose shape
(you type the symbol) already matches the trade. There is no positions store; nothing is tracked
across a wheel cycle.

Both sides share every sellability gate and the whole chain/earnings path. They differ in exactly
two places:

| | Put (cash-secured) | Call (covered) |
|---|---|---|
| Target delta | −0.20 | +0.20 (`signed_target_delta` re-signs the magnitude) |
| Yield base | the **strike** — the cash set aside | the **share price** — what the 100 pledged shares are worth |
| Breakeven column | strike − premium (what you'd pay) | strike + premium (what you'd receive) |

Anchoring calls to the market price rather than the holder's cost basis is deliberate: basis is
per-holder and unknown here, and using it would make the same contract score differently for two
people reading the same row. The quoted yield is therefore the **static** return (premium kept if
the stock goes nowhere); it excludes the capital gain also realized if the shares are called away.

Spot comes from the chain snapshot when the provider returns it in-band (Schwab), else the
provider's quote endpoint (`AlpacaChainProvider.spot` — Alpaca's option snapshots are option-only),
else the EOD profile price. When all three fail the contract still lists with an empty yield cell:
a missing denominator must not become a fabricated number, nor delete a perfectly sellable row.

The earnings guard is reused as-is, but note the flag means something different on each side. For a
put, a report before expiry is a genuine defect (issue #113) — assignment at a stale price, and the
inflated pre-earnings premium makes those contracts win a yield ranking. For a call the shares are
held through the report either way: selling into it cushions a drop and caps a pop. That is a
trade-off to surface, not one to make for the user, so search FLAGs on both sides rather than
excluding — safe here because search orders by delta-proximity, not yield.

## 7. Provider reference (verified June 2026)

### Schwab Trader / Market Data API
- **Auth:** OAuth2; access token **30 min**; refresh token **hard 7-day** cap → weekly re-login
  (`auth-login`). Register "Market Data Production" app (callback `https://127.0.0.1:8182`);
  approval ~days; no sandbox.
- **Chain** `GET /marketdata/v1/chains`: per contract `delta/gamma/theta/vega/rho`, `volatility`
  (IV %), `openInterest`, `bid/ask`, `strikePrice`, `daysToExpiration`, … nested under
  `callExpDateMap`/`putExpDateMap`. ~120 req/min, **one underlying per request**.
- **Python:** `schwab-py` (OAuth + token-file auto-refresh).

### FMP `/stable/` (universe + fundamentals + earnings — central now)
- `/company-screener` — `priceMoreThan/priceLowerThan, marketCapMoreThan, exchange, isFund, isActivelyTrading`.
- `/ratios-ttm`, `/key-metrics-ttm` (+ `*-ttm-bulk` for the pre-rank) — PE/PS/PB/PEG, ROE/ROA/ROS/ROIC,
  Debt/Equity, `netDebtToEBITDATTM`, current/quick/cash ratios. Plus the DCF endpoint.
- `/earnings-calendar?from=&to=` — blackout. **Silently clips**: ~90-day range clamp AND a
  4000-row cap, both dropping the EARLIEST rows, with no error and no usable signal in the row
  count (issue #113). Always walk the range in small slices and verify coverage.
- `/earnings?symbol=` — one symbol's history + next date; authoritative, used to settle gaps.
- `/quote` `yearHigh` (52-wk) + `/historical-price-eod/dividend-adjusted` for true ATH.
- **Tiers:** Free 250/day (too low for a universe); Starter ~$22/mo 300/min; Premium ~$59/mo 750/min
  30-yr (true ATH). Prefer `*-ttm-bulk`. Client: `fmp-data` (async, rate-limit + cache).

### Alternative chain providers (for the abstraction)
marketdata.app (`/v1/options/chain`, greeks+IV default, credit-based) · Tradier (`greeks=true`, ORATS,
~hourly, 120/min) · Polygon/Massive (`/v3/snapshot/options`, unlimited on paid → scan-friendly) ·
IBKR (multi-call, subscription-gated → execution/targeted only). All map behind `ChainProvider`.

### Extras
CBOE weeklys flag (`available_weeklys/get_csv_download/`) · greeks fallback `py_vollib_vectorized`.

## 8. Stack

`uv` · `pydantic v2` + `pydantic-settings` · `httpx` + `tenacity` + a TTL'd on-disk `DiskCache`
(hishel was dropped — its 1.x API churned and we control our endpoints) · per-provider rate limiter ·
`Typer` · `polars` · `pytest` + `respx` · `ruff`.

## 9. Module tree

```
src/wheel_screener/
  core/
    models.py            # ScreenCriteria, Underlying, FundamentalMetrics, FundamentalRating,
                         # OptionContract, ChainSnapshot, CandidateResult, ChainFilter, ProviderCaps
    fundamentals.py      # sanitize_metrics + gate_reasons + rank_by_fundamentals  [pure, tested]
    ports.py             # FundamentalsProvider, ChainProvider
    pipeline/            # universe · rate_fundamentals · pull_chains · select_strike · rank
    ranking.py           # annualized_csp_yield  [pure, tested]
    service.py           # ScreenerService.run_screen — the one entry CLI+API call
  adapters/  fmp/ · schwab/ · http.py · cache.py
  cli/  api/(FastAPI scaffold)  jobs/(CBOE weeklys)   config.py   composition.py
tests/  pyproject.toml  .env.example  README.md  .gitignore
```

## 10. Roadmap

- **M0 — scaffold** *(done)*: layout, ports, models, fundamentals rating (pure, tested), config, DI,
  CLI shell, tests/CI. **Register the Schwab app now.**
- **M1 — fundamentals CLI**: the ranking engine (sanitize → gate → cross-sectional percentile →
  composite, with `select_top`) is **done + tested**; M1 is the FMP adapter (universe, `*-ttm-bulk`
  pre-rank, deep `fetch_metrics`, earnings) to feed it → ranked stocks to CSV. Useful standalone.
- **M2 — chains + contract + output**: Schwab OAuth/token-file, chain pull (throttled), −0.20Δ
  selection + yield, final ranking → full candidate CSV.
- **M3 — FastAPI** *(optional)*: wrap the same service for the web UI.
- **M4 — Swift app**: consumes the REST API.
- **v2 (optional)** — IV-rank overlay (`IvRankProvider`); CBOE weeklys flag; greeks fallback.

## 11. Risks / open items

- Schwab **7-day refresh-token** re-login (no unattended-forever runs).
- Verify Schwab chain field **names/casing** against a live response post-approval.
- **FMP rate limits** matter more now (fundamentals are per-name): rely on `*-ttm-bulk` for the
  pre-rank; Free 250/day can't run a universe — Starter/Premium.
- "All-time high": `yearHigh` = 52-wk only; true lifetime ATH needs Premium (30-yr). Start with
  52-wk + ~5-yr ATH.
- Confirm exact FMP TTM field spellings (`netDebtToEBITDATTM`, etc.) against one live response — and
  that FMP returns the **sign inputs** the gates need (EPS, total equity, EBITDA), not just ratios.
- Ranking quality: FMP `sector` may be None/coarse (falls back to universe-wide percentile); thin
  sectors and median-imputation of sparse names are mitigated by the coverage gate + universe fallback,
  but watch them once real data flows.
