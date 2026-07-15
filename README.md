# wheel-screener

A command-line **cash-secured-put / wheel options screener**. It finds financially-sound common
stocks you'd be happy to own, then surfaces the cash-secured puts on them actually worth selling —
liquid, ~**−0.20 delta**, ~**30–45 DTE**, ranked by a blend of fundamental quality and annualized
yield, with earnings-risk names filtered out. The output is a **shortlist to verify, not trade
signals**.

> Full design, data-source facts, and roadmap: **[docs/PLAN.md](docs/PLAN.md)**.

## How it works (in plain terms)

It's a **manual command-line tool** — you run it when you want a screen; **nothing runs in the
background**. One command, `candidates`, does the whole thing and writes a CSV.

Each run is a pipeline:

1. **Universe** — read a local store of whole-market fundamentals (downloaded once) and keep common
   stocks priced $20–200 on NASDAQ/NYSE.
2. **Filter out the bad** — drop names that are insolvent, unprofitable, over-leveraged, or
   cash-flow-negative (hard gates), plus anything reporting earnings inside the trade window.
3. **Rank the survivors** — a cross-sectional score across valuation / efficiency / sustainability.
4. **Pull option chains** — for the survivors, fetch live put chains from Schwab (concurrent,
   rate-limited, cached, with retry on transient hiccups).
5. **Pick the put** — the ~−0.20Δ put nearest 30–45 DTE that's genuinely liquid (open interest,
   tight spread, a real sellable bid).
6. **Rank the shortlist** — by a configurable blend of fundamental quality and (conservative,
   bid-based) annualized yield → CSV.

**Two data sources:**

- **FMP** (Financial Modeling Prep) — fundamentals + the earnings calendar. The whole market is
  downloaded **once** into a local store, so screens are instant and quota-free; small refresh jobs
  keep it current on a cheap key.
- **Schwab** — live option chains with greeks/IV (OAuth; the only piece that needs a weekly
  re-login). The default chain source.
