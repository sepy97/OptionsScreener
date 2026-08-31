"""The optionable-ETF universe, which no single vendor we use can answer for.

An ETF needs three facts and they come from three places:

* **that it IS one** — FMP's ``etf-list``. Alpaca classes ETFs and common stock alike as
  ``us_equity``, and guessing from the name ("Trust", "Fund") misfiles both ways.
* **that it has options** — Alpaca's asset record, via the ``has_options`` attribute.
* **what it costs and how much trades** — Alpaca's stock snapshots, the same bulk call the
  batched chain fetch already makes.

Deliberately NOT the local fundamentals store, which is where this started. The store does
carry 3,235 ETF rows, but not one of SPY, QQQ, IWM, TLT, GDX or XLF — only small recent funds.
An ETF screen without the liquid ones is not worth having, so the store is bypassed rather than
patched.

No fundamentals are fetched for these at all. An ETF has no margins to judge, no leverage to
gate on and no peers to rank against, so it carries no strength score and none is invented for
it; the blend already judges an unrated name on its yield alone.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

import httpx

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.errors import ALPACA, FMP, map_http_error
from wheel_screener.adapters.http import RateLimiter
from wheel_screener.config import AlpacaSettings, FmpSettings
from wheel_screener.core.models import ScreenCriteria, Underlying

logger = logging.getLogger(__name__)

SNAPSHOT_BATCH = 100  # the stock snapshot endpoint's own cap


def _chunks(seq: Sequence[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


class EtfUniverseProvider:
    """Composes the three sources into one list of screenable ETFs."""

    def __init__(
        self,
        fmp: FmpSettings,
        alpaca: AlpacaSettings,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._fmp = fmp
        self._alpaca = alpaca
        self._client = client or httpx.Client(timeout=timeout)
        self._limiter = RateLimiter(alpaca.calls_per_minute)
        # Membership changes on the timescale of fund launches, not minutes, so it is cached
        # for a day. Prices are never cached here — they come from the live snapshot call.
        self._cache: DiskCache | None = (
            DiskCache(alpaca.chain_cache_dir, 86_400) if alpaca.chain_cache_enabled else None
        )

    def _alpaca_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._alpaca.api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._alpaca.api_secret.get_secret_value(),
            "accept": "application/json",
        }

    def _etf_symbols(self) -> dict[str, str]:
        """``{symbol: name}`` for every listed ETF."""
        cached = self._cache.get("etf:list") if self._cache else None
        if isinstance(cached, dict) and cached:
            return cached
        url = f"{self._fmp.base_url.rstrip('/')}/etf-list"
        try:
            resp = self._client.get(
                url, params={"apikey": self._fmp.api_key.get_secret_value()}
            )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, FMP) from e
        out = {
            r["symbol"]: r.get("name") or r["symbol"]
            for r in rows if isinstance(r, dict) and r.get("symbol")
        }
        if self._cache is not None and out:
            self._cache.set("etf:list", out)
        return out

    def _optionable(self) -> set[str]:
        """Symbols Alpaca will quote an option chain for."""
        cached = self._cache.get("etf:optionable") if self._cache else None
        if isinstance(cached, list) and cached:
            return set(cached)
        url = f"{self._alpaca.trading_base_url.rstrip('/')}/v2/assets"
        try:
            self._limiter.acquire()
            resp = self._client.get(
                url, headers=self._alpaca_headers(),
                params={"status": "active", "asset_class": "us_equity"},
            )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, ALPACA) from e
        out = {
            r["symbol"] for r in rows
            if isinstance(r, dict) and r.get("tradable")
            and "has_options" in (r.get("attributes") or [])
        }
        if self._cache is not None and out:
            self._cache.set("etf:optionable", sorted(out))
        return out

    def _quotes(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        """``{symbol: (price, dollar volume)}`` — the same bulk endpoint the chain fetch uses."""
        url = f"{self._alpaca.data_base_url.rstrip('/')}/v2/stocks/snapshots"
        out: dict[str, tuple[float, float]] = {}
        for chunk in _chunks(symbols, SNAPSHOT_BATCH):
            try:
                self._limiter.acquire()
                resp = self._client.get(
                    url, headers=self._alpaca_headers(),
                    params={"symbols": ",".join(chunk), "feed": self._alpaca.stock_feed},
                )
                resp.raise_for_status()
                page = resp.json()
            except httpx.HTTPStatusError:
                # One unlisted ticker rejects the whole batch of 100. A missing price costs
                # that ETF its place in the screen, never the rest of the batch.
                logger.info("etf quotes: a batch of %d was rejected, skipping it", len(chunk))
                continue
            except httpx.TransportError as e:
                raise map_http_error(e, ALPACA) from e
            for sym, snap in (page or {}).items():
                bar = snap.get("dailyBar") or {}
                price = (snap.get("latestTrade") or {}).get("p") or bar.get("c")
                if price:
                    out[sym] = (float(price), float(price) * float(bar.get("v") or 0))
        return out

    def etf_universe(self, criteria: ScreenCriteria) -> list[Underlying]:
        """Optionable ETFs inside the criteria's price and turnover band."""
        names = self._etf_symbols()
        candidates = sorted(names.keys() & self._optionable())
        if not candidates:
            return []
        quotes = self._quotes(candidates)
        out = [
            Underlying(
                symbol=sym, name=names.get(sym), price=price, is_etf=True,
                # No sector: an ETF's holdings span them, so naming one would be a claim the
                # data does not support — and the sector cap must not bucket them together.
            )
            for sym, (price, dollar_volume) in sorted(quotes.items())
            if criteria.min_price <= price <= criteria.max_price
            and dollar_volume >= criteria.min_dollar_volume
        ]
        logger.info(
            "etf universe: %d optionable of %d listed, %d inside $%g-%g and $%gM/day",
            len(candidates), len(names), len(out), criteria.min_price, criteria.max_price,
            criteria.min_dollar_volume / 1e6,
        )
        return out
