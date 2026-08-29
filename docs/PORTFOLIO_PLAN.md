# Portfolio tab — plan

**A living document.** Update it as decisions land and phases complete; the Status section is the
part that goes stale fastest. Target release: **v2.0.0**.

**Status:** planning. Nothing implemented.

| Decision | State |
|---|---|
| Auth posture for account data | **decided: Sign in with Schwab** (section 1a) |
| Multi-broker support | designed for from day one, Schwab implemented first (section 1b) |
| Schwab callback URL changed in the developer portal | not started |
| Read-only invariant | agreed (see Security) |
| Release label | v2.0.0 |

---

## What it is

A fourth tab showing the current state of the linked Schwab account: what cash is available, what
short puts are open, what shares are held, and how much collateral is already committed — with a
single-click reconnect, because Schwab makes that a weekly event.

---

## 1. The blocker: this cannot ship on a public instance

`docker-compose.yml` currently says, on purpose:

```yaml
AUTH__REQUIRED: "false"
AUTH__PASSWORD: ""     # PUBLIC instance: the login gate is intentionally OFF
```

A Portfolio tab publishes brokerage balances and positions to anyone with the URL. That is not a
tuning problem; it is a hard incompatibility with the current posture, and nothing else here
matters until it is resolved.

Two sane answers:

- **Re-enable the gate site-wide** — delete those two lines, set `AUTH__PASSWORD` in the droplet's
  `.env`. Simplest, and reverses the v1.1.0 "make it public" decision.
- **Gate only `/portfolio*`** — keep the screener shareable, protect the account routes. More code,
  and the callback route must be gated too since it carries the authorization code.

The second is preferable if the public screener is still wanted. Either way this is decided before
any code is written.

### Does the Schwab login itself count as the tab's auth?

Tempting — show a Connect page, let Schwab do the authenticating — but on its own **no**, and the
reason is worth writing down because the idea looks sufficient.

Two different authentications hide in "auth for this tab":

| | what it proves | who it is for |
|---|---|---|
| Schwab OAuth | the **app** may read the account | Schwab |
| Site auth | the **visitor** is the owner | this site |

A Connect page does the first. The gap is what happens after the first successful connect: the
refresh token now lives on the server, so `/portfolio` renders positions for **anyone who loads the
URL**. The Connect page only appears while disconnected — a stranger arriving the next day doesn't
meet a login, they meet the balances. Schwab OAuth gates *connecting*, not *viewing*.

**It can be made to work** by having the callback issue a signed **session** rather than only
storing a token — "Sign in with Schwab":

```
GET /portfolio          no session cookie -> the Connect page
  click -> Schwab login (requires the owner's Schwab credentials)
  -> callback -> store token AND set a signed session cookie
  -> /portfolio -> positions
```

A stranger who clicks Connect is sent to Schwab and stops there. The property wanted — only the
account owner sees the account — falls out correctly, and the weekly reconnect that the 7-day token
already forces *becomes* the login, with no second password to invent.

The cost is real auth code: a cookie-signing secret, a session expiry policy (tied to the token, or
independent), binding the session to the account hash so a session issued for one account cannot
view another, and mandatory `state` verification.

**Decided: Sign in with Schwab**, with no Basic-Auth interim. The interim was proposed to keep the
risky code small; it was dropped in favour of building the real thing once. The rest of this
document assumes it.

The gate must also cover **`/portfolio/schwab/callback`** — that route carries the authorization
code.

---

## 1a. Sign in with Schwab — implementation

### Two layers, deliberately separate

| Layer | Answers | Lifetime |
|---|---|---|
| **Session** | may this *visitor* see the portfolio? | capped at the link's expiry |
| **Link** | which *broker account* is connected, with what credentials? | Schwab: 7 days |

Conflating them is the mistake section 1 describes. Keeping them apart is also what makes other
brokers possible: a session can be minted by any broker that authenticates a human, while a link
merely needs credentials from somewhere.

### Verified mechanics

Checked against the installed schwab-py 1.5.1, not assumed:

```python
get_auth_context(api_key, callback_url, state=None)   # -> AuthContext(callback_url, authorization_url, state)
client_from_received_url(api_key, app_secret, auth_context, received_url, token_write_func)
```

`client_from_received_url` reads only `auth_context.callback_url` and `auth_context.state`, and the
library's own source notes the OAuth client "cannot be passed around". **So only the `state` string
has to survive the redirect** — the context is reconstructible. No PKCE verifier to persist, no
server affinity, and an app restart mid-flow costs one re-click rather than a wedged state.

### The state machine

```
no session cookie                 -> Connect page (list of linkable brokers)
session + link healthy            -> the portfolio
session + link expired            -> the portfolio chrome, with Reconnect
session + no link (disconnected)  -> Connect page
```

