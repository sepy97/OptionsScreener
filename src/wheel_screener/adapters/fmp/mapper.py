"""Pure FMP-JSON -> core-model mapping.

FMP `/stable/` field spellings could not be verified against the (gated) live docs, so
mapping is defensive: each field tries several candidate keys. Verify against one real
response and prune the candidate lists once confirmed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from wheel_screener.core.models import Dividend, FundamentalMetrics, Underlying


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def map_universe_row(row: dict) -> Underlying:
    return Underlying(
        symbol=row.get("symbol"),
        name=_pick(row, "companyName", "name"),
        price=_num(_pick(row, "price")),
        market_cap=_num(_pick(row, "marketCap", "marketCapitalization")),
        sector=_pick(row, "sector"),
    )


def map_metrics(
    ratios: dict, key_metrics: dict, income: dict, balance: dict, dcf: dict
) -> FundamentalMetrics:
    r, k, inc, bal, d = ratios or {}, key_metrics or {}, income or {}, balance or {}, dcf or {}
    # ROE/ROA/ROIC live in key-metrics-ttm; PE/PS/PB/margins/liquidity in ratios-ttm — merge
    # so a field is found regardless of which endpoint carries it.
    rk = {**r, **k}
    return FundamentalMetrics(
        # value (first key in each list is the verified live /stable/ field name)
        pe=_num(_pick(rk, "priceToEarningsRatioTTM", "peRatioTTM", "priceEarningsRatioTTM")),
        ps=_num(_pick(rk, "priceToSalesRatioTTM", "priceSalesRatioTTM")),
        pb=_num(_pick(rk, "priceToBookRatioTTM", "priceBookValueRatioTTM", "pbRatioTTM")),
        peg=_num(_pick(rk, "priceToEarningsGrowthRatioTTM", "pegRatioTTM")),
        dcf=_num(_pick(d, "dcf")),
        price=_num(_pick(d, "Stock Price", "stockPrice", "price")),
        # efficiency
        roe=_num(_pick(rk, "returnOnEquityTTM")),
        roa=_num(_pick(rk, "returnOnAssetsTTM")),
        ros=_num(_pick(rk, "netProfitMarginTTM", "netIncomePerRevenueTTM")),
        roi=_num(_pick(rk, "returnOnInvestedCapitalTTM", "roicTTM")),
        debt_to_equity=_num(_pick(rk, "debtToEquityRatioTTM", "debtEquityRatioTTM")),
        net_debt_to_ebitda=_num(_pick(rk, "netDebtToEBITDATTM", "netDebtToEbitdaTTM")),
        fcf_yield=_num(_pick(rk, "freeCashFlowYieldTTM")),
        # liquidity
        current_ratio=_num(_pick(rk, "currentRatioTTM")),
        quick_ratio=_num(_pick(rk, "quickRatioTTM")),
        cash_ratio=_num(_pick(rk, "cashRatioTTM")),
        # sign inputs for the gates
        eps=_num(_pick(inc, "eps", "epsdiluted", "epsDiluted")),
        total_equity=_num(_pick(bal, "totalStockholdersEquity", "totalEquity")),
        ebitda=_num(_pick(inc, "ebitda")),
    )


def map_dividends(rows: object) -> list[Dividend]:
    """Map `/stable/dividends?symbol=` rows to ex-dividends, earliest first.

    ``adjDividend`` is preferred over ``dividend``: it is restated for later splits, so a
    pre-split payment is comparable with today's share price, and the two are equal on every
    row since the last split. Rows without a date or a positive amount are dropped.
    """
    out: list[Dividend] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            ex = datetime.strptime(str(row.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        amount = _num(row.get("adjDividend")) or _num(row.get("dividend"))
        if not amount or amount <= 0:
            continue
        freq = str(row.get("frequency") or "").strip().lower() or None
        out.append(Dividend(ex_date=ex, amount=amount, frequency=freq))
    return sorted(out, key=lambda d: d.ex_date)


def map_earnings(rows: list[dict]) -> dict[str, date]:
    """Map earnings-calendar rows to {symbol -> earliest upcoming earnings date}."""
    out: dict[str, date] = {}
    for row in rows or []:
        sym = row.get("symbol")
        raw = row.get("date")
        if not sym or not raw:
            continue
        try:
            d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if sym not in out or d < out[sym]:
            out[sym] = d
    return out
