"""ChainProvider backed by Schwab GET /marketdata/v1/chains (via schwab-py)."""

from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.errors import map_http_error
from wheel_screener.adapters.http import RateLimiter, run_with_retry
from wheel_screener.adapters.schwab.mapper import parse_chain
from wheel_screener.config import SchwabSettings
from wheel_screener.core.errors import ProviderError, ProviderUnavailableError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, ProviderCaps


class SchwabChainProvider:
    """Option chains with greeks + IV from Schwab. OAuth/token handled by schwab-py
    (lazy-loaded so the package imports without it and fundamentals-only runs stay light)."""

    def __init__(self, settings: SchwabSettings) -> None:
        self._settings = settings
        self._client = None
        self._limiter = RateLimiter(settings.calls_per_minute)
        self._cache: DiskCache | None = (
            DiskCache(settings.chain_cache_dir, settings.chain_cache_ttl_seconds)
            if settings.chain_cache_enabled
            else None
        )

    def _get_client(self):
        if self._client is None:
            from wheel_screener.adapters.schwab.auth import load_client

            self._client = load_client(self._settings)
        return self._client

    def _fetch_payload(self, symbol: str, from_date: date, to_date: date, strike_count: int):
        from schwab.client import Client

        self._limiter.acquire()  # re-acquired per attempt so retries respect the rate limit
        resp = self._get_client().get_option_chain(
            symbol,
            contract_type=Client.Options.ContractType.PUT,
            from_date=from_date,
            to_date=to_date,
            strike_count=strike_count,
        )
        resp.raise_for_status()
        return resp.json()

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        import httpx

        today = date.today()
        from_date = today + timedelta(days=filt.min_dte or 0)
        to_date = today + timedelta(days=filt.max_dte or 60)
        strike_count = filt.strike_count or 50
        cache_key = f"chain:{symbol}:{from_date}:{to_date}:{strike_count}:PUT"

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return parse_chain(cached)

        try:
            payload = run_with_retry(
                lambda: self._fetch_payload(symbol, from_date, to_date, strike_count),
                max_attempts=self._settings.max_retries + 1,
                multiplier=self._settings.retry_backoff_multiplier,
            )
        except ProviderError:
            raise  # e.g. AuthExpiredError from token load — never mask it (and never retried)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e) from e  # transient kinds already retried + exhausted
        except Exception as e:  # vendor/authlib failure: surface as a provider problem
            raise ProviderUnavailableError(f"schwab chain fetch failed for {symbol}: {e}") from e

        if self._cache is not None:
            self._cache.set(cache_key, payload)
        return parse_chain(payload)

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="schwab",
            supports_batch_underlyings=False,
            max_concurrency=self._settings.max_concurrency,
            server_side_filters=["contractType", "strikeCount", "fromDate", "toDate", "range"],
            realtime=True,
        )