Session and link expire independently, so all four states are reachable and each gets a real
rendering. "Link expired" in particular must never surface as an error page: it is the normal
weekly condition.

### Sessions

- **Server-side, in SQLite on the volume** (a `sessions` table beside the jobs DB), not a stateless
  signed cookie. Stateless is less code, but revocation is the point: Disconnect has to actually end
  the session, and a server-side row makes that true rather than approximately true.
- Cookie carries an opaque random id only. `HttpOnly`, `Secure`, `SameSite=Lax` — Lax rather than
  Strict because the callback arrives as a cross-site top-level redirect from Schwab.
- Signed with `AUTH__SESSION_SECRET`. **Fail closed**: with the portfolio enabled and no secret set,
  the app refuses to start, exactly as `AUTH__REQUIRED` already does.
- **Expiry is capped at the link's expiry.** One clock to reason about, and it makes the weekly
  reconnect *be* the re-login rather than a second thing to remember.
- Bound to the linked account fingerprint, so a session minted for one account cannot read another
  connected later.

### CSRF and the `state`

`state` is random, single-use, stored server-side with a short TTL, and verified on callback. An
unrecognised or reused `state` is rejected outright. Without this the callback is a place to point
someone else's browser.

### Security specifics

- **The authorization code appears in the callback URL, and Caddy logs request URLs.** That puts a
  live credential in the access log. Mitigate by suppressing query strings for that path (or the
  route entirely) in the Caddyfile — cheap, and easy to forget until it is in a log shipper.
- Tokens live on the mounted volume, `0600`, uid 10001. Never in the image, never in a log line.
- The connect and callback routes are rate-limited like the other expensive endpoints.
- Disconnect revokes the session row **and** deletes the stored token; neither alone is enough.
- Read-only invariant unchanged: only GET endpoints, ever.

---

## 1b. Supporting other brokers

The port is broker-neutral from the start; only Schwab is implemented. What actually varies:

| Broker | How a link is established | Can it mint a session? |
|---|---|---|
| Schwab | OAuth2, 7-day refresh | yes |
| E*TRADE | OAuth 1.0a | yes |
| Tastytrade | username/password -> session token | yes |
| Alpaca | API key/secret, configured server-side | **no — no human authenticates** |
| IBKR | self-hosted gateway | probably not deployable here |

**The load-bearing consequence:** a key-based broker cannot be the login, because nobody proves who
they are — the credentials are just sitting in the server's config. So broker-as-login requires at
least one OAuth-capable link. If a key-only broker is ever the sole integration, a password gate has
to come back for the tab. Worth knowing before the abstraction implies otherwise.

### Ports

```python
class BrokerageAccountProvider(Protocol):      # reading, broker-neutral
    def accounts(self) -> list[BrokerageAccount]: ...
    def positions(self, account_id: str) -> list[Position]: ...
    def link_status(self) -> LinkStatus: ...    # connected? expires when?

class OAuthBrokerLink(Protocol):               # only brokers that authenticate a human
    def authorize_url(self, state: str) -> str: ...
    def complete(self, received_url: str, state: str) -> LinkedIdentity: ...
    def revoke(self) -> None: ...
```

Splitting the reading port from the linking port is what keeps a key-based broker implementable: it
provides the first and simply does not provide the second.

### Storage

Per-broker rather than the current single file: `/data/links/{broker}.json`, so a second broker is
additive rather than a migration. `SCHWAB__TOKEN_PATH` keeps working for the existing CLI flow.

### Normalisation

Adapters translate into broker-neutral `BrokerageAccount` and `Position`, with options carrying
underlying / strike / expiry / right / DTE as fields rather than a broker's own symbol format. The
normalising is the adapter's job precisely because every broker spells it differently.

Multi-user is explicitly **out of scope**: one operator, one session at a time, no user table.

---

## 2. What "one click" actually is

schwab-py 1.5.1 exposes exactly the primitives for a server-side flow (verified against the
installed version):

```python
get_auth_context(api_key, callback_url, state=None)              # -> authorize URL + state
client_from_received_url(api_key, app_secret, auth_context, received_url, token_write_func)
```

So the flow really is one click:

```
[Connect Schwab] -> 302 to Schwab -> user authenticates there
                 -> Schwab redirects to /portfolio/schwab/callback?code=…&state=…
                 -> token written to /data/schwab_token.json -> back to /portfolio
```

**External prerequisite, likely the longest pole:** the Schwab app registers
`https://127.0.0.1:8182` today. It needs `https://steadybull.net/portfolio/schwab/callback`.
Schwab app edits can require re-approval, so start this early. Check whether **both** callbacks can
be registered — otherwise the local `auth-login` flow stops working.

---

## 3. The seven-day reality

