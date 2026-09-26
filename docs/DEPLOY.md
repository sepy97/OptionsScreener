# Deploying steadybull.net

Single-user production deploy: FastAPI + HTMX behind Caddy (auto-TLS) on a DigitalOcean droplet,
via `docker compose`. Chain source is **Alpaca** (key/secret, no OAuth). The ~2 GB fundamentals
store is slimmed to ~365 MB and lives on the host, mounted into the container — never in the image.

## Artifacts

| File | Role |
|---|---|
| `Dockerfile` | app image (uv `--frozen --extra api`, uvicorn, non-root, healthcheck) |
| `docker-compose.yml` | `app` + `caddy`; prod env (`AUTH__REQUIRED=true`, `AUTH__SCOPE=portfolio`, `CHAIN_SOURCE=alpaca`), `./data` volume |
| `deploy/caddy/Caddyfile` | `steadybull.net` → reverse-proxy `app:8000`, automatic HTTPS; `www` redirects. Mounted as a directory — see compose for why |
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
GH_TOKEN=...                            # OPTIONAL — enables the Fundamentals tab, see below
```

### `GH_TOKEN` — the Fundamentals tab (optional)

The tab is powered by a fundamental-analysis engine that lives in a **private** repository, so it
is deliberately not a dependency of this project: it is absent from `pyproject.toml` and
`uv.lock`, which is what keeps this repository installable and testable by anyone with no
credentials, and keeps CI secret-free.

Instead `deploy/fetch_fundcore.sh` downloads a built wheel into `vendor/` **before** the image is
built, so the build itself needs no registry credentials, no BuildKit secret and no git binary.
The deploy workflow runs it automatically.

Create a **fine-grained personal access token** scoped to the `sepy97/StockAnalysis` repository
with **Contents: read-only**, and set it as `GH_TOKEN` in `/srv/steadybull/.env`.

Without it the fetch is a deliberate no-op: the image builds normally, and the Fundamentals tab
explains that it is not deployed. Every other part of the app is unaffected.

The engine version deployed is pinned in [`deploy/fundcore.version`](../deploy/fundcore.version).
To upgrade, bump that one line, merge, and tag a release as usual.

## Bring it up

```bash
docker compose up -d --build
docker compose logs -f app          # watch it warm the store
curl -sf https://steadybull.net/health
```

Open https://steadybull.net — the screener loads with no prompt (it is public on purpose).
Open **/portfolio** and you should get a Basic-Auth prompt: compose sets `AUTH__SCOPE=portfolio`, so
the password covers the account data and the broker link and nothing else. If `AUTH__PASSWORD` is
unset the **app refuses to start** (by design), so a healthy container means the gate is on.

Why the portfolio needs it even though the tab already requires signing in with Schwab: the OAuth
*connect* route cannot require a session (nobody has one before signing in), so without a password
any visitor with a Schwab account of their own could complete the exchange, overwrite the stored
token and end the owner's sessions. See [`MULTI_USER_PLAN.md`](MULTI_USER_PLAN.md) §1.

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

## Portfolio tab (optional)

The tab appears for everyone, but only signed-in people get past it (see **Signing in** below),
and only an admin can connect a broker — which needs Schwab credentials on the droplet. Without
them the tab tells the admin there is nothing to connect to, and nothing else in the app is
affected.

```
# /srv/steadybull/.env
SCHWAB__CLIENT_ID=...
SCHWAB__CLIENT_SECRET=...
```

`SCHWAB__CALLBACK_URL` is set by `docker-compose.yml`, not `.env` — it defaults to a loopback
address for the CLI's local login flow, and a deploy that inherited that default would send the
visitor's browser to their own machine with the authorization code attached. The web sign-in stays
disabled (the tab says the broker isn't set up) until the callback points at this site.

```bash
sudo mkdir -p /srv/steadybull/data/links
sudo chown -R 10001:10001 /srv/steadybull/data/links
```

The users, passkeys and sessions, and the broker token, are written to the mounted volume
(`compose` sets `PORTFOLIO__SESSIONS_DB_PATH` and `SCHWAB__TOKEN_PATH`) — a deploy replaces the
container, so anything left inside it would be destroyed on every release. **Back that file up**:
losing it loses every account, and a passkey cannot be re-issued from the server side (#66).

The Schwab app must also have the **Accounts and Trading** product and must register
`https://steadybull.net/portfolio/oauth/schwab/callback` as a callback URL.

