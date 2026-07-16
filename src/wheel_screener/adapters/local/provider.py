"""FundamentalsProvider backed by the local bulk-CSV store (tools/fmp_bulk_import.py output).

Serves the screener with ZERO API calls by reading FMP `*-bulk` CSVs from a data dir.
Earnings (not in the bulk store) are delegated to an optional live provider, so the
earnings blackout still works on the cheap Starter key; without one, blackout is skipped.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Protocol

import polars as pl

from wheel_screener.adapters.local.overlay import OVERLAY_FILENAME, read_overlay
from wheel_screener.core.models import FundamentalMetrics, ScreenCriteria, Underlying

logger = logging.getLogger(__name__)

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
    "fcf_yield": "freeCashFlowYieldTTM",
}
_STATEMENT_YEARS = 3  # latest N fiscal years scanned for sign inputs (coalesced per symbol)

# the only profile columns the universe filter + Underlying need — projected at load so FMP's
# fat text columns (description, etc.) never materialize (the main memory win of the lazy load).
_PROFILE_COLS = [
    "symbol", "companyName", "exchange", "sector", "price", "marketCap", "averageVolume",
    "isEtf", "isFund", "isAdr", "isActivelyTrading", "industry", "fullTimeEmployees",
]

# Names that aren't common stock: notes/baby-bonds, preferreds, warrants, depositary
# shares, and partnerships (MLP units). Matched case-insensitively against companyName.
_NONCOMMON_NAME = (
    r"(?i)%|\bnotes?\b|\bdebentures?\b|\bsubordinated\b|\bpreferred\b|\bpfd\b"
    r"|\bdepositary\b|\bwarrants?\b|\bdue\s+20\d\d|\bL\.P\.|,\s*LP\b"
)


class _EarningsSource(Protocol):
    def earnings_calendar(self, start: date, end: date) -> dict[str, date]: ...


class LocalFundamentalsProvider:
    def __init__(self, data_dir: str, earnings_provider: _EarningsSource | None = None) -> None:
        self._dir = Path(os.path.expanduser(data_dir))
        self._earnings = earnings_provider
        self._profiles: pl.DataFrame | None = None
        self._metrics: pl.DataFrame | None = None  # one row per symbol, FundamentalMetrics columns
        self._overlay: dict[str, FundamentalMetrics] | None = None  # fresh per-symbol refreshes
        self._overlay_mtime: float | None = None
        self._lock = threading.Lock()  # guards lazy load + overlay reload when shared (web)

    # --- loading (lazy: scan + project so only the needed columns ever materialize) ----------
    def _scan(self, name: str) -> pl.LazyFrame:
        return pl.scan_csv(self._dir / name, infer_schema_length=0)

    def _latest_statement(self, prefix: str, cols: list[str]) -> pl.LazyFrame:
        """Coalesce the latest available fiscal year per symbol (handles filing lag), lazily.

        Per-column ``sort_by(fiscalYear).last()`` picks the newest year order-independently — no
        reliance on a global sort surviving the group-by."""
        files = sorted(self._dir.glob(f"{prefix}_*.csv"), reverse=True)[:_STATEMENT_YEARS]
        empty = pl.DataFrame({"symbol": []}, schema={"symbol": pl.Utf8}).lazy()
        if not files:
            return empty
        lf = pl.concat(
            [pl.scan_csv(f, infer_schema_length=0) for f in files], how="vertical_relaxed"
        )
        avail = lf.collect_schema().names()
        value_cols = [c for c in cols if c in avail]
        if "fiscalYear" not in avail or not value_cols:
            return empty
        return (
            lf.select(["symbol", "fiscalYear", *value_cols])
            .with_columns(pl.col("fiscalYear").cast(pl.Int64, strict=False))
            .group_by("symbol")
            .agg([pl.col(c).sort_by("fiscalYear").last() for c in value_cols])
        )

    def _ensure_loaded(self) -> None:
        if self._metrics is not None:
            return
        with self._lock:  # only one thread loads the store; concurrent first-touch requests wait
            if self._metrics is None:
                self._load()

    def _load(self) -> None:
        parts = sorted(self._dir.glob("profile-bulk_part*.csv"))
        if not parts:
            raise FileNotFoundError(
                f"no profile-bulk CSVs in {self._dir} — run tools/fmp_bulk_import.py first"
            )
        prof = pl.concat(
            [pl.scan_csv(p, infer_schema_length=0) for p in parts], how="vertical_relaxed"
        )
        keep = [c for c in _PROFILE_COLS if c in prof.collect_schema().names()]
        self._profiles = (
            prof.select(keep)
            .with_columns(
                [pl.col(c).cast(pl.Float64, strict=False) for c in ("price", "marketCap")
                 if c in keep]
            )
            .collect()
        )

        def sel(lf: pl.LazyFrame, mapping: dict[str, str]) -> pl.LazyFrame:
            avail = lf.collect_schema().names()
            present = {dst: src for dst, src in mapping.items() if src in avail}
            return lf.select(["symbol", *[pl.col(src).alias(dst) for dst, src in present.items()]])

        m = sel(self._scan("ratios-ttm-bulk.csv"), _RATIOS)
        m = m.join(sel(self._scan("key-metrics-ttm-bulk.csv"), _KEY_METRICS),
                   on="symbol", how="full", coalesce=True)
        dcf = self._scan("dcf-bulk.csv").select(
            [pl.col("symbol"), pl.col("dcf"), pl.col("Stock Price").alias("price")]
        )
        m = m.join(dcf, on="symbol", how="full", coalesce=True)
        m = m.join(
            self._latest_statement("income-statement-bulk", ["eps", "ebitda"]),
            on="symbol", how="full", coalesce=True,
        )
        bal = self._latest_statement("balance-sheet-statement-bulk", ["totalStockholdersEquity"])
        if "totalStockholdersEquity" in bal.collect_schema().names():
            bal = bal.rename({"totalStockholdersEquity": "total_equity"})
        m = m.join(bal, on="symbol", how="full", coalesce=True)

        float_cols = [c for c in m.collect_schema().names() if c != "symbol"]
        # collect ONCE — projection pushdown means only these ~15 columns are ever materialized
        self._metrics = m.with_columns(
            [pl.col(c).cast(pl.Float64, strict=False) for c in float_cols]
        ).collect()

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
        # common stocks only: drop notes/preferreds/warrants/MLPs (by name) and
        # closed-end funds (Asset-Management industry with no employees).
        if "companyName" in df.columns:
            df = df.filter(~pl.col("companyName").fill_null("").str.contains(_NONCOMMON_NAME))
        if "industry" in df.columns and "fullTimeEmployees" in df.columns:
            emp = pl.col("fullTimeEmployees").cast(pl.Int64, strict=False).fill_null(0)
            df = df.filter(~((pl.col("industry") == "Asset Management") & (emp <= 0)))
        # keep only the shortest ticker per company name: drops suffixed preferreds (IMPP vs
        # IMPPP, same name) and de-dups dual share classes (keep GOOG, drop GOOGL).
        if "companyName" in df.columns:
            min_len = (
                self._profiles.with_columns(pl.col("symbol").str.len_chars().alias("_l"))
                .group_by("companyName")
                .agg(pl.col("_l").min().alias("_min_len"))
            )
            df = (
                df.join(min_len, on="companyName", how="left")
                .filter(pl.col("symbol").str.len_chars() <= pl.col("_min_len").fill_null(99))
                .drop("_min_len")
            )
        df = df.filter(
            (pl.col("price") >= criteria.min_price)
            & (pl.col("price") <= criteria.max_price)
            & (pl.col("marketCap") >= criteria.min_market_cap)
        )
        if criteria.min_dollar_volume > 0 and "averageVolume" in df.columns:
            # skip stocks too thin to have tradeable options (they'd fail the OI/spread gates
            # after a wasted chain call) — the cheap win against the Schwab rate limit
            dvol = pl.col("price") * pl.col("averageVolume").cast(pl.Float64, strict=False)
            df = df.filter(dvol.fill_null(0.0) >= criteria.min_dollar_volume)
        return [
            Underlying(
                symbol=r["symbol"], name=r.get("companyName"),
                price=r.get("price"), market_cap=r.get("marketCap"), sector=r.get("sector"),
            )
            for r in df.iter_rows(named=True)
            if r["symbol"]
        ]

    def known_symbols(self) -> set[str]:
        """Every symbol in the bulk store (used by refresh-fundamentals to scope reporters)."""
        self._ensure_loaded()
        return set(self._profiles.get_column("symbol").to_list())

    def _current_overlay(self) -> dict[str, FundamentalMetrics]:
        """The overlay, reloaded if overlay_metrics.csv changed — so a long-lived server picks up a
        refresh-fundamentals run without a restart. The stat + reload happen under the lock (so the
        mtime always matches the loaded content), and a transient read failure keeps the last-good
        overlay rather than crashing the request."""
        with self._lock:
            try:
                mtime: float | None = (self._dir / OVERLAY_FILENAME).stat().st_mtime
            except OSError:
                mtime = None
            if self._overlay is None or mtime != self._overlay_mtime:
                try:
                    self._overlay = read_overlay(str(self._dir))
                    self._overlay_mtime = mtime
                except Exception as e:  # noqa: BLE001 - keep the last-good overlay; retry next call
                    logger.warning("overlay reload failed, keeping previous: %s", e)
                    if self._overlay is None:
                        self._overlay = {}
            return self._overlay

    def _metrics_for(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        self._ensure_loaded()
        overlay = self._current_overlay()
        wanted = set(symbols)
        fields = set(FundamentalMetrics.model_fields)
        out: dict[str, FundamentalMetrics] = {}
        for r in self._metrics.filter(pl.col("symbol").is_in(wanted)).iter_rows(named=True):
            out[r["symbol"]] = FundamentalMetrics(**{k: v for k, v in r.items() if k in fields})
        for sym in wanted & overlay.keys():
            out[sym] = overlay[sym]  # fresh per-symbol refresh wins over the bulk snapshot
        return out

    def bulk_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        return self._metrics_for(symbols)

    def fetch_metrics(self, symbols: list[str]) -> dict[str, FundamentalMetrics]:
        return self._metrics_for(symbols)  # local: full metrics either way (no API cost)

    def earnings_calendar(self, start: date, end: date) -> dict[str, date]:
        return self._earnings.earnings_calendar(start, end) if self._earnings else {}
