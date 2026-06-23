"""Pure FMP-JSON -> core-model mapping.

FMP `/stable/` field spellings could not be verified against the (gated) live docs, so
mapping is defensive: each field tries several candidate keys. Verify against one real
response and prune the candidate lists once confirmed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from wheel_screener.core.models import FundamentalMetrics, Underlying


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
