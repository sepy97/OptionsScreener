# Multi-user access + broker linking — plan

**Goal:** let other people use the options screener, each linking their own brokerage accounts, with no way to see each other's data.

**Open question:** only friends, or open to the public? This decides whether Tailscale is an option (section 1), and how much the license and legal checks in section 5 matter.

---

## 1. Login: passkeys, no passwords

Nobody creates a password, and the server never stores one.

**How it works for the user**
1. You send them a personal **invite link** (one-time use). Opening it creates their account, with no form and no email to type.
2. The page asks them to save a **passkey**, which is one Face ID / Touch ID tap. It syncs through iCloud Keychain or Google Password Manager, so it works on their other devices.
3. **Long sessions** (months). When they do need to sign in again, the browser offers the passkey on its own (no username typed).

**How to build it**
- Use an existing passkey (WebAuthn) library, e.g. `py_webauthn`, or a login service. Don't write the crypto yourself.
- The server stores only the **public key** of each passkey. A stolen database can't be used to log in.

**Friends-only alternative: Tailscale**
- Each person installs the Tailscale app once. After that, there's no login on the site: Tailscale tells the app who is visiting.
- Plain WireGuard (current router setup) keeps strangers out but does **not** tell the app *who* each person is, so it isn't enough by itself.

**Don't**
- Use the broker login as the site login. Schwab tokens expire every 7 days, and it breaks once someone links a second broker.
- Use "invisible" cookie-only accounts. A cleared cookie means a lost account, and a copied cookie exposes someone's brokerage data.

---

## 2. Broker linking via SnapTrade

SnapTrade is a middleman. It holds the broker connections; the app holds one small secret per user.

**Broker access** (as of Sep 2026, check the Broker Access Guide for changes)

| Broker | Access | Notes |
|---|---|---|
| Interactive Brokers | Read-only | Available right after SnapTrade approval |
| Fidelity | Read-only | ~2 business days |
| Schwab | Read-only | ~2 weeks |
| Schwab | Trading | Needs my own Schwab **Commercial** API keys (the current Individual keys cover only my own accounts) |

The free plan includes up to 5 brokerage connections.

**What the user sees**
1. Taps "Connect broker".
2. SnapTrade's page opens, and they pick their broker. The first time, they also accept SnapTrade's terms.
3. They're sent to the broker's real site, log in there, and approve access. **I never see their broker password.**
4. They land back on my site, and their accounts show up.
5. If a connection breaks, they repeat steps 2–4. They can disconnect anytime.

**What I do**
1. **Setup, once:** get the SnapTrade API key (client ID + secret key). It lives in server secrets, never in code or the database.
2. **First connect:** register the user with SnapTrade using my **internal user ID**, not their email, since it must never change. SnapTrade returns a random **user secret**. Store it encrypted.
3. **Connect:** ask SnapTrade for a login link (user ID + user secret), then redirect the user to it. Options: pre-select the broker, choose where they land afterward, or reconnect a broken connection by its ID.
4. **Use:** list accounts and pull holdings/positions with the user ID + user secret, always server-side (Python SDK available).

**Not chosen: going direct to Schwab.** That would mean storing each user's access token (~30 min) and refresh token (7 days), handling the refreshes myself, and building each broker separately.

---

## 3. What the server stores

| Table | Contents |
|---|---|
| users | internal user ID, display name |
| passkeys | user ID, passkey public key |
| sessions | random session ID → user ID, expiry (browser holds the cookie) |
| broker_links | user ID → SnapTrade user secret (**encrypted**) |

**Kept outside the database:** the SnapTrade API key, and the encryption key for user secrets (environment variable or cloud key manager). A stolen database alone is then useless.

---

## 4. Keeping users apart

- **The main rule:** always get the user ID and SnapTrade secret **from the session**. Never trust anything the browser sends, like an account ID in the URL.
- **Database:** every private row has a `user_id`. Turn on Postgres **row-level security**, so the database itself blocks reads across users even if a query forgets its filter.
- **Shared vs private:**
  - Shared, one cache for everyone: market data, screener results.
  - Private, always stored and cached per user: accounts, positions, balances, orders.
- **Background jobs:** one job per user, using only that user's secret.
- **Logs:** never log secrets, tokens, or account numbers.
- **Read-only by default.** Ask for trading access only if it's truly needed.

---

## 5. Before inviting anyone

- **Data licenses:** personal plans from FMP and marketdata.app usually don't allow showing their data to other people. Check terms, or upgrade.
- **Legal:** giving others trade picks, or placing trades for them, can count as investment advice. Check before going beyond friends.

---

## Build order

- [ ] Users + sessions + passkey login + invite links
- [ ] `user_id` on all private tables + row-level security
- [ ] SnapTrade free plan: register a user, connect a broker, list accounts and holdings
- [ ] Encrypt the stored user secrets; key kept outside the database
- [ ] Split the screener: shared market data vs per-user positions
- [ ] Handle broken connections (reconnect link)
- [ ] Leak test with two users: try to reach A's data while logged in as B, e.g. by changing IDs in URLs
- [ ] Check data license terms
- [ ] Apply for Schwab Commercial keys (only if trading is needed)

## Links

- SnapTrade Getting Started: https://docs.snaptrade.com/docs/getting-started
- SnapTrade Broker Access Guide: https://docs.snaptrade.com/docs/broker-access-guide
- Schwab Trader API – Commercial: https://developer.schwab.com/products/trader-api--commercial
