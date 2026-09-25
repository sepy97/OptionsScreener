# Put swap rule

> **IMPLEMENTATION NOTE (added by the repo, not part of the spec).** The code follows this
> document with two deliberate departures, both made after the rule flagged a put sold days
> earlier: the comparison is made at the open put's own TENOR rather than at the best-paying
> expiry in the entry window, and a put must first be paying under 15%/yr to be considered used
> up at all. Reasoning and evidence: `PORTFOLIO_PLAN.md`, phase P5b.


When to buy back an open short put early and put the cash into a new one.

Status: draft. The two limits are starting values and have not been backtested yet.

## Purpose

An open put can get "used up": the stock moves up and away from the strike, the put becomes nearly worthless, and the locked cash earns almost nothing for the rest of its life. This rule spots those puts and suggests what to open instead.

It must not fire all the time. There is always some put with a better yield, so the rule has limits that stop constant swapping.

This rule does not define entry rules. It uses whatever the project's entry rules are (stock screen, delta limit, days to expiry, no earnings before expiry, per-stock cap, enough free cash).

## Scope

- Applies to: open short puts where the stock price is above the strike.
- Does not apply to: puts where the stock is at or below the strike. Those go to the assignment logic (take the shares, sell calls).
- Covered calls: not decided. Out of scope for now.
- Losing puts need no special case. Their yield is high, so the rule never fires on them.

## Definitions

All yields are yearly and written as decimals (0.22 means 22%).

```
yield        = price / strike * 365 / days_to_expiry
old_yield    = yield of the open put, using its ASK price (what you pay to buy it back)
new_yield    = yield of a candidate put, using its BID price (what you get for selling it)
cash         = strike * 100 * contracts        (of the open put)
extra        = (yardstick - old_yield) * cash * days_left / 365 - swap_cost
```

Using the ask for the old put and the bid for the new one builds most of the trading cost into the comparison. It also makes thinly traded options look worse, which is correct.

`extra` is the extra premium the swap collects over the old put's remaining days. It is premium, not profit: part of it pays for the extra risk of the new put.

## The yardstick

The open put is always measured against one specific put, the yardstick.

1. **Same ticker first.** The yardstick is the one put the entry rules would open on the same ticker today. Use one fixed pick per ticker, not the best of several. Otherwise the yardstick always drifts to the riskiest allowed strike.
2. **Fallback.** If the same ticker has no valid pick (earnings before the new expiry, ticker dropped off the screen, per-stock cap reached), use the middle (median) yield of all valid picks across the list. Never use the best yield on the list.
3. **No valid picks at all.** Keep the old put.

Why same ticker: same stock, same risk. A yield gap can then only mean the old put is used up. A jumpy stock elsewhere on the list cannot trigger a swap.

Side effect, on purpose: each ticker gets its own line. A volatile stock has a high fresh yield, so its swap fires earlier. A calm stock fires later.

## The rules

A put is flagged "swap" only if both pass, measured against the yardstick.

| # | Rule | Default | What it stops |
|---|---|---|---|
| 1 | `yardstick >= MIN_RATIO * old_yield` | `MIN_RATIO = 2.0` | Swapping right after opening. On day one a put's yield is about equal to the yardstick, so nothing can be 2x better. |
| 2 | `extra >= MIN_EXTRA` | `MIN_EXTRA = $100` | Swaps on small positions, and on puts with few days left where the cash frees itself soon anyway. |

Rule 3 is the yardstick choice above: both tests use the same-ticker put, not the best put on the list.

## Output

- **keep**, with the reason (which rule failed).
- **swap**, with a list of suggestions:
  1. the same-ticker put, if there is one
  2. the top `TOP_N` other tickers by yield

All suggestions must pass the entry rules. Other tickers never affect whether to swap, only what to swap into.

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `MIN_RATIO` | 2.0 | Rule 1 multiple |
| `MIN_EXTRA` | 100 | Rule 2 minimum, dollars |
| `SWAP_COST` | 10 | Dollars per swap: commission plus any bid/ask loss not already in the prices |
| `TOP_N` | 3 | Other-ticker suggestions to show |

## Worked example (positions of 2026-09-21)

No option chain data was at hand, so the yardstick for every ticker was set to 0.219, the average yield of the three puts opened that day. Old yields use the mid price from the positions file, not the ask.

