"""ChainProvider backed by Schwab GET /marketdata/v1/chains."""

from __future__ import annotations

from wheel_screener.config import SchwabSettings
from wheel_screener.core.models import ChainFilter, ChainSnapshot, ProviderCaps


class SchwabChainProvider:
    """Option chains with greeks + IV from Schwab.

    Auth is OAuth2 with a token file (30-min access token auto-refreshed; 7-day
    refresh token re-login via the ``auth-login`` command). One request per
    underlying, throttled to ~120 req/min.

    TODO(M2): OAuth/token-file, httpx client, callExpDateMap/putExpDateMap parsing,
    and Schwab JSON -> core model mapping.
    """

    def __init__(self, settings: SchwabSettings) -> None:
        self._settings = settings

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        raise NotImplementedError("Schwab chain pull lands in M2")

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="schwab",
            supports_batch_underlyings=False,
            max_concurrency=2,
            server_side_filters=["contractType", "strikeCount", "fromDate", "toDate", "range"],
            realtime=True,
        )
