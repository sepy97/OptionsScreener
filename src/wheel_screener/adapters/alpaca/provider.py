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
from wheel_screener.adapters.errors import ALPACA, map_http_error
from wheel_screener.adapters.http import RateLimiter, run_with_retry
from wheel_screener.config import AlpacaSettings
from wheel_screener.core.errors import ProviderError, ProviderUnavailableError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, OptionType, ProviderCaps


class AlpacaChainProvider:
    def __init__(
        self, settings: AlpacaSettings, client: httpx.Client | None = None, timeout: float = 15.0
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=timeout)
        # the data API (snapshots) and trading API (contracts) enforce SEPARATE rate limits,
        # so each host gets its own limiter rather than sharing one budget (which would 429)
        self._data_limiter = RateLimiter(settings.calls_per_minute)
        self._trading_limiter = RateLimiter(settings.calls_per_minute)
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

    def check_auth(self) -> str | None:
        """Verify the credentials with ONE cheap authenticated call. None means healthy.

        ``/v2/account`` is the smallest endpoint that exercises the key pair, and it lives on the
        trading host — the account-bound one — so it also catches a paper key pointed at the live
        API. Presence of a key in the environment proves nothing; only a call does.
        """
        url = f"{self._settings.trading_base_url.rstrip('/')}/v2/account"
        try:
            resp = self._client.get(url, headers=self._headers(), timeout=5.0)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            return str(map_http_error(e, ALPACA))
        except Exception as e:  # noqa: BLE001 - a probe must never raise
            return f"{ALPACA} check failed: {e}"
        return None

    def _get(self, url: str, params: dict, limiter: RateLimiter) -> dict:
        limiter.acquire()  # re-acquired per attempt so retries respect the rate limit
        resp = self._client.get(url, params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, url, params, collect, limiter, *, attempts: int, mult: float):
        """Run a token-paginated GET, calling ``collect(page)`` per page (each a retried call)."""
        token = None
        for _ in range(500):  # safety cap, far beyond any real window — avoids an infinite loop
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            page = run_with_retry(
                lambda p=page_params: self._get(url, p, limiter),
                max_attempts=attempts, multiplier=mult,
            )
            collect(page)
            token = page.get("next_page_token")
            if not token:
                return

    def _snapshots(
        self, symbol: str, from_date: date, to_date: date, option_type: OptionType
    ) -> dict:
        url = f"{self._settings.data_base_url.rstrip('/')}/v1beta1/options/snapshots/{symbol}"
        params = {
            "feed": self._settings.feed,
            "type": option_type.value,
            "expiration_date_gte": from_date.isoformat(),
            "expiration_date_lte": to_date.isoformat(),
            "limit": 1000,
        }
        out: dict = {}
        self._paginate(
            url, params, lambda page: out.update(page.get("snapshots") or {}), self._data_limiter,
            attempts=self._settings.max_retries + 1, mult=self._settings.retry_backoff_multiplier,
        )
        return out

    def _open_interest(
        self, symbol: str, from_date: date, to_date: date, option_type: OptionType
    ) -> dict:
        url = f"{self._settings.trading_base_url.rstrip('/')}/v2/options/contracts"
        params = {
            "underlying_symbols": symbol,
            "type": option_type.value,
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
            url, params, _collect, self._trading_limiter,
            attempts=self._settings.max_retries + 1, mult=self._settings.retry_backoff_multiplier,
        )
        return oi

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        today = date.today()
        from_date = today + timedelta(days=filt.min_dte if filt.min_dte is not None else 0)
        to_date = today + timedelta(days=filt.max_dte if filt.max_dte is not None else 60)
        # the option type is part of the key: puts and calls are separate fetches over the same
        # symbol/window, so a shared key would serve one side's chain for the other's request
        side = filt.option_type
        cache_key = (
            f"alpaca:{symbol}:{from_date}:{to_date}:{self._settings.feed}:{side.value.upper()}"
        )

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if isinstance(cached, dict):
                return build_chain(symbol, cached.get("snapshots"), cached.get("oi"), today)

        try:
            snapshots = self._snapshots(symbol, from_date, to_date, side)
            oi = self._open_interest(symbol, from_date, to_date, side)
        except ProviderError:
            raise  # never mask a typed provider error
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, ALPACA) from e  # transient kinds already retried + exhausted
        except Exception as e:  # noqa: BLE001 - any vendor failure -> a provider problem
            raise ProviderUnavailableError(f"alpaca chain fetch failed for {symbol}: {e}") from e

        if self._cache is not None:
            self._cache.set(cache_key, {"snapshots": snapshots, "oi": oi})
        return build_chain(symbol, snapshots, oi, today)

    def spot(self, symbol: str) -> float | None:
        """Latest trade price for the underlying, or None if it can't be established.

        Alpaca's option snapshot is option-only (no spot), so a covered-call yield — premium over
        the share price — needs this one extra call. Used only by the single-ticker search, where
        one more request is immaterial; the screener is CSP-only and prices off the strike.

        Never raises: spot is a display/denominator input, and losing it must degrade the yield
        cell to "—", not fail the whole search.
        """
        url = f"{self._settings.data_base_url.rstrip('/')}/v2/stocks/{symbol}/snapshot"
        try:
            payload = run_with_retry(
                lambda: self._get(url, {"feed": self._settings.stock_feed}, self._data_limiter),
                max_attempts=self._settings.max_retries + 1,
                multiplier=self._settings.retry_backoff_multiplier,
            )
        except Exception:  # noqa: BLE001 - see docstring: a missing spot is not a failed search
            return None
        # latest trade first (an actual print); the daily bar's close covers a halted/quiet tape
        for path in (("latestTrade", "p"), ("dailyBar", "c"), ("prevDailyBar", "c")):
            node = (payload or {}).get(path[0]) or {}
            raw = node.get(path[1])
            if isinstance(raw, int | float) and raw > 0:
                return float(raw)
        return None

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="alpaca",
            supports_batch_underlyings=False,
            max_concurrency=self._settings.max_concurrency,
            server_side_filters=["type", "expiration_date_gte", "expiration_date_lte"],
            realtime=(self._settings.feed == "opra"),
        )
