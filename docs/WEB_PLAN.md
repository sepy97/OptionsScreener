# Web UI plan (Milestone M3)

The roadmap for putting a web interface on the screener. Kept API-first so a future Swift
client remains possible. See [PLAN.md](PLAN.md) for the overall product design.

## Decisions

- **Scope:** private, **single-user** web app (not multi-user / SaaS).
- **Stack:** **FastAPI + HTMX** (server-rendered Jinja templates). The JSON API stays a thin,
  optional layer kept ready for a future Swift client — not a second surface we maintain now.
- **Execution model:** a screen takes minutes, so it runs as a **background job** and the UI
  **polls for progress** (reuses the `cancel` / `max_runtime_seconds` / partial-results /
  stage-logging seam built in the hardening PRs).
- **Results store:** **SQLite** on the deploy volume (single-user; no DB server needed).
- **App access:** single password / HTTP Basic Auth behind HTTPS.
- **Schwab OAuth on the server:** a web "Connect Schwab" callback route (interim: upload a
  locally-generated token file).
- **Deploy:** **DigitalOcean Droplet + block-storage volume + docker-compose + Caddy (auto-TLS) +
  host cron.** Chosen over App Platform (the data store is stateful) and over AWS/Azure (simpler,
  and matches existing experience).

## Milestones

| Milestone | Scope | Status |
|---|---|---|
| **M3.0** | Core prep — finalize the JSON contract | ✅ done (#11) |
| **M3.1** | API / serving foundation | **this doc** |
| **M3.2** | HTMX web UI (dashboard, run form, live progress, detail) | planned |
| **M3.3** | Auth (app gate) + Schwab OAuth on the server | planned |
| **M3.4** | Data & scheduled ops (slim store, cron refresh, backups) | planned |
| **M3.5** | Containerize & deploy (Dockerfile, compose, DO droplet, CI/CD) | planned |

---

## M3.1 — API / serving foundation

**Goal:** make the FastAPI backend correct, fast, safe under concurrency, and able to run
minutes-long screens without blocking a request. No UI, auth, or deployment here — after M3.1 the
backend is `curl`-able clean JSON with job polling.

### A. Serving foundation
1. **Singleton `ScreenerService` via FastAPI `lifespan`** — build once at startup, share across
   requests (stop `get_service` rebuilding providers + re-reading the store per request).
2. **Thread-safety** — a `threading.Lock` around `LocalFundamentalsProvider`'s lazy load
   (`_ensure_loaded` / overlay), or eager-load at startup, so concurrent first-touch requests
   don't race.
3. **Overlay freshness** — reload `overlay_metrics.csv` on mtime change, so a `refresh-fundamentals`
   run is picked up without restarting the server.
4. **Shared Schwab `RateLimiter`** — one limiter across all requests (falls out of the singleton),
   so concurrent screens can't collectively blow past Schwab's per-minute cap.
5. **Fix `api/app.py`** — `run_screen` is called without the required `today` arg (currently 500s);
   pass `date.today()` and decide server-set vs. an optional "as-of" date.
6. **Typed-error → HTTP handlers** — `AuthExpiredError`→401, `RateLimitedError`→429 (+`Retry-After`),
   `ProviderUnavailableError`→503, `ProviderDataError`→422 (instead of opaque 500s).
7. **`GET /health`** — reports store-loaded + Schwab-token-present/valid (drives platform liveness
   probes and lets the UI prompt re-auth proactively, before a screen fails mid-run).

### B. Slow-screen support
8. **Background-job runner** — `POST /screen` returns immediately (202 + job id); the screen runs in
   a background thread; job state (status / progress / partial / result / error) lives in SQLite.
   In-process is sufficient for one user (no Celery/Redis).
9. **Progress/poll endpoint** — `GET /screen/{id}` returns status + stage progress (universe →
   fundamentals → chains → candidates, from the pipeline logs) + partial/final results.
10. **Cancel-on-disconnect + time budget** — wire `Request.is_disconnected()` to the `cancel` event
    and honor `max_runtime_seconds` (the core seam already exists).
11. **Slim request DTO** — a `ScreenRequest` with ~6 user-facing fields (top_n, fundamental_weight,
    min_yield, DTE range, timeout) mapped to `ScreenCriteria` server-side; don't expose all ~30
    internal engine knobs as the API contract.

### C. Tests
12. **`TestClient` coverage** with a fake `ScreenerService` via `dependency_overrides`: `/screen`
    happy path (regression for the `today` bug), `/health`, the job lifecycle (start → poll →
    result), and the error → status mapping.

### PR breakdown
- **PR A — serving foundation:** items 1–7 + tests.
- **PR B — slow-screen support:** items 8–11 + tests.

### Acceptance criteria
- Exactly one service instance per process; the store is loaded once at startup.
- Concurrent requests don't race on first load; a single shared rate-limiter governs Schwab calls.
- `POST /screen` returns a job id immediately; `GET /screen/{id}` reports progress then results;
  disconnecting or exceeding the time budget stops the run and returns partials.
- Provider failures surface as 401/429/503/422 (never a raw 500); `/health` reflects store + token.
- The API has `TestClient` coverage (it has none today).

### Out of scope (later milestones)
HTML / templates / styling (M3.2); login + Schwab-on-server OAuth (M3.3); containerization &
deployment (M3.5).
