"""Thin synchronous httpx client for the FMP `/stable/` API with retry/backoff."""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from wheel_screener.config import FmpSettings


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


class FmpClient:
    """GET wrapper that injects the API key and retries 429/5xx with backoff.

    TODO(M1+): a per-provider token-bucket rate limiter + response caching (hishel),
    so a large universe stays inside the tier's calls/min.
    """

    def __init__(
        self, settings: FmpSettings, client: httpx.Client | None = None, timeout: float = 15.0
    ) -> None:
        self._base = settings.base_url.rstrip("/")
        self._key = settings.api_key.get_secret_value()
        self._client = client or httpx.Client(timeout=timeout)

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=30),
        retry=retry_if_exception(_is_retryable),
    )
    def get(self, path: str, params: dict | None = None) -> object:
        query = dict(params or {})
        query["apikey"] = self._key
        resp = self._client.get(f"{self._base}/{path.lstrip('/')}", params=query)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