| Put | Days | Old yield | Rule 1 (line = 0.11) | Extra | Result |
|---|---|---|---|---|---|
| CRDO 130 | 25 | 0.062 | pass | $130 | swap |
| TER 290 | 25 | 0.069 | pass | $288 | swap |
| SCCO 170 | 25 | 0.125 | fail | - | keep |
| LRCX 270 | 4 | 0.184 | fail | $0 | keep |
| AVGO 390 | 4 | - | - | - | out of scope, stock below strike |

## Reference code

```python
from statistics import median

MIN_RATIO = 2.0    # rule 1: yardstick yield must be at least this many times the old yield
MIN_EXTRA = 100.0  # rule 2: minimum extra premium in dollars, after swap cost
SWAP_COST = 10.0   # dollars per swap: commission plus bid/ask loss not already in the prices
TOP_N = 3          # how many other-ticker suggestions to show


def put_yield(price, strike, days):
    """Yearly pay rate on the locked cash, as a decimal (0.22 = 22%)."""
    return price / strike * 365 / days


def review(old, picks, min_ratio=MIN_RATIO, min_extra=MIN_EXTRA,
           cost=SWAP_COST, top=TOP_N):
    """Decide keep or swap for one open short put.

    old:   {"ticker", "strike", "days", "ask", "contracts", "stock_price"}
    picks: {ticker: {"yield": float, ...}} - the ONE put the entry rules would
           open on each ticker today, yield computed at the bid.
    Returns a dict with the decision, the numbers behind it, and suggestions.
    """
    out = {"action": "keep", "reason": "", "suggestions": []}
    if old["stock_price"] <= old["strike"]:
        out["reason"] = "stock at or below strike: handled by assignment logic"
        return out
    if old["days"] < 1 or not old.get("ask") or not picks:
        out["reason"] = "no usable price, no days left, or no candidates"
        return out

    same = picks.get(old["ticker"])
    if same:
        yardstick, source = same["yield"], "same ticker"
    else:
        yardstick, source = median(p["yield"] for p in picks.values()), "list median"

    old_yield = put_yield(old["ask"], old["strike"], old["days"])
    cash = old["strike"] * 100 * old["contracts"]
    extra = (yardstick - old_yield) * cash * old["days"] / 365 - cost
    out.update(old_yield=old_yield, yardstick=yardstick,
               yardstick_source=source, extra_premium=extra)

    if yardstick < min_ratio * old_yield:
        out["reason"] = "rule 1 failed: yardstick is less than %.1fx the old yield" % min_ratio
        return out
    if extra < min_extra:
        out["reason"] = "rule 2 failed: extra premium below $%.0f" % min_extra
        return out

    others = sorted((t for t in picks if t != old["ticker"]),
                    key=lambda t: picks[t]["yield"], reverse=True)[:top]
    out["action"] = "swap"
    out["reason"] = "both rules passed"
    out["suggestions"] = ([old["ticker"]] if same else []) + others
    return out
```

Checked against the worked example above: CRDO and TER return swap ($130, $288), SCCO and LRCX return keep on rule 1, AVGO returns keep as out of scope, and removing TER from `picks` falls back to the list median.

## Open decisions

1. **Which suggestion the bot opens.** While trading by hand, the person chooses. The bot needs a fixed pick rule, for example "same ticker unless another is clearly better". Settle in the backtester.
2. **Tune `MIN_RATIO` and `MIN_EXTRA`.** Both are starting values.
3. **Test against holding to expiry.** An earlier backtest had hold-to-expiry beating "close at 50% or 21 days". This rule must beat plain holding in the backtester before it goes live.
4. **Highest yield is usually highest risk.** Among other-ticker suggestions, the top yield often means the market sees trouble in that stock. The entry rules and per-stock cap are the guard; check they are enough.

## Ideas looked at and dropped

- **"Premium kept" percent** (Schwab's Gain %). Looks backward: depends on the price you sold at, which the market does not care about. Ignores days left.
- **Greeks as criteria.** Delta adds nothing here (a low-yield put is always far from the strike). The daily decay number overstates what a far-away put will still earn, by 2x or more. All of them come from the broker's pricing model and get noisy on thinly traded puts. Price, strike and days are hard numbers.
- **Fair-price formula** (strip out the part of premium that pays for risk, using the measured overpricing of options). More correct, but complicated and sensitive to one estimated input. Its main result was that "about 2x" is the right size for rule 1, which is why `MIN_RATIO` starts at 2.0.