### The weekly reconnect

Schwab refresh tokens last **7 days**. This is the only part of the deployment that stops working
on a clock rather than by breaking, so it will not announce itself — the Portfolio tab simply goes
quiet. Both diagnostics report the time remaining:

```bash
curl -s https://steadybull.net/health | jq '.brokers, .warnings'
docker compose exec app wheel-screener doctor      # "Broker link" section
```

`/health` warns below 48 hours and **deliberately does not go `degraded`** for an expiring link: a
non-200 there fails the container healthcheck and rolls back the release, which cannot renew a
token that was always going to lapse.

`doctor` goes further and **calls the broker** rather than reading the token file's age. A token
can sit on disk, unexpired, and be useless — authorising anywhere else revokes the previous one,
and a token written in the wrong shape loads without complaint and fails every request. Both look
healthy to a presence check.

Reconnecting is one click on the Portfolio tab. It no longer signs you in — the passkey does
that, on its own 90-day clock — so the two expire independently.

### Signing in: passkeys and invites

Nobody signs up; an admin invites them. **The first admin has to come from the droplet** — a site
where the first visitor becomes admin is a well-known way to lose one:

```bash
docker compose exec -T app wheel-screener invite "Sam" --admin
```

After that, invites are made on the site: **Portfolio → Invite people** (admins only). The page
makes a link and shows it once with a Copy button, lists invites not yet used (with Cancel), and
lists everyone with a *New passkey link* button. The CLI does the same things (`invite`, `invite
--for-user <id>`, `users`) if the site is ever unreachable.

A link works once, for 72 hours (`PASSKEYS__INVITE_HOURS`). Opening it and pressing *Save a
passkey* creates the account and signs the person in; there is no password anywhere. Send it
privately — until it is used, whoever holds it can claim it.

**A lost device, or a second one:** *New passkey link* on that person's row makes a link that adds
a passkey to their account instead of creating one. Old passkeys keep working.

Only admins can connect a broker, because there is still one Schwab token per deployment: anyone
else linking would replace the owner's. A signed-in non-admin sees no account at all — the token
does not know whose it is, so who linked it is recorded separately and checked on every request.

`PASSKEYS__RP_ID` and `PASSKEYS__ORIGIN` are set by `docker-compose.yml` to `steadybull.net`. A
passkey is bound to that hostname, so it will not work on the droplet's IP or any other name.

#### First deploy of passkeys (v3.3.0)

The password that v3.0.0 put in front of `/portfolio` is switched off in the same release: the
passkey sign-in page is the only way in. (Compose blanks `AUTH__PASSWORD`, since `.env` still has
one.) Until step 2, **nobody can reach the Portfolio**, you included.

1. Deploy.
2. `docker compose exec -T app wheel-screener invite "<you>" --admin`, and open the link.
3. Save a passkey. You land on the Portfolio.
4. **Reconnect Schwab once.** The token on disk has no recorded owner — it predates owners — so
   the tab offers *Connect Schwab* rather than guessing it is yours.
5. Make a *New passkey link* for yourself on **Invite people** and open it on your other devices,
   unless your passkey already syncs to them through iCloud Keychain or Google Password Manager.

Rolling back to v3.2.0 is safe: this release only adds tables. The old release finds its own
session table untouched and goes back to signing in with Schwab.

### Linking brokerages through SnapTrade

How people link their own accounts — Fidelity, Robinhood, IBKR, Schwab and the rest — without this
site ever seeing a broker password: *Link a brokerage* sends them to SnapTrade's connection portal,
they sign in **at their broker**, and they come back to `/portfolio/brokerages/return`. Read-only:
the portal is always asked for a read connection, and no call that trades exists in the code.

Off until three values are in the droplet's `.env`:

```
SNAPTRADE__CLIENT_ID=...        # SnapTrade dashboard — COMMERCIAL keys (each person links their own)
SNAPTRADE__CONSUMER_KEY=...
SNAPTRADE__SECRET_KEY=...       # this deployment's own; see below
```

`SNAPTRADE__SECRET_KEY` encrypts the secret SnapTrade issues each person, which is stored in the
accounts database — so the database, or a backup of it, cannot read anyone's brokerage on its own.
Make it once, and **keep a copy in a password manager**:

```bash
docker compose exec -T app python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Losing it costs nobody their account, only their broker links (a click each to redo). A malformed
key stops the app at startup, loudly, rather than at the first person's click.

People are registered with SnapTrade under this site's internal user id, never their email. The
free tier allows 5 connected accounts; the next step up is $100/month (see
[`MULTI_USER_PLAN.md`](MULTI_USER_PLAN.md) §3).

**Built from SnapTrade's published API spec, not yet run against a real linked account.** The
first time someone links one, check their Portfolio against the broker's own app: balances, each
option's strike, expiry and contract count, and the opening dates. Anything wrong there is a
mapping fix in `adapters/snaptrade/account.py`, whose notes say what each field was assumed to be.

## Diagnosing a broken data connection

```bash
cd /srv/steadybull
docker compose exec -T app wheel-screener doctor     # names the failing provider
curl -s https://steadybull.net/health                # same, as JSON under `providers`
```

Credentials live in `/srv/steadybull/.env`. After editing it, `docker compose up -d` recreates
the container — no rebuild needed, since only the environment changed. Note that Alpaca and FMP
are static key/secret pairs regenerated in their dashboards (Alpaca shows the secret once), while
Schwab is OAuth and is refreshed with `auth-login`.

## Rollback & backups

- **Rollback:** `git checkout v<previous>` and `docker compose up -d --build` — or run the Deploy
  workflow by hand against an older tag.

### What is backed up, and how

`wheel-screener backup` runs nightly from cron (04:30 New York) and writes a dated folder under
`/data/backups`, keeping the newest 14 (`BACKUP_KEEP`):

| File | What it holds | Why it is copied |
|---|---|---|
| `accounts.sqlite` | who can sign in, their passkeys' **public** keys, who owns the broker link | Losing it loses every account: people would need new invites, and their old passkeys would be orphaned on their phones |
| `jobs.sqlite` | past screens, including the precomputed ones | Screens are rebuilt four times a day, but the history is not |
| `overlay_metrics.csv` | fundamentals refreshed after earnings | The bulk store does not have them |

Each copy is taken with SQLite's online backup from a read-only connection, so it is consistent
while the app is writing and cannot disturb it; each is integrity-checked; a backup is assembled
under a temporary name and renamed only when complete.

**What the accounts copy deliberately leaves out:** live sessions, invite links, sign-in
challenges and OAuth state. Each is a bearer credential or worthless in minutes, and a backup
should not be a way into the site. The rule is a list of tables to *keep*, so a table added later
is left out until someone decides it belongs — and the file is rebuilt after the rest are dropped,
because SQLite otherwise leaves deleted rows in the file as leftovers (a test searches the backup's
bytes for session tokens). The cost: after a restore everyone signs in again, which is one passkey
tap.

**Still not backed up, on purpose:**

| File | Why not |
|---|---|
| `data/links/schwab_token.json` | It expires in 7 days, so a restored copy is dead on arrival more often than not — and it is **trading-capable**. Copying it into a backup that leaves the box widens the blast radius of a credential that a single click replaces. |
| `data/fundamentals/` (except the overlay) | Rebuilt by the refresh job. |

### Getting it off the droplet — needs doing once, by hand

`/data/backups` is on the droplet's own disk. That protects against a bad migration or a corrupt
file, **not against losing the droplet**. For that, turn on DigitalOcean's backups:
*Droplets → steadybull → Backups → Enable* (daily or weekly; ~20–30% of the droplet's price). They
image the whole disk, and the nightly copies are what make that image safe to restore from — an
image taken mid-write can catch a live SQLite file in an inconsistent state, while the copies in
`/data/backups` never are.

After a deploy that changes `deploy/crontab`, reinstall it: `crontab /srv/steadybull/deploy/crontab`.

### Restoring

```bash
cd /srv/steadybull
docker compose stop app
B=data/backups/<the folder>                              # e.g. 2026-09-27T083000
sudo cp $B/accounts.sqlite data/sessions.sqlite && sudo rm -f data/sessions.sqlite-wal data/sessions.sqlite-shm
sudo cp $B/jobs.sqlite     data/jobs.sqlite     && sudo rm -f data/jobs.sqlite-wal data/jobs.sqlite-shm
sudo cp $B/overlay_metrics.csv data/fundamentals/
sudo chown -R 10001:10001 data
docker compose start app
```

Remove the `-wal`/`-shm` files: they belong to the database being replaced, and SQLite would try to
apply them to the restored one. The tables the backup left out are recreated empty on start.
Everyone then signs in with their passkey as usual; if the Schwab token was lost too, the owner
reconnects it.
