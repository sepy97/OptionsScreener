# Portfolio tab — plan

**A living document.** Update it as decisions land and phases complete; the Status section is the
part that goes stale fastest. Target release: **v2.0.0**.

**Status:** planning. Nothing implemented.

| Decision | State |
|---|---|
| Auth posture for account data | **OPEN — blocks everything** |
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
- [ ] **P2 — server-side OAuth.** connect / callback / disconnect, state verification, token on the
      volume.
- [ ] **P3 — the tab.** Templates, the capacity / puts / shares views, and the never-connected and
      expired states.
- [ ] **P4 — ops.** Token expiry in `/health` and `doctor`, docs, backup posture.
- [ ] **P5 — cross-links** into screener and search.
- [ ] **v2.0.0 release.**

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