- **Alpaca** *(optional alternative)* — option chains with greeks/IV at ~1000 req/min (vs Schwab's
  ~120) and simple key/secret auth (no weekly OAuth). Opt in with `CHAIN_SOURCE=alpaca` +
  `ALPACA__API_KEY` / `ALPACA__API_SECRET`; `ALPACA__FEED=indicative` (free) or `opra` (paid,
  real-time). See [Using Alpaca](#using-alpaca-for-chains).

**Nothing is scheduled for you.** You run the screen on demand and run the small data-refresh
commands periodically — by hand, or wire them into your own `cron` (examples below).

## What you need

- [uv](https://docs.astral.sh/uv/) and Python 3.11+ (uv fetches Python for you).
- An **FMP API key** (Starter tier covers the refresh jobs + earnings) and, for the one-time
  whole-market download, a separate **bulk key** (Ultimate tier — only needed briefly).
- A **Schwab "Market Data Production" app** (OAuth) — register at https://developer.schwab.com
  **early**; approval takes a few days.

## One-time setup

```bash
# 1. install (creates the venv + installs everything; uv fetches Python 3.11)
uv sync

# 2. configure secrets
cp .env.example .env          # then fill in the keys (see below)

# 3. load the local fundamentals store (one-time, ~2 GB; uses the bulk FMP key)
FMP_BULK_API_KEY=your-bulk-key python3 tools/fmp_bulk_import.py \
    --out data/fundamentals --years 2015-2025

# 4. build the local earnings calendar (for the earnings blackout)
uv run wheel-screener refresh-earnings

# 5. log in to Schwab (opens a browser; the token lasts 7 days)
uv run wheel-screener auth-login
```

`.env` keys: `FMP__API_KEY` (fundamentals + earnings) and `SCHWAB__CLIENT_ID` /
`SCHWAB__CLIENT_SECRET` (chains). `FMP_BULK_API_KEY` is used only by the importer (env var,
`--api-key`, or `.env`). All of `.env`, `.secrets/`, and `data/` are gitignored.

### Using Alpaca for chains

Alpaca is an optional drop-in alternative to Schwab for the option-chain stage — higher rate limit
(~1000 vs ~120 req/min) and key/secret auth (no weekly OAuth login). It implements the same
`ChainProvider` port, so nothing else changes; the composition root picks it from config:

```dotenv
CHAIN_SOURCE=alpaca
ALPACA__API_KEY=your-key
ALPACA__API_SECRET=your-secret
ALPACA__FEED=indicative        # free (delayed/modified quotes); use "opra" for paid real-time
# ALPACA__TRADING_BASE_URL=https://paper-api.alpaca.markets   # ONLY if using paper-account keys
```

It merges two Alpaca endpoints per underlying — the data-API snapshot (quotes/greeks/IV) and the
trading-API contracts reference (open interest). The contracts host defaults to the **live** API
(`api.alpaca.markets`); if your keys are **paper** keys, override `ALPACA__TRADING_BASE_URL` to
`https://paper-api.alpaca.markets` (it must match the environment of your key/secret). The default
remains `CHAIN_SOURCE=schwab`.

## Running a screen

```bash
uv run wheel-screener candidates --top-n 250 --output candidates.csv
```

Runs the full pipeline live and writes a ranked CSV. Handy flags:

- `--top-n N` — how many fundamental survivors to pull chains for (more = broader, slower).
- `--fundamental-weight 0..1` — final-rank blend (1 = pure fundamentals, 0 = pure yield; default 0.5).
- `--min-yield`, `--min-market-cap` — optional floors.
- `--timeout SECONDS` — wall-clock budget for the chain pull (returns partial results if exceeded).

**Output columns:**

| Column | Meaning |
|---|---|
| `rank` | position in the shortlist (by blended `score`) |
| `symbol` `strike` `expiration` `dte` | the put to sell |
| `delta` | ~−0.20 target |
| `iv` | per-contract implied volatility |
| `bid` | **credited premium** — the conservative, fillable price |
| `mid` | `(bid+ask)/2`, reference only (not credited) |
| `open_interest` | option liquidity |
| `annualized_yield` | `(bid/strike) × (365/dte)` — computed off the bid |
| `collateral` | `strike × 100` (the cash you set aside) |
| `fundamental_score` | 0–1 cross-sectional fundamental composite |
| `score` | final blended rank score |

> `screen` (fundamentals-only, no chains, no Schwab needed) ranks the universe by fundamentals
> alone — a quick, free check.

## Keeping the data fresh (you run these)

The local store goes stale as companies report. Nothing refreshes it automatically — run these
periodically:

| Cadence | Command | Why |
|---|---|---|
| **Weekly** | `uv run wheel-screener auth-login` | Schwab's refresh token expires every 7 days |
| Daily / weekly | `uv run wheel-screener refresh-earnings` | refresh the earnings-blackout calendar (cheap) |
| Daily / weekly | `uv run wheel-screener refresh-fundamentals` | re-fetch fundamentals for names that just reported |
| Occasionally | `python3 tools/fmp_bulk_import.py --out data/fundamentals --years 2015-2025` | full rebuild if the store drifts too far |

Want it hands-off? Add the refresh jobs to your own `cron` (the screen stays manual; `auth-login`
can't be automated — it needs a browser):

```cron
# example — adjust paths
0  6 * * *  cd ~/dev/OptionsScreener && ~/.local/bin/uv run wheel-screener refresh-earnings
30 6 * * *  cd ~/dev/OptionsScreener && ~/.local/bin/uv run wheel-screener refresh-fundamentals
```

## Commands

| Command | What it does |
|---|---|
| `candidates` | full screen → ranked candidate CSV (fundamentals + live chains) |
| `screen` | fundamentals-only ranking → CSV (no Schwab needed) |
| `auth-login` | Schwab OAuth browser login (run weekly) |
| `refresh-earnings` | rebuild the local earnings calendar from FMP |
| `refresh-fundamentals` | incremental fundamentals refresh for recent reporters |

Global flags go *before* the command: `-v` / `-vv` for progress / per-symbol logging, and
`--debug` for a full traceback on an unexpected error — e.g. `wheel-screener -v candidates …`.

## Logging & troubleshooting

Results and the table print to **stdout**; everything diagnostic goes to **stderr**, so
`candidates -v > out.csv` keeps the CSV clean while you watch progress.

- **Console verbosity** — quiet by default (only warnings + errors). Add `-v` for per-stage
  progress (universe → fundamentals funnel → chains → candidates), or `-vv` for per-symbol detail:
  ```bash
  uv run wheel-screener -v candidates --top-n 250
  ```
- **Always-on log file** — every run also writes to a **rotating file at `logs/wheel-screener.log`**
  (INFO and up, regardless of console verbosity; ~1 MB × 5 files). This is what makes a cron'd
  refresh recoverable — the history is on disk even with no console attached. Tune via
  `LOG__DIR`, `LOG__FILE_LEVEL`, `LOG__MAX_BYTES`, `LOG__BACKUP_COUNT`, or `LOG__ENABLE_FILE=false`.
- **Errors** — data-provider problems (expired Schwab login, rate limit, outage) print a clear,
  actionable message and exit non-zero — **never a silently empty result**. For a full traceback on
  an *unexpected* failure, re-run with `--debug`.

## How it's built

Hexagonal **ports & adapters**: a framework-free `core/` (models, pipeline, ranking,
`ScreenerService`) wrapped by thin delivery layers (`cli/` today, `api/` FastAPI later — both call
the *same* service). Concrete providers live in `adapters/` behind `core/ports.py` Protocols and are
wired in `composition.py`; swapping a provider is a one-line change.

```
src/wheel_screener/
  core/      models · ports · fundamentals · ranking · errors · service
             pipeline/ (universe · rate_fundamentals · pull_chains · select_strike · rank)
  adapters/  fmp/ · schwab/ · local/ · http.py · cache.py · errors.py
  cli/  api/   config.py   composition.py   logging_config.py
tools/   fmp_bulk_import.py     # one-time whole-market bulk loader
docs/    PLAN.md · TODO.md
```

## Development

```bash
uv run ruff check .     # lint
uv run pytest           # tests
```

CI (GitHub Actions) runs ruff + pytest on Python 3.11 and 3.12 for every push and pull request.
`main` is **protected** — changes land via a PR with green CI. Deferred / optional work is tracked
in [docs/TODO.md](docs/TODO.md).

## Status

**v0.2.0 — web UI + Alpaca.** Working end-to-end and live-validated: local fundamentals → Schwab
**or Alpaca** chains → ranked CSP shortlist, with the earnings blackout, conservative bid-based
yields, and a blended fundamental+yield ranking. A server-rendered **web UI** (FastAPI + HTMX) runs,
cancels, and displays screens with live progress, sortable results, per-candidate detail, an instant
precomputed dashboard, and CSV export. Hardened for real-world failures — typed provider errors,
retry/backoff, run timeout + cancellation + partial results, no silent masking, no raw tracebacks,
and verbosity-controlled logging with an always-on rotating log file. The UI is intentionally
preliminary (see [`docs/UI_STATUS.md`](docs/UI_STATUS.md)); auth + deployment (DigitalOcean) and a
native/Swift front-end are the next layers.
