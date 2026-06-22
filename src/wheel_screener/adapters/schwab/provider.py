"""ChainProvider backed by Schwab GET /marketdata/v1/chains (via schwab-py)."""

from __future__ import annotations

from datetime import date, timedelta

from wheel_screener.adapters.schwab.mapper import parse_chain
from wheel_screener.config import SchwabSettings
from wheel_screener.core.models import ChainFilter, ChainSnapshot, ProviderCaps


class SchwabChainProvider:
    """Option chains with greeks + IV from Schwab. OAuth/token handled by schwab-py
    (lazy-loaded so the package imports without it and fundamentals-only runs stay light)."""

    def __init__(self, settings: SchwabSettings) -> None:
        self._settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            from wheel_screener.adapters.schwab.auth import load_client

            self._client = load_client(self._settings)
        return self._client

    def get_chain(self, symbol: str, filt: ChainFilter) -> ChainSnapshot:
        from schwab.client import Client

        today = date.today()
        resp = self._get_client().get_option_chain(
            symbol,
            contract_type=Client.Options.ContractType.PUT,
            from_date=today + timedelta(days=filt.min_dte or 0),
            to_date=today + timedelta(days=filt.max_dte or 60),
            strike_count=filt.strike_count or 50,
        )
        resp.raise_for_status()
        return parse_chain(resp.json())

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="schwab",
            supports_batch_underlyings=False,
            max_concurrency=2,
            server_side_filters=["contractType", "strikeCount", "fromDate", "toDate", "range"],
            realtime=True,
        )
