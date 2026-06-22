"""FundamentalsProvider backed by the local bulk-CSV store (tools/fmp_bulk_import.py output).

Serves the screener with ZERO API calls by reading FMP `*-bulk` CSVs from a data dir.
Earnings (not in the bulk store) are delegated to an optional live provider, so the
earnings blackout still works on the cheap Starter key; without one, blackout is skipped.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Protocol

import polars as pl

from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying

# FundamentalMetrics field -> source bulk column
_RATIOS = {
    "pe": "priceToEarningsRatioTTM",
    "ps": "priceToSalesRatioTTM",
    "pb": "priceToBookRatioTTM",
    "peg": "priceToEarningsGrowthRatioTTM",
    "ros": "netProfitMarginTTM",
    "current_ratio": "currentRatioTTM",
    "quick_ratio": "quickRatioTTM",
    "cash_ratio": "cashRatioTTM",
    "debt_to_equity": "debtToEquityRatioTTM",
}
_KEY_METRICS = {
    "roe": "returnOnEquityTTM",
    "roa": "returnOnAssetsTTM",
    "roi": "returnOnInvestedCapitalTTM",
    "net_debt_to_ebitda": "netDebtToEBITDATTM",
}
_STATEMENT_YEARS = 3  # latest N fiscal years scanned for sign inputs (coalesced per symbol)


class _EarningsSource(Protocol):
    def earnings_calendar(self, start: date, end: date) -> dict[str, date]: ...


class LocalFundamentalsProvider:
    def __init__(self, data_dir: str, earnings_provider: _EarningsSource | None = None) -> None:
        self._dir = Path(os.path.expanduser(data_dir))
        self._earnings = earnings_provider
        self._profiles: pl.DataFrame | None = None
        self._metrics: pl.DataFrame | None = None  # one row per symbol, FundamentalMetrics columns

    # --- loading -------------------------------------------------------------
    def _read(self, name: str) -> pl.DataFrame:
        return pl.read_csv(self._dir / name, infer_schema_length=0)

    def _latest_statement(self, prefix: str, cols: list[str]) -> pl.DataFrame:
        """Coalesce the latest available fiscal year per symbol (handles filing lag)."""
        files = sorted(self._dir.glob(f"{prefix}_*.csv"), reverse=True)[:_STATEMENT_YEARS]
        if not files:
            return pl.DataFrame({"symbol": []}, schema={"symbol": pl.Utf8})
        df = pl.concat(
            [pl.read_csv(f, infer_schema_length=0) for f in files], how="vertical_relaxed"
        )
        keep = [c for c in ["symbol", "fiscalYear", *cols] if c in df.columns]
        latest = (
            df.select(keep)
            .with_columns(pl.col("fiscalYear").cast(pl.Int64, strict=False))
            .sort("fiscalYear")
            .group_by("symbol")
            .last()
        )
        return latest.drop("fiscalYear")  # drop after picking latest; avoids join collisions

    def _ensure_loaded(self) -> None:
        if self._metrics is not None:
            return
        parts = sorted(self._dir.glob("profile-bulk_part*.csv"))
        if not parts:
            raise FileNotFoundError(
                f"no profile-bulk CSVs in {self._dir} — run tools/fmp_bulk_import.py first"
            )
        self._profiles = pl.concat(
            [pl.read_csv(p, infer_schema_length=0) for p in parts], how="vertical_relaxed"
        ).with_columns(
            pl.col("price").cast(pl.Float64, strict=False),
            pl.col("marketCap").cast(pl.Float64, strict=False),
        )

        def sel(df: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
            present = {dst: src for dst, src in mapping.items() if src in df.columns}
            return df.select(["symbol", *[pl.col(src).alias(dst) for dst, src in present.items()]])

        m = sel(self._read("ratios-ttm-bulk.csv"), _RATIOS)
        km = sel(self._read("key-metrics-ttm-bulk.csv"), _KEY_METRICS)
        m = m.join(km, on="symbol", how="full", coalesce=True)
        dcf = self._read("dcf-bulk.csv").select(
            ["symbol", pl.col("dcf").alias("dcf"), pl.col("Stock Price").alias("price")]
        )
        m = m.join(dcf, on="symbol", how="full", coalesce=True)
        m = m.join(
            self._latest_statement("income-statement-bulk", ["eps", "ebitda"]),
            on="symbol", how="full", coalesce=True,
        )
        bal = self._latest_statement("balance-sheet-statement-bulk", ["totalStockholdersEquity"])
        if "totalStockholdersEquity" in bal.columns:
            bal = bal.rename({"totalStockholdersEquity": "total_equity"})
        m = m.join(bal, on="symbol", how="full", coalesce=True)

        float_cols = [c for c in m.columns if c != "symbol"]
        self._metrics = m.with_columns(
            [pl.col(c).cast(pl.Float64, strict=False) for c in float_cols]
        )

    # --- port methods --------------------------------------------------------
    def screen_universe(self, criteria: ScreenCriteria) -> list[Underlying]:
        self._ensure_loaded()
        exch = [e.upper() for e in criteria.exchanges]
        df = self._profiles.filter(pl.col("exchange").is_in(exch))
        for flag in ("isEtf", "isFund", "isAdr"):
            if flag in df.columns:
                df = df.filter(pl.col(flag).str.to_lowercase() != "true")
        if "isActivelyTrading" in df.columns:
            df = df.filter(pl.col("isActivelyTrading").str.to_lowercase() == "true")
        df = df.filter(
            (pl.col("price") >= criteria.min_price)
            & (pl.col("price") <= criteria.max_price)
            & (pl.col("marketCap") >= criteria.min_market_cap)
        )
        return [
            Underlying(
                symbol=r["symbol"], name=r.get("companyName"),
                price=r.get("price"), market_cap=r.get("marketCap"), sector=r.get("sector"),
            )
            for r in df.iter_rows(named=True)
            if r["symbol"]
        ]

    def _metrics_for(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        self._ensure_loaded()
        wanted = set(symbols)
        fields = set(FundamentalMetrics.model_fields)
        out: dict[str, FundamentalMetrics] = {}
        for r in self._metrics.filter(pl.col("symbol").is_in(wanted)).iter_rows(named=True):
            out[r["symbol"]] = FundamentalMetrics(**{k: v for k, v in r.items() if k in fields})
        return out

    def bulk_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        return self._metrics_for(symbols)

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        return self._metrics_for(symbols)  # local: full metrics either way (no API cost)

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        return self._earnings.earnings_calendar(start, end) if self._earnings else {}
