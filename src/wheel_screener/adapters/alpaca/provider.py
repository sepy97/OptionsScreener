"""ChainProvider backed by Alpaca options data (plain REST via httpx).

Alpaca's data API allows ~1000 req/min (vs Schwab's ~120) and authenticates with a key/secret
header — no OAuth. Two calls per underlying, merged by OCC symbol: the *snapshot* (quotes/greeks/
IV) from the data API, and the *contracts* reference (open interest) from the trading API. Each
endpoint paginates via ``next_page_token``. ``feed`` is 'indicative' (free) or 'opra' (paid).
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from wheel_screener.adapters.alpaca.mapper import build_chain
from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.errors import map_http_error
from wheel_screener.adapters.http import RateLimiter, run_with_retry
from wheel_screener.config import AlpacaSettings
from wheel_screener.core.errors import ProviderError, ProviderUnavailableError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, ProviderCaps


class AlpacaChainProvider:
    def __init__(
        self, settings: AlpacaSettings, client: httpx.Client | None = None, timeout: float = 15.0
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=timeout)
        self._limiter = RateLimiter(settings.calls_per_minute)
        self._cache: DiskCache | None = (
            DiskCache(settings.chain_cache_dir, settings.chain_cache_ttl_seconds)
            if settings.chain_cache_enabled
            else None
        )

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._settings.api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._settings.api_secret.get_secret_value(),
            "accept": "application/json",
        }

    def _get(self, url: str, params: dict) -> dict:
        self._limiter.acquire()  # re-acquired per attempt so retries respect the rate limit
        resp = self._client.get(url, params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, url: str, params: dict, collect, *, attempts: int, mult: float):
        """Run a token-paginated GET, calling ``collect(page)`` per page (each a retried call)."""
        token = None
        for _ in range(500):  # safety cap, far beyond any real window — avoids an infinite loop
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            page = run_with_retry(
                lambda p=page_params: self._get(url, p), max_attempts=attempts, multiplier=mult
            )
            collect(page)
            token = page.get("next_page_token")
            if not token:
                return

    def _snapshots(self, symbol: str, from_date: date, to_date: date) -> dict:
        url = f"{self._settings.data_base_url.rstrip('/')}/v1beta1/options/snapshots/{symbol}"
        params = {
            "feed": self._settings.feed,
            "type": "put",
            "expiration_date_gte": from_date.isoformat(),
            "expiration_date_lte": to_date.isoformat(),
            "limit": 1000,
        }
        out: dict = {}
        self._paginate(
            url, params, lambda page: out.update(page.get("snapshots") or {}),
            attempts=self._settings.max_retries + 1, mult=self._settings.retry_backoff_multiplier,
        )
        return out

    def _open_interest(self, symbol: str, from_date: date, to_date: date) -> dict:
        url = f"{self._settings.trading_base_url.rstrip('/')}/v2/options/contracts"
        params = {
            "underlying_symbols": symbol,
            "type": "put",
            "status": "active",
            "expiration_date_gte": from_date.isoformat(),
            "expiration_date_lte": to_date.isoformat(),
            "limit": 10000,
        }
        oi: dict[str, int] = {}

        def _collect(page: dict) -> None:
            for c in page.get("option_contracts") or []:
                sym, raw = c.get("symbol"), c.get("open_interest")
                if sym and raw is not None:
                    try:
                        oi[sym] = int(raw)
                    except (TypeError, ValueError):
                        pass

        self._paginate(
            url, params, _collect,
            attempts=self._settings.max_retries + 1, mult=self._settings.retry_backoff_multiplier,
        )
        return oi

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        today = date.today()
        from_date = today + timedelta(days=filt.min_dte if filt.min_dte is not None else 0)
        to_date = today + timedelta(days=filt.max_dte if filt.max_dte is not None else 60)
        cache_key = f"alpaca:{symbol}:{from_date}:{to_date}:{self._settings.feed}:PUT"

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if isinstance(cached, dict):
                return build_chain(symbol, cached.get("snapshots"), cached.get("oi"), today)

        try:
            snapshots = self._snapshots(symbol, from_date, to_date)
            oi = self._open_interest(symbol, from_date, to_date)
        except ProviderError:
            raise  # never mask a typed provider error
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e) from e  # transient kinds already retried + exhausted
        except Exception as e:  # noqa: BLE001 - any vendor failure -> a provider problem
            raise ProviderUnavailableError(f"alpaca chain fetch failed for {symbol}: {e}") from e

        if self._cache is not None:
            self._cache.set(cache_key, {"snapshots": snapshots, "oi": oi})
        return build_chain(symbol, snapshots, oi, today)

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="alpaca",
            supports_batch_underlyings=False,
            max_concurrency=self._settings.max_concurrency,
            server_side_filters=["type", "expiration_date_gte", "expiration_date_lte"],
            realtime=(self._settings.feed == "opra"),
        )
