# UI vision — the "final goal" for the web interface

The current UI is deliberately preliminary (see [`UI_STATUS.md`](UI_STATUS.md)). This is the
target we're building toward: a clean, focused, non-preliminary interface for the live public app
at **steadybull.net**. Server-rendered FastAPI + HTMX + `custom.css` (no SPA, no heavy JS).

## 1. Shape — two tabs, one calm surface

The two workflows — **run a market screen** and **search one ticker** — are separate tabs, not one
crowded page. A persistent header carries the brand and the two tabs; each is its own bookmarkable
route. A slim, always-present line carries the intro + "not investment advice" disclaimer (no heavy
marketing landing).

```
┌───────────────────────────────────────────────────────┐
│  🎯 SteadyBull        [ Screener ]   Search            │  ← active tab: green underline
└───────────────────────────────────────────────────────┘
   cash-secured-put / wheel screener · not investment advice
```

Routes: `GET /` = Screener (home), `GET /search` = Search, `POST /search` = search action
(fragment), `/runs/*` = the existing background-job endpoints (unchanged).

## 2. Screener tab (`/`)

```
Run a screen                                     what is this? ⌄
┌─ Criteria ─────────────────────────────────────────────┐
│  DTE  [21]–[35]      Rank:  Yield ·(Balanced)· Quality   │
│  ▸ Option filters   Δ target/max · IV · OI · spread · $  │  ← adjustable criteria
│  ▸ Advanced         top-N · min-yield % · $-volume       │
│                                    [  Run screen  ]      │
└─────────────────────────────────────────────────────────┘

Latest results · precomputed 12m ago   ⟳ re-run    ⭳ Export
┌ Sym   Strike   Exp / DTE    Yield    Strength   Score ┐   ← 9 focused cols (Bid/Mid/OI → detail)
│ CALM  $52.50   Aug 21·37d   31.2%    95/100     ▰▰▰▰▱ │   ← score green→red; $ + % formatting
│ TNK   $18.00   Aug 21·37d   28.4%    88/100     ▰▰▰▰▱ │   ← left accent stripe on strong rows
```

Precomputed vs. a fresh run are visibly distinct (a "your run" area appears below on submit) so
stale-vs-live is never ambiguous.

## 3. Search tab (`/search`)

```
Search a ticker
[ AAPL                                    ]  [ Search ]     ← faded ghost placeholder

MU · strength 74/100                                        ← two lines, no "N sellable puts"
     81% vs peers   ⚠ earnings Aug 8
┌ Strike   Exp/DTE   Yield   Δ    IV    Breakeven   ... ┐
```

## 4. The "not preliminary" visual layer

- **Score/strength color scale** green→amber→red for at-a-glance scanning.
- **Consistent formatting** everywhere: percentiles as `81%`, prices as `$190`, yields (incl.
  min-yield) as `%`.
- **Candidate detail → a card**, grouped by mental model instead of a bullet list:
  ```
  AAPL · …P00190000 (put)                             [×]
  Contract     $190 · Aug 16 · 37 DTE
  Market       bid $2.10 / mid $2.15 · spread 2.3% · OI 4,120
  Return       premium $2.10 · yield 14.2% · collateral $19,000
  Fundamentals 78/100 strength · 81% peers · 0.61 score
  ```
- **Type + spacing scale** (defined CSS vars), **row-level flags** (earnings-before-expiry dot,
  delta-risk badge), a **visual progress funnel** (Universe → Fundamentals → Chains → Candidates,
  with live counts), **diagnostic empty states** ("your 15% yield floor excludes most names — try
  10%"), and a **mobile** pass (cards on phones, dense table on desktop) + basic a11y (ARIA on the
  nav/tabs, keyboard, contrast).

## 5. Backlog map

Already filed: **#81** landing/intro · **#82** UI-rework (now an epic) · **#87** search header ·
**#88** faded placeholder · **#89** %/$ formatting · **#90** adjustable criteria · **#91** green→red
score. New (split out of #82): **tabs / IA restructure** (the anchor), **candidate-detail card**,
**progress funnel**, **diagnostic empty states**, **row-state flags**, **mobile + a11y**,
**type/spacing scale**.

## 6. Build order

1. **Tabs / IA** — routes + nav + split templates (the structural foundation).
2. **Formatting + score color + search-header + placeholder** (#87–#89, #91) — small high-visibility
   wins, mostly template/CSS.
3. **Adjustable criteria** (#90) — the biggest feature.
4. **Candidate card + progress funnel + empty states + row flags** — polish.
5. **Mobile + a11y + landing/intro** — reach + first impression.
