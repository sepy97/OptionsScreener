# TODO / deferred

Optional work, not needed for the core CSP/wheel screen (which is complete and live-validated).

## Deferred
- **IV-rank overlay** *(postponed 2026-06-22)* — an `IvRankProvider` that contextualizes a
  contract's implied volatility against its own recent history, to flag *structurally* high IV
  the earnings blackout can't (e.g. litigation/event vol). Schwab exposes only raw per-contract
  IV, so this needs either a homegrown daily IV store or a paid feed (ORATS / FlashAlpha).
- **FastAPI backend** — wrap the existing `ScreenerService` for a web UI (`api/` is scaffolded).
- **Swift app** — consumes the REST API.

## Declined (do not build)
- Deeper / multi-year-averaged fundamentals (TTM snapshot is intentional for a screener).
- LLM red-flag annotator on the shortlist.
- IBKR integration.
