# wheel-screener

A cash-secured-put / wheel options screener. Ranks a stock universe on **fundamentals** (FMP —
valuation, efficiency, sustainability), pulls option chains with greeks/IV for the best
names (Schwab), selects the put nearest **−0.20 delta** at **30–45 DTE**, and ranks the shortlist
by **annualized yield** (per-contract IV shown as a column; an earnings blackout removes event-risk
names). Output is a list of **candidates to verify**, not signals.

> Full design, data-source facts, and roadmap: **[docs/PLAN.md](docs/PLAN.md)**.

## Status

**M0 — scaffold.** Architecture, domain models, provider ports, config/DI wiring, CLI shell, and
tests are in place. Pipeline stages and provider adapters (FMP/Schwab fetching) are stubs
(`NotImplementedError`) landing across M1–M3. The pure pieces are implemented + tested: the
fundamental rating (`core/fundamentals.py`, criteria reused from `pythonBot`), annualized-yield
math, nearest-delta selection, and the earnings blackout.

## Architecture (ports & adapters)

A framework-free `core/` (models, ports, pipeline, ranking, service) wrapped by thin delivery layers
(`cli/` today, `api/` FastAPI later — both call the *same* `ScreenerService`). Concrete providers
live in `adapters/` behind `core/ports.py` Protocols and are wired in `composition.py`. Swapping a
provider = one line.

```
src/wheel_screener/
  core/      models.py · ports.py · fundamentals.py · ranking.py · service.py
             pipeline/ (universe · rate_fundamentals · pull_chains · select_strike · rank)
  adapters/  fmp/ · schwab/ · http.py · cache.py
  cli/  api/  jobs/   config.py   composition.py
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync --all-extras          # create venv + install deps (uv fetches Python 3.11)
uv run ruff check .           # lint
uv run pytest                 # tests
uv run wheel-screener --help  # CLI shell
```

Copy `.env.example` to `.env` and fill provider keys (see comments there). Register a Schwab
"Market Data Production" app at https://developer.schwab.com **early** — approval takes a few days,
and the OAuth refresh token must be renewed weekly (`uv run wheel-screener auth-login`, M2).

## Configuration

Typed via `pydantic-settings`; env vars use a `__` nesting delimiter, e.g. `SCHWAB__CLIENT_ID`,
`FMP__API_KEY`, `IV_RANK__SOURCE`. See `.env.example`.
