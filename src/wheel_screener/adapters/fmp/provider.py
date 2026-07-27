"""FundamentalsProvider backed by Financial Modeling Prep (https://financialmodelingprep.com/stable/).

The same provider pythonBot uses. Rating thresholds live in ``core.fundamentals``;
this adapter only fetches + maps FMP JSON into the core models.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from wheel_screener.adapters.errors import map_http_error
from wheel_screener.adapters.fmp.client import FmpClient
from wheel_screener.adapters.fmp.mapper import map_earnings, map_metrics, map_universe_row
from wheel_screener.config import FmpSettings
from wheel_screener.core.errors import ProviderDataError
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying

# FMP's earnings-calendar silently returns a PARTIAL, right-anchored slice — it drops the
# EARLIEST rows, which are exactly the near-term ones the blackout needs. Two independent rules
# do it (measured 2026-07-25, issue #113):
#   * a 4000-row cap  — a denser window returns only the latest 4000 rows;
#   * a ~90-day range clamp — beyond that, `from` is pulled up to `to - 90d`.
# Neither is announced in the payload, and the row count is NOT a reliable signal: a 120-day
# request came back with 3754 rows (under the cap) whose earliest date was 30 days late.
# So we never ask for a wide window; we walk it in small slices and verify the coverage we got.
_EARNINGS_ROW_CAP = 4000
_EARNINGS_CHUNK_DAYS = 7  # peak season fills a 14-day slice past the cap; 7 stays under it
# The documented upstream limit (docs/PLAN.md). Nothing here should come close — every request is
# a chunk — so exceeding it means a caller found a way around the slicing.
_EARNINGS_MAX_RANGE_DAYS = 90

logger = logging.getLogger(__name__)


# Coverage assertion. Clipping removes a CONTIGUOUS block of dates, so a run of empty business
# days is the signature. Three tolerates a holiday-extended weekend; the check is hard only over
# the near term (where the whole market reports every day and where the blackout actually looks)
# and advisory beyond it, since months out the calendar is legitimately sparse.
_MAX_EMPTY_BUSINESS_DAYS = 3
_COVERAGE_STRICT_DAYS = 45


def _first(payload: object) -> dict:
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def _row_date(row: object) -> date | None:
    raw = row.get("date") if isinstance(row, dict) else None
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _longest_empty_run(start: date, end: date, covered: set[date]) -> tuple[int, date | None]:
    """Longest run of consecutive business days in [start, end] with no rows, and where it began."""
    worst, worst_at = 0, None
    run, run_at = 0, None
    day = start
    while day <= end:
        if day.weekday() < 5:  # Mon-Fri
            if day in covered:
                run, run_at = 0, None
            else:
                run_at = run_at or day
                run += 1
                if run > worst:
                    worst, worst_at = run, run_at
        day += timedelta(days=1)
    return worst, worst_at


def verify_calendar_coverage(
    start: date, end: date, covered: set[date], strict_until: date | None = None
) -> None:
    """Raise when the fetched calendar has a hole where trading days should be.

    This is the guard that would have caught issue #113: the upstream endpoint answered a
    120-day request with rows starting 30 days late and no error, so every near-term reporter
    silently vanished from the blackout.

    ``strict_until`` is an ABSOLUTE date, not an offset from ``start`` — the hard check has to
    mean "the next few weeks of the whole request", or a far segment of a long range would be
    judged strictly against a stretch of calendar that is legitimately sparse (holiday weeks
    months out), turning a quiet December into a screen-blocking error.
    """
    strict_until = strict_until or start + timedelta(days=_COVERAGE_STRICT_DAYS)
    strict_end = min(end, strict_until)
    run, at = _longest_empty_run(start, strict_end, covered)
    if run > _MAX_EMPTY_BUSINESS_DAYS:
        raise ProviderDataError(
            f"earnings calendar looks truncated: {run} consecutive business days with no rows "
            f"from {at} (requested {start}..{end}). Refusing to treat it as 'nobody reports'."
        )
    if end > strict_end:
        far_run, far_at = _longest_empty_run(strict_end + timedelta(days=1), end, covered)
        # advisory only: months out, a company simply hasn't announced its date yet
        if far_run > _MAX_EMPTY_BUSINESS_DAYS:
            logger.warning(
                "earnings calendar sparse beyond %dd: %d empty business days from %s "
                "(far-dated earnings are often unscheduled; not fatal)",
                _COVERAGE_STRICT_DAYS, far_run, far_at,
            )


class FmpFundamentalsProvider:
    def __init__(self, settings: FmpSettings, client: FmpClient | None = None) -> None:
        self._settings = settings
        self._client = client or FmpClient(settings)

    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]:
        params = {
            "priceMoreThan": criteria.min_price,
            "priceLowerThan": criteria.max_price,
            "marketCapMoreThan": int(criteria.min_market_cap),
            "exchange": ",".join(criteria.exchanges),
            "isFund": "false",
            "isEtf": "false",
            "isActivelyTrading": "true",
            "limit": 3000,
        }
        rows = self._client.get("company-screener", params)
        if not isinstance(rows, list):
            return []
        return [map_universe_row(r) for r in rows if isinstance(r, dict) and r.get("symbol")]

    def _bulk(self, path: str) -> dict[str, dict]:
        payload = self._client.get(path, {})
        rows = payload if isinstance(payload, list) else []
        return {r["symbol"]: r for r in rows if isinstance(r, dict) and r.get("symbol")}

    def bulk_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        """Cheap pre-rank metrics for the whole universe via the *-ttm-bulk endpoints
        (no sign inputs / DCF — those come from the deep ``fetch_metrics``).

        Returns {} ONLY when the bulk endpoints aren't in the account's subscription
        (verified: lower tiers return HTTP 402/404) so the caller can fall back to a
        capped per-name deep fetch. Any other failure (auth, rate limit, outage) is
        raised as a ProviderError rather than masked as a degraded/empty ranking.
        """
        try:
            ratios = self._bulk("ratios-ttm-bulk")
            key_metrics = self._bulk("key-metrics-ttm-bulk")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (402, 404):
                return {}  # not in subscription -> caller falls back to deep fetch
            raise map_http_error(e) from e
        except httpx.TransportError as e:
            raise map_http_error(e) from e
        out: dict[str, FundamentalMetrics] = {}
        for sym in symbols:
            if sym in ratios or sym in key_metrics:
                out[sym] = map_metrics(ratios.get(sym, {}), key_metrics.get(sym, {}), {}, {}, {})
        return out

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        """Per-symbol deep fetch (incl. EPS / equity / EBITDA sign inputs + DCF)."""
        out: dict[str, FundamentalMetrics] = {}
        for sym in symbols:
            try:
                ratios = _first(self._client.get("ratios-ttm", {"symbol": sym}))
                key_metrics = _first(self._client.get("key-metrics-ttm", {"symbol": sym}))
                income = _first(self._client.get("income-statement", {"symbol": sym, "limit": 1}))
                balance = _first(
                    self._client.get("balance-sheet-statement", {"symbol": sym, "limit": 1})
                )
                dcf = _first(self._client.get("discounted-cash-flow", {"symbol": sym}))
            except httpx.HTTPStatusError as e:
                mapped = map_http_error(e)
                if isinstance(mapped, ProviderDataError):
                    continue  # 4xx for this symbol (e.g. 404) -> skip just this name
                raise mapped from e  # auth/rate/outage is systemic -> surface it
            except httpx.TransportError as e:
                raise map_http_error(e) from e
            out[sym] = map_metrics(ratios, key_metrics, income, balance, dcf)
        return out

    def _earnings_slice(self, start: date, end: date) -> list[dict]:
        """One slice of the calendar, halving on a cap hit. Never called with a wide window."""
        if (end - start).days > _EARNINGS_MAX_RANGE_DAYS:
            raise ProviderDataError(
                f"refusing to request {(end - start).days}d of earnings calendar in one call — "
                f"upstream clamps anything past {_EARNINGS_MAX_RANGE_DAYS}d and drops the "
                "near-term rows without saying so (issue #113)"
            )
        payload = self._client.get(
            "earnings-calendar",
            {"from": start.isoformat(), "to": end.isoformat()},
            cache=False,  # the calendar must be current on every request, not day-old
        )
        rows = payload if isinstance(payload, list) else []
        if len(rows) >= _EARNINGS_ROW_CAP and end > start:
            # a very dense slice can still clip — halve and retry both sides
            mid = start + timedelta(days=(end - start).days // 2)
            return self._earnings_slice(start, mid) + self._earnings_slice(
                mid + timedelta(days=1), end
            )
        return rows

    def _earnings_rows(self, start: date, end: date) -> list[dict]:
        """Walk [start, end] in small slices, then verify what came back actually covers it.

        Slicing is unconditional: every call stays far under both the row cap and the 90-day
        range clamp, so neither can silently eat the near term. Nothing here ever issues a wide
        window — that request shape is the bug (see the constants above).
        """
        rows: list[dict] = []
        covered: set[date] = set()
        cursor = start
        while cursor <= end:
            stop = min(cursor + timedelta(days=_EARNINGS_CHUNK_DAYS - 1), end)
            chunk = self._earnings_slice(cursor, stop)
            rows.extend(chunk)
            for row in chunk:
                when = _row_date(row)
                if when is not None:
                    covered.add(when)
            cursor = stop + timedelta(days=1)
        verify_calendar_coverage(start, end, covered)
        return rows

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        if end < start:
            return {}
        return map_earnings(self._earnings_rows(start, end))

    def next_earnings(self, symbol: str, on_or_after: date) -> date | None:
        """This one symbol's next report, from the per-symbol endpoint — authoritative and immune
        to the calendar's clipping, so it settles gaps the bulk window left behind."""
        payload = self._client.get("earnings", {"symbol": symbol}, cache=False)
        rows = payload if isinstance(payload, list) else []
        upcoming = [
            when
            for when in (_row_date(r) for r in rows if isinstance(r, dict))
            if when is not None and when >= on_or_after
        ]
        return min(upcoming) if upcoming else None
