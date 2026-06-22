"""Thin synchronous httpx client for the FMP `/stable/` API.

Adds 429/5xx retry with backoff, a client-side rate limiter, and a per-run in-memory
cache that dedupes identical GETs within a single screen.
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from wheel_screener.adapters.http import RateLimiter
from wheel_screener.config import FmpSettings


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


class FmpClient:
    def __init__(
        self,
        settings: FmpSettings,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
        limiter: RateLimiter | None = None,
        cache: dict | None = None,
    ) -> None:
        self._base = settings.base_url.rstrip("/")
        self._key = settings.api_key.get_secret_value()
        self._client = client or httpx.Client(timeout=timeout)
        self._limiter = limiter if limiter is not None else RateLimiter(settings.calls_per_minute)
        self._cache: dict = {} if cache is None else cache

    def get(self, path: str, params: dict | None = None) -> object:
        key = (path, tuple(sorted((params or {}).items())))
        if key in self._cache:
            return self._cache[key]
        data = self._fetch(path, params)
        self._cache[key] = data
        return data

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=30),
        retry=retry_if_exception(_is_retryable),
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
