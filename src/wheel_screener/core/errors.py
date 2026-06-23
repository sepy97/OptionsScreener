"""Typed provider errors that cross the hexagonal boundary.

Adapters raise these (mapping vendor/httpx exceptions) so the pipeline and the delivery
layers can distinguish a transient / auth / outage failure from "no results" — and respond
appropriately (re-auth, back off, 503) instead of masking the failure as an empty screen.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base for any data-provider failure."""


class AuthExpiredError(ProviderError):
    """Credentials / token missing or expired — the caller must re-authenticate."""


class RateLimitedError(ProviderError):
    """Provider rate limit hit — back off and retry later."""


class ProviderUnavailableError(ProviderError):
    """Network/connectivity/5xx — the provider is temporarily unreachable."""


class ProviderDataError(ProviderError):
    """A single item's payload was malformed/unusable — usually skip just that item."""