Schwab refresh tokens expire after 7 days. There is no way around it, so it is designed for rather
than treated as an error:

- the tab shows connection state **and days remaining**, not just "connected"
- expiry renders a **Reconnect** button, never a stack trace
- `/health` and `wheel-screener doctor` report token age, so it is visible before it bites
- a banner from roughly day 5

"One click" is not one-time setup — it is a weekly click, and the UI treats that as normal.

---

## 4. Architecture

Follows the shape already used for chains and for fundamental reports.

| Layer | Piece |
|---|---|
| Port | `BrokerageAccountProvider` — `accounts()`, `positions()`, `auth_status()` |
| Adapter | `adapters/schwab/account.py` over `get_account_numbers()` + `get_account(hash, fields=POSITIONS)` |
| Models | `BrokerageAccount` (cash, buying power, equity) · `Position` (equity and option legs) |
| Service | `portfolio()`, returning `None` when not connected |
| Routes | `GET /portfolio` · `GET /portfolio/schwab/connect` · `GET /portfolio/schwab/callback` · `POST /portfolio/schwab/disconnect` |

Schwab allows ~120 req/min; a portfolio view is 2 calls. Cache briefly (30–60s) so a refresh spree
can't burn the budget.

---

## 5. What to show

Generic position tables are the boring version. The valuable view is wheel-shaped:

- **Capacity** — cash and buying power minus collateral already committed. Answers "how much more
  can I sell?"
- **Open short puts** — strike, expiry, DTE, collateral tied up, current value, P/L, and whether
  spot sits below strike (assignment watch), sorted by DTE
- **Shares held** — cost basis, with >=100-share lots flagged as covered-call candidates
- **Cross-links into the screener** — mark screener rows for symbols already held so positions
  aren't doubled up unknowingly, and surface 100-share lots in the covered-call side of search

That last item is what makes this more than a second broker UI: the portfolio makes the screener
smarter. It is also the part most likely to be cut for time, so it is scheduled explicitly (phase 5)
rather than left as "later".

---

## 6. Security

- **Read-only by construction.** Only GET endpoints, ever. Stated as an invariant because the token
  Schwab issues is trading-capable: whoever holds that file can place orders.
- **Token lives on the mounted volume** (`/data/schwab_token.json`, uid 10001, mode 0600). Never in
  the image, never in a log line, never in a backup that leaves the box.
- **CSRF is real here.** The `state` parameter must be generated server-side, stored, and verified
  on callback, or the callback becomes an open target. In-memory with a short TTL is fine for a
  single user; losing it on restart just means clicking Connect again.
- **This changes what the deployment is.** Today a compromised droplet costs read-only market-data
  keys. Afterwards it costs brokerage access. That is a deliberate step up in blast radius and
  should be taken on purpose, not as a side effect of wanting a nice tab.

---

## 7. Phases

- [ ] **P0 — decisions + Schwab app callback change.** Blocking, with external latency.
- [ ] **P1 — read path.** Port, adapter, models, and a `wheel-screener positions` CLI command.
      Deliberately web-free: it proves the data model against a real account before any OAuth
      plumbing exists.
- [ ] **P2 — sessions + OAuth.** Session store and cookie, `state` issuance and verification,
      connect / callback / disconnect, token on the volume, Caddy log suppression for the callback.
- [ ] **P3 — the tab.** Templates, the capacity / puts / shares views, and the never-connected and
      expired states.
- [ ] **P4 — ops.** Token expiry in `/health` and `doctor`, docs, backup posture.
- [ ] **P5 — cross-links** into screener and search.
- [ ] **v2.0.0 release.**
- [ ] *(later)* **A second broker**, to prove the abstraction is real rather than Schwab wearing a
      trench coat.

---

## 8. Why v2.0.0

The project's rule is that MAJOR means *a release needing special care on the droplet*. This one
needs a new secret, a changed auth posture, a Schwab app reconfiguration, and a new persisted
credential on the volume. That is exactly the case the label was reserved for.

---

## 9. What only the operator can do

- decide the auth posture (section 1)
- change the Schwab callback URL in the developer portal, and confirm the loopback one can stay
- put `SCHWAB__CLIENT_ID` / `SCHWAB__CLIENT_SECRET` in `/srv/steadybull/.env`
- set `SCHWAB__TOKEN_PATH=/data/schwab_token.json` in `docker-compose.yml`

---

## 10. Open questions

- Multiple linked accounts: pick one, or show all? `get_account_numbers()` returns every linked
  account.
- Does the assignment watch need live quotes (spot vs strike), or is the position's own market
  value enough? Live quotes mean a chain/quote call per underlying.
- Should the portfolio influence the screen itself (exclude names already held), or only annotate
  the results? Annotating is safer; excluding hides trades the operator may still want.
