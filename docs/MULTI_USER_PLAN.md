# Multi-user — investigation and plan

**A living document.** Update it as decisions land and phases complete.

**Target release: v3.0.0.** Source brief: [`MULTI_USER_BROKER_LINKING.md`](MULTI_USER_BROKER_LINKING.md)
(vendored verbatim, 2026-09-25). That brief covers the infrastructure — login, the broker
middleman, the tables, keeping users apart. This document covers what it does not: what in *this
codebase* assumes one person, and in what order to take it apart. It also records what was checked
against the vendors rather than assumed.

**Status:** investigated; **Phase 0 built** (the password now covers /portfolio only — §1), not
yet deployed. Supersedes the line in
[`PORTFOLIO_PLAN.md`](PORTFOLIO_PLAN.md) §1b — "Multi-user is explicitly out of scope: one
operator, one session at a time, no user table."

| Decision | State |
|---|---|
| Audience | **friends now, decide later** — build on SQLite, accept a Postgres migration if it goes public (§6) |
| Phase 0 posture | **decided and built: gate /portfolio only, screener stays public** (§1) |
| Login | passkeys + invite links, per the brief (§4) |
| Broker linking | SnapTrade, keeping the direct Schwab adapter for the owner (§3) |
| Database | SQLite, with one choke point and a two-user leak test in place of row-level security (§5) |
| Release label | v3.0.0 |

---

## 1. The reason this is now urgent, not just wanted

The deployment that is **running right now** is public on purpose — the compose file it was built
from set `AUTH__PASSWORD: ""` and `AUTH__REQUIRED: "false"` — and the Portfolio tab is gated by
"Sign in with Schwab": no
session, no account data, and the session is minted only by a completed Schwab OAuth exchange.
For **viewing** the owner's data that is sound, and it is what `PORTFOLIO_PLAN.md` §1 set out to
achieve — a stranger who clicks Connect is sent to Schwab and stops there.

What it does not cover is a stranger who has a Schwab account **of their own**. They click
Connect, authenticate to Schwab as themselves, and the callback:

1. writes *their* refresh token over `/data/links/schwab_token.json` — the deployment has one
   token path, from `SchwabSettings.token_path`;
2. calls `store.revoke_broker("schwab")`, ending every session the owner held;
3. mints them a session.

Nobody sees anybody else's positions — each callback replaces the token and the sessions
together — but the deployment has a single broker slot that any visitor can claim, and claiming
it evicts the owner. (`link.complete()` writes the token a moment before `revoke_broker` runs, so
a live session spanning those milliseconds would read the new account. Narrow, but it is there.)

### Phase 0, as built

The password gate was site-wide or nothing; it now takes a **scope** (`AuthSettings.scope`, a
`Literal` so a misspelling refuses to start):

* `site` — every path. The default, and the original posture.
* `portfolio` — only `/portfolio` and below. The screener stays shareable; account data **and the
  broker link** need the password.

`docker-compose.yml` sets `AUTH__REQUIRED=true` and `AUTH__SCOPE=portfolio`, and no longer forces
`AUTH__PASSWORD` empty — so **`AUTH__PASSWORD` must be in the droplet's `.env` before this
deploys**, or the app fail-closes and refuses to start, which is the intended behaviour and would
take the site down.

One ordering change came with it. The session gate had been registered *outside* the password gate
(Starlette runs the last-registered middleware outermost), so an unauthenticated
`/portfolio/positions` was redirected to the Connect page — and did a session-store read — before it
was ever challenged. No data escaped, but the order was backwards; the session gate is now
registered first so it sits inside the password check. The test asserts a 401 *without* following
redirects, which is what pins the order.

Note what Phase 0 does **not** settle: the screener stays public, so the data-licence question in
§7 stays open on its own terms.

---

## 2. What in the code assumes one person

The brief says nothing about this, and it is most of the work. The good news first.

### Already the right shape

