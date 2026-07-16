# Deploying steadybull.net

Single-user production deploy: FastAPI + HTMX behind Caddy (auto-TLS) on a DigitalOcean droplet,
via `docker compose`. Chain source is **Alpaca** (key/secret, no OAuth). The ~2 GB fundamentals
store is slimmed to ~365 MB and lives on the host, mounted into the container — never in the image.

## Artifacts

| File | Role |
|---|---|
| `Dockerfile` | app image (uv `--frozen --extra api`, uvicorn, non-root, healthcheck) |
| `docker-compose.yml` | `app` + `caddy`; prod env (`AUTH__REQUIRED=true`, `CHAIN_SOURCE=alpaca`), `./data` volume |
| `Caddyfile` | `steadybull.net` → reverse-proxy `app:8000`, automatic HTTPS |
| `tools/slim_store.py` | build the deploy-size fundamentals store |

## One-time: provision the droplet

1. Create a DigitalOcean droplet — Ubuntu LTS, `s-1vcpu-2gb` (~$12/mo; the slim store fits the
   50 GB root disk, so no block volume).
2. Point DNS: an **A record `steadybull.net` → droplet IP** (Caddy needs it to issue the cert).
3. Harden: a non-root sudo user, SSH-key-only login, `ufw allow 22,80,443`, install Docker +
   the compose plugin.

## One-time: seed data + secrets

On your laptop, build the slim store and ship it (plus the earnings calendar) to the droplet:

```bash
uv run python tools/slim_store.py --src data/fundamentals --out data/fundamentals-slim
rsync -avz data/fundamentals-slim/   droplet:/srv/steadybull/data/fundamentals/
rsync -avz data/earnings_calendar.csv droplet:/srv/steadybull/data/earnings_calendar.csv
```

On the droplet, clone the repo into `/srv/steadybull` (so `./data` = `/srv/steadybull/data`).

**Make the data dir writable by the container** — it runs as **uid 10001**, but a host bind mount
keeps the host owner, so hand it over or the app can't create `jobs.sqlite` and won't start:

```bash
sudo chown -R 10001:10001 /srv/steadybull/data
```

Then write `.env` (from `.env.example`) with the real secrets — **at minimum**:

```
AUTH__PASSWORD=<a strong password>      # AUTH__REQUIRED=true is set by compose; boot fails without this
ALPACA__API_KEY=...
ALPACA__API_SECRET=...
ALPACA__FEED=indicative                 # or opra (paid, real-time)
ALPACA__TRADING_BASE_URL=https://api.alpaca.markets   # paper keys -> paper-api.alpaca.markets
FMP__API_KEY=...                        # for the refresh-earnings / refresh-fundamentals cron
```

## Bring it up

```bash
docker compose up -d --build
docker compose logs -f app          # watch it warm the store
curl -sf https://steadybull.net/health
```

Open https://steadybull.net — you should get a Basic-Auth prompt, then the dashboard. If
`AUTH__PASSWORD` is unset the **app refuses to start** (by design), so a healthy container means the
gate is on.

## Scheduled refresh (cron on the host)

The refresh jobs (earnings + post-earnings fundamentals daily, and a precomputed screen a few
times per market day so the dashboard stays current) live in [`deploy/crontab`](../deploy/crontab).
Install them for the deploy user:

```bash
# first, confirm the command works interactively (writes /data/earnings_calendar.csv):
cd /srv/steadybull && docker compose exec -T app wheel-screener refresh-earnings
# then install the schedule (replaces the user's crontab — dedicated droplet, so that's fine):
crontab /srv/steadybull/deploy/crontab
crontab -l        # verify
```

Cron-level errors go to `$HOME/steadybull-cron.log`; the CLI's own logs are in
`/data/logs/wheel-screener.log`. Times are in the droplet's local timezone (America/New_York).

## Releasing (deploy-on-tag)

Versioning is **semver-for-apps** (`__init__.__version__` is the single source):

- `1.0.0` = this first production deploy (bump `src/wheel_screener/__init__.py`, PR, merge).
- Tag it: `git tag v1.0.0 && git push origin v1.0.0`.
- On the droplet, deploy that release:

```bash
git fetch --tags && git checkout v1.0.0
docker compose up -d --build
```

Later: `MINOR` for features, `PATCH` for hotfixes (both deploy), `MAJOR` when a release needs a
manual migration step. (A GitHub Action to deploy automatically on a `vX.Y.Z` tag is step #6.)

## Rollback & backups

- **Rollback:** `git checkout v<previous>` and `docker compose up -d --build`.
- **Backups:** scheduled DigitalOcean droplet snapshots, plus a cron copy of `data/jobs.sqlite`
  and `data/fundamentals/overlay_metrics.csv` (the mutable state).
