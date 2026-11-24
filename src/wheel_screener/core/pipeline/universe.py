"""Stage 1 — build the candidate universe from cheap price/market-cap/exchange data."""

from __future__ import annotations

import logging
import math

from wheel_screener.core.errors import ProviderError
from wheel_screener.core.models import ChainSnapshot, ScreenCriteria, Underlying
from wheel_screener.core.ports import FundamentalsProvider, QuoteProvider

logger = logging.getLogger(__name__)


def build_universe(
    provider: FundamentalsProvider, criteria: ScreenCriteria, quotes: QuoteProvider | None = None,
) -> list[Underlying]:
    """Return the full price/market-cap/exchange-filtered universe (FMP company-screener).

    No truncation here — stage 2 either bulk-pre-ranks the whole universe or (when bulk
    is unavailable) caps the deep fetch by market cap.

    With ``quotes``, the price range is judged on live prices rather than the provider's own.
    """
    if quotes is None:
        universe = provider.screen_universe(criteria)
    else:
        universe = _priced_live(provider, criteria, quotes)
    universe = drop_blocked(universe, criteria)
    logger.info(
        "universe: %d names ($%g-%g, %s)",
        len(universe), criteria.min_price, criteria.max_price, "/".join(criteria.exchanges),
    )
    return universe


def _priced_live(
    provider: FundamentalsProvider, criteria: ScreenCriteria, quotes: QuoteProvider
) -> list[Underlying]:
    """The universe cut on today's prices rather than the store's.

    The store's price is as old as its last bulk import, so a name that has since crossed into
    the range was never screened, and one that has left it was. Every name is asked for at any
    price, priced live in batches, and cut on that. A name with no live quote keeps its stored
    price; with no quotes at all, the stored prices decide, as they did before.
    """
    every = provider.screen_universe(
        criteria.model_copy(update={"min_price": 0.0, "max_price": math.inf}))
    try:
        live = quotes.last_prices([u.symbol for u in every])
    except ProviderError as e:
        logger.warning("live prices unavailable (%s); the price range uses the store's", e)
        live = {}
    kept = []
    for u in every:
        price = live.get(u.symbol) or u.price
        if price is not None and criteria.min_price <= price <= criteria.max_price:
            u.price = price
            kept.append(u)
    logger.info("live prices: %d of %d names priced live", len(live), len(every))
    return kept


def drop_blocked(names: list[Underlying], criteria: ScreenCriteria) -> list[Underlying]:
    """Leave out every name on the blocklist the screen started with."""
    if not criteria.blocked_symbols:
        return names
    kept = [u for u in names if u.symbol.upper() not in criteria.blocked_symbols]
    if len(kept) < len(names):
        logger.info("blocklist: left out %d name(s) — %s", len(names) - len(kept),
                    ", ".join(sorted({u.symbol for u in names} - {u.symbol for u in kept})))
    return kept


def in_price_range(
    chains: dict[str, ChainSnapshot], criteria: ScreenCriteria
) -> dict[str, ChainSnapshot]:
    """The chains whose share price, as of the pull, is inside the screen's price range.

    The universe may have been cut on the fundamentals store's price, which is only as fresh as
    the store's last bulk import — FCX was kept in a $50-150 screen while trading at $46.78. The
    chain carries the price as of now, so the range is judged again on it. A chain without a
    spot (Alpaca's are option-only) keeps the universe's verdict.
    """
    kept, left = {}, []
    for symbol, chain in chains.items():
        spot = chain.underlying_price
        if spot and not criteria.min_price <= spot <= criteria.max_price:
            left.append(f"{symbol} ${spot:,.2f}")
        else:
            kept[symbol] = chain
    if left:
        logger.info("price: left out %d name(s) now outside $%g-%g — %s",
                    len(left), criteria.min_price, criteria.max_price, ", ".join(sorted(left)))
    return kept