* **`SessionStore`** (`api/sessions.py`) is already server-side, opaque-token, revocable, expiry-
  checked on read, SQLite-backed. A passkey login needs exactly this. Adding `user_id` is a
  column, not a redesign.
* **`oauth_state`** in the same store is single-use, TTL'd, and consumed on callback. A WebAuthn
  registration or authentication challenge has *precisely* that lifecycle, so the passkey work
  reuses code that already exists and is already tested — add a `purpose` column rather than a
  second table.
* **The ports are the seam.** `BrokerageAccountProvider` is a Protocol over "read the linked
  accounts". SnapTrade is one more adapter under `adapters/`, and `core/` does not change. The
  deliberate split between the reading port and `OAuthBrokerLink` (recorded in
  `PORTFOLIO_PLAN.md` §1b) is what makes a middleman implementable at all.
* **`parse_osi`** (`core/osi.py`) already parses OCC option symbols, which is what SnapTrade
  returns — so most of the position mapping is written.
* **The `/portfolio` gate** is already deny-by-default with exact-match exemptions, and the
  comment explains why prefix-matching is how the callback ends up unprotected by accident. The
  passkey session gate should keep that shape rather than invent one — see Phase 1 in §8, which
  Phase 0 has made a smaller job than it looked.
* **Shared and private are already physically apart:** the fundamentals store, chain caches and
  jobs DB are shared; sessions and the broker token are private.

### What breaks

**a. `ScreenerService` is a process singleton that carries a per-user credential.** Built once in
the app lifespan, stashed on `app.state.service`. Every field on it is shared *except*
`accounts`, and exactly one method reads that field (`brokerage_accounts()`). Everything else
account-shaped — `_price_positions`, `swap_reviews`, `_fresh_put_and_ask` — works on `Position`
objects it is handed, and `exit_options` takes explicit arguments and touches no account at all.
So the per-user surface is one field and one method. §2.1 below is the proposed split.

**b. Two process-level caches hold account data, keyed by nothing.**

| Cache | Contents | Under multi-user |
|---|---|---|
| `app.state.balances_cache` | `(timestamp, every account)`, 30 s TTL | **a leak**: the second visitor inside 30 s is served the first one's balances and positions |
| `app.state.swap_cache` | `{(symbol, quantity, strike, expiration): (timestamp, verdict)}`, 10 min | **a collision, not yet a leak** |

The swap cache deserves the distinction. Its key includes the contract *and* the size, and the
verdict is computed from `OpenPut(symbol, strike, days, contracts, spot, ask)` — every field of
which is either in the key or market-wide. So two people short the same MRVL $210 Oct 30 put in
the same quantity share an entry whose contents are correct for both. It is wrong in principle
and harmless in content, and it is **one field away from not being**: the moment a verdict carries
the premium collected or the date opened — both of which the Schwab adapter already reconstructs
from transactions — the same collision serves one person's entry price to another.

Neither cache is bounded. `swap_cache` grows for the process's life and is cleared only by the
Refresh button.

