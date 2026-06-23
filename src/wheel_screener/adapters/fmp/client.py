"""Thin synchronous httpx client for the FMP `/stable/` API.

Adds 429/5xx retry with backoff, a client-side rate limiter, a per-run in-memory cache
(dedupes identical GETs within one screen), and an optional persistent on-disk cache
(``DiskCache``) shared across runs and with the future API server.
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.http import RateLimiter, is_retryable
from wheel_screener.config import FmpSettings


class FmpClient:
    def __init__(
        self,
        settings: FmpSettings,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
        limiter: RateLimiter | None = None,
        cache: dict | None = None,
        disk: DiskCache | None = None,
    ) -> None:
        self._base = settings.base_url.rstrip("/")
        self._key = settings.api_key.get_secret_value()
        self._client = client or httpx.Client(timeout=timeout)
        self._limiter = limiter if limiter is not None else RateLimiter(settings.calls_per_minute)
        self._cache: dict = {} if cache is None else cache
        if disk is not None:
            self._disk: DiskCache | None = disk
        elif settings.cache_enabled:
            self._disk = DiskCache(settings.cache_dir, settings.cache_ttl_seconds)
        else:
            self._disk = None

    def get(self, path: str, params: dict | None = None) -> object:
        norm = tuple(sorted((params or {}).items()))
        mem_key = (path, norm)
        if mem_key in self._cache:
            return self._cache[mem_key]
        disk_key = f"{path}?{norm}"
        if self._disk is not None:
            cached = self._disk.get(disk_key)
            if cached is not None:
                self._cache[mem_key] = cached
                return cached
        data = self._fetch(path, params)
        self._cache[mem_key] = data
        if self._disk is not None:
            self._disk.set(disk_key, data)
        return data

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=30),
        retry=retry_if_exception(is_retryable),
    )
    def _fetch(self, path: str, params: dict | None) -> object:
        self._limiter.acquire()
        query = dict(params or {})
        query["apikey"] = self._key
        resp = self._client.get(f"{self._base}/{path.lstrip('/')}", params=query)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
