# UI status — preliminary

**The current web UI is preliminary.** It was built functionally-first to make the screener
usable in a browser; its visual design and UX are intentionally minimal and **will be reworked**
in a later, dedicated effort.

## What exists today (M3.2)
- A server-rendered **HTMX** UI on the FastAPI app (`src/wheel_screener/api/`), styled with
  classless **Pico.css**. No build step, no SPA framework; htmx + pico are vendored.
- Dashboard: a run-screen form, a live progress funnel (poll-until-done), a sortable results
  table with per-candidate detail expand, and the latest precomputed run with a staleness note.

## Why it's preliminary
- **Functional-first.** The goal of M3.2 was a working, testable UI over the existing engine —
  not a polished product surface.
- **Placeholder styling.** Pico gives reasonable defaults for zero design effort, but it is not a
  considered visual language.
- **Minimal UX.** One screen, one flow. No saved screens/watchlists, charts, comparison view,
  responsive/mobile pass, or accessibility review yet.

## What a future rework should consider
- A deliberate visual design (layout, typography, dark mode, branding).
- Richer UX: saved screens / watchlists, candidate comparison, inline charts (price, IV), and the
  quick-filter that was deferred from M3.2 PR-D.
- A frontend-approach decision: stay server-rendered (HTMX + a real CSS system such as Tailwind) or
  move to a SPA. The architecture separates the engine from delivery, so either is viable — the
  same API can also back the planned Swift/native client. See [`WEB_PLAN.md`](WEB_PLAN.md).
- Accessibility (ARIA, keyboard nav, contrast) and responsive/mobile.

## Scope note
This does **not** block deployment. The preliminary UI is good enough to run privately; the rework
is a separate effort to schedule after auth + deploy (M3.3–M3.5) are in place.