**c. `revoke_broker(broker)` deletes every session for a broker.** Deliberate today ("a relink may
be a different account"). Under multi-user, one person reconnecting signs *everyone* out.

**d. `latest_done()` is global.** The Close? column compares each open put against the most recent
finished screen, whatever it was. Today that is one of the four scheduled cron screens or a run
the owner started. With users it is whoever screened last, on whatever criteria — so a friend's
odd DTE window would silently become the comparison for everybody's positions.

**e. One screen in flight, process-wide.** `JobRunner.start` raises `JobBusyError` if `_active` is
set. Today "a screen is already running" always means *you* started it; with users it means a
stranger did. And a screen is 2–4 minutes of the shared FMP and Alpaca budget, so N users pressing
Run is a quota problem as much as a UX one.

**f. Issue #64 — job results and CSV export have no owner and no TTL.** Any job id fetches any
screen forever. Screens are shared, so the harm is small; the criteria still describe how somebody
trades.

The CLI stays single-user and that is fine: it runs on the operator's own machine against their own
token file. What has to stop is `SchwabSettings.token_path` governing the **web**.

### 2.1 The proposed split

Three ways to get the user's credential to the code that needs it:

| | Shape | Verdict |
|---|---|---|
| A | `brokerage_accounts(accounts)` — pass the provider per call | Smallest diff. Leaves a shared singleton whose methods are about *somebody's* positions, and nothing stops the next method from reading a stale field. |
| B | **A `PortfolioService`** holding `accounts` plus the shared ports it needs, built per request from the session | **Recommended.** |
| C | A registry of per-user services, cached by user id | Worst. It re-creates the exact bug this project has already been bitten by twice: a process-level dict keyed by something, read by the wrong request. |

B is recommended because the invariant becomes structural rather than remembered. After the
split, `ScreenerService` has **no field that belongs to a user**, so "did this request use the
right person's data?" has exactly one answer — whatever `PortfolioService` was constructed with —
and it cannot be got wrong by forgetting to pass an argument. A test can even pin it:
`assert not hasattr(service, "accounts")`.

The per-request construction is one FastAPI dependency, `get_portfolio(request)`, built from the
session's user id, and it is the **only** place in the app where a user id turns into a
credential. That is the single place the brief's §4 rule — always take the user from the session,
never from anything the browser sent — has to hold.

Caches move off `app.state` into a small TTL cache whose key is always prefixed with the user id
supplied by that dependency, so a route cannot spell a key itself. Keying them by the session
token is also **correct today and forward-compatible**, so that part can land before anything
else in v3.

---

## 3. Broker linking: what the vendors actually say

Checked against the documentation on 2026-09-25 rather than assumed.

### SnapTrade covers what this app needs

* **Option positions are first class.** `listOptionHoldings` returns strike, expiration,
  CALL/PUT, the OCC ticker and the underlying, and **negative `units` for a short position** —
  which is the whole Portfolio tab.
* **The open date and the premium collected are available.** The activities endpoint returns
  option transactions with `option_type` of `SELL_TO_OPEN` / `BUY_TO_CLOSE`, a `trade_date`, and
  a `price` documented as "price per share of the option contract". That is exactly what the
  Schwab adapter's `_opening_trades` reconstructs today.
* **Broken connections are detectable properly.** A `CONNECTION_BROKEN` webhook (HMAC-SHA256 over
  the body, keyed by the consumer key), plus a `disabled` field on the connection to poll. This is
  *better* than what the app does today, which infers link health from the token file's creation
  timestamp plus seven days.
* **One caveat that touches a shipped feature.** SnapTrade's position `price` is "last known
  market price… freshness depends on the brokerage", and they recommend using your own market data
  instead. Short puts are already re-priced from the chain, so they are unaffected; the `idle`
  covered-call verdict is the one thing that leans on the broker's mark (`c.mark`), and it would
  either need pricing from the chain too or accept a stale figure.

### The cost is a cliff, not a ramp

| Tier | What it includes |
|---|---|
| Build (free) | **5 connected accounts**, real-time data, trading |
| Launch | **$100/month** + $2 per connected user/month real-time, or $1 daily |

So friends are free up to five and friend number six costs $100/month. If the answer to
"friends or public" is public, SnapTrade is a $100/month floor. Two things to confirm with them
before building: whether "5 connected accounts" counts *people* or *brokerage accounts* (a friend
with a taxable account and an IRA may be two), and whether the free tier reaches real brokers or
only their sandbox — the getting-started page says production keys need "approval and billing
steps".

Broker access, from their guide: **Interactive Brokers** read-only immediately on production
approval; **Fidelity** read-only after ~2 business days of broker approval; **Schwab** read-only
on production approval, with trading requiring your own Schwab keys registered directly. (The
brief's "~2 weeks" for Schwab read-only looks like the older figure.)

### What SnapTrade does not fix

**Schwab's refresh token still expires in seven days.** That is Schwab's rule, not an artefact of
going direct, so a Schwab connection through SnapTrade also goes `disabled` and needs the
reconnect flow. SnapTrade makes the repair one link instead of code you own, and it removes the
weekly step entirely for brokers whose tokens live longer (IBKR, Fidelity) — but a friend on
Schwab still re-links weekly.

### Still: SnapTrade, not direct

Going direct to Schwab for other people needs **Commercial** keys (the Individual keys cover your
own accounts), and it means holding each person's access token (~30 min) and refresh token (7
days) and handling every refresh. The weekly relink is already the most annoying thing about this
app for one person; multiplied by friends, with an eviction bug in the middle, it is the feature's
death. And each additional broker would be built from scratch.

Keep the direct Schwab adapter for the owner's own account until SnapTrade is proven against a
real account — both satisfy the same port, so this costs nothing but a branch in
`_build_accounts`.

---

## 4. Login: passkeys, with the parts worth deciding early

The brief chooses passkeys plus one-time invite links, and that is right — mostly because of what
already exists. `SessionStore` does what a passkey login needs, `oauth_state` does what a
challenge needs, `py_webauthn` does the crypto, and there is no password to store or reset.

The practical points the brief leaves out:

* **The relying-party id is the hostname.** `steadybull.net`, HTTPS via Caddy, already true. A
  passkey registered there does not work on an IP or a differently-named staging host. Local
  development works because WebAuthn treats `localhost` as a secure origin.
* **No usernames means the credential must be discoverable** — a resident key, with
  `userVerification: "required"` — so the browser can offer it with nothing typed.
* **Recovery is a decision, not a detail.** A friend who loses their only device with no synced
  keychain is locked out. For a friends-only site the cheapest answer is that you re-issue an
  invite; that should be chosen deliberately now rather than discovered later. Allowing a second
  passkey per account (a laptop as well as a phone) is nearly free and worth doing from the start.
* **The invite link is a bearer credential** — single use, short TTL, consumed on use. That is the
  `oauth_state` code again.
* **The risky change is the middleware, not the crypto.** The site-wide Basic-Auth gate becomes a
  session gate over every path with a small exempt list (`/health`, `/static`, the invite and
  login endpoints). Get the exempt list wrong and something is public that should not be. Copy
  `_needs_portfolio_session`, which already denies by default and matches exemptions exactly.

Tailscale would remove all of this if the answer is genuinely friends-only-forever, at the cost of
every friend installing a VPN, a worse phone experience, and no path to opening the site. Not
recommended: a passkey is one Face ID tap and forecloses nothing.

---

## 5. Database: SQLite or Postgres

The brief says Postgres with row-level security. **Recommendation: stay on SQLite for now**, and
revisit if the answer to §6 is "public".

There are already three SQLite databases in WAL mode on a 2 GB droplet. Postgres means a
container, a migration, and a backup story — and **there are no backups today at all** (issue
#66). Row-level security buys one thing: the database refuses a query that forgot its filter. At
this size the same bug is caught more cheaply by the single choke point of §2.1 plus the brief's
own two-user leak test, which is the thing that actually finds it.

Be honest about the trade, though: RLS catches that bug a layer lower than any test, the cost of
migrating grows with the data, and both the web app and cron already write to these files. If this
is going public, Postgres and RLS are the right answer and doing it first is cheaper than doing it
later.

Either way, **backups become a precondition rather than a P1 nice-to-have**. Losing the droplet
today loses the jobs DB and the broker link. With users it would lose the user table and every
passkey — that is everyone's account, unrecoverable.

Tables, as far as this investigation got:

```
users(id, display_name, is_admin, created_at)
invites(token, created_by, expires_at, used_at, user_id)          -- single use
credentials(id, user_id, public_key, sign_count, transports, …)   -- passkeys, public key only
sessions(token, user_id, expires_at, created_at)                  -- + user_id, - broker
broker_links(user_id, provider, external_user_id, secret_encrypted, connection_id, disabled, …)
one_time(token, purpose, expires_at)                              -- oauth_state, generalised
```

On encrypting the SnapTrade user secret: worth doing, but the honest reason is backups, not
attackers. The Schwab token sits unencrypted on the volume at mode 0600 today, which is fine for
one owner on a box they control — and encryption at rest defends a leaked snapshot, not somebody
who has the machine. Since backups are about to exist, encrypt it, with the key in the environment
rather than the database.

---

## 6. The fork: friends, or the public — **friends now, decide later**

The brief names this as its open question, and it is the one decision that changes the work rather
than the wording. **Decided: build for friends, on SQLite, and accept that going public later means
a Postgres migration with more data in it than today.**

| | Friends (≤5 connections) | Public |
|---|---|---|
| SnapTrade | free | $100/month floor + per user |
| Database | SQLite plus a leak test | Postgres plus row-level security |
| Data licences | still needed (§7), smaller exposure | needed, and priced as commercial |
| Screens | one scheduled screen for everyone | per-user quotas, probably a bigger box |
| Invites | you hand them out | self-service signup, abuse handling |

---

## 7. The licences are not optional, and the exposure exists today

Checked 2026-09-25:

* **FMP**, personal licence terms: a personal licence is for the customer's "own personal,
  non-business and non-commercial purposes", and the customer "may not share… permit other users
  access to our Services through the Customer's account, integrate the Data or Services into any
  tools or applications accessible by any third parties, or use the Services to host, share,
  display, or provide content." Displaying FMP data to other people needs a separate data-display
  and licensing agreement.
* **Alpaca**, terms and conditions: content is "provided exclusively for personal and
  noncommercial access and use", and no part of it may be "publicly displayed… or distributed".

steadybull.net is already public and already shows data derived from both. v3 does not create that
exposure — it is there now — and putting the site behind invite-only login **reduces** it without
removing it. So the sequencing is: either price the commercial or display licence, or gate the
site. §1 recommends gating it today for an unrelated reason, which happens to be the cheaper half
of this too.

The brief's other point stands and is outside what this investigation can answer: giving other
people trade picks can count as investment advice, and that wants a real opinion before the invites
go out.

---

## 8. Build order

**Phase 0 — close the slot. Done, not deployed.** `AUTH__SCOPE=portfolio` plus `AUTH__PASSWORD` in
the droplet's `.env`. See §1.

**Phase 1 — identity.** `users`, `invites`, `credentials`, `user_id` on `sessions`; passkey
registration and login; the password gate over `/portfolio` replaced by a passkey session. No broker
change: the owner's Schwab link keeps working, now owned by user #1.

Phase 0's scoped gate makes this smaller than the investigation first assumed. Because the screener
stays public, the passkey session only ever has to cover `/portfolio` — which is a prefix that
already has a deny-by-default gate with a tested exempt list. The risky part of §4, rewriting a
site-wide middleware, largely goes away: the gate keeps its shape and the password it checks becomes
a passkey session.

**Phase 2 — the seam.** `PortfolioService` per request, `ScreenerService` with no user-bound
field, both caches keyed by user and bounded, `revoke_broker` narrowed to one person, the
scheduled screen made canonical for the Close? column, ownership and a TTL on job results (#64),
and the two-user leak test — two live sessions, every `/portfolio` route, asserting that the
second visitor never sees the first one's numbers. That test is the one that matters: **both live
bugs in the Close? column were in the wiring rather than the logic, and neither was caught by a
unit test.**

The cache keying is worth pulling out of Phase 2 and doing first: keyed by session token it is
correct today and needs nothing else to exist.

**Phase 3 — SnapTrade.** `adapters/snaptrade/` behind `BrokerageAccountProvider`, the encrypted
`broker_links` table, connect/reconnect/disconnect routes, the `CONNECTION_BROKEN` webhook.
Proven against one real account before any invite goes out.

**Phase 4 — the preconditions.** Backups (#66), the licence decision, and then invites.
