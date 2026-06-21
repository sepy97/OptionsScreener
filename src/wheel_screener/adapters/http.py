"""Shared HTTP layer for provider adapters.

A single httpx client per provider, wrapped with response caching (hishel),
retry/backoff (tenacity), and a per-provider rate limiter so the combined pipeline
stays within FMP (250/day on free) and Schwab (~120/min) limits.

TODO(M2): client factory + token-bucket rate limiter + retry policy.
"""

from __future__ import annotations
