"""ChainProvider backed by Alpaca options data (plain REST via httpx).

Alpaca's data API allows ~1000 req/min (vs Schwab's ~120) and authenticates with a key/secret
header — no OAuth. Two calls per underlying, merged by OCC symbol: the *snapshot* (quotes/greeks/
IV) from the data API, and the *contracts* reference (open interest) from the trading API. Each
endpoint paginates via ``next_page_token``. ``feed`` is 'indicative' (free) or 'opra' (paid).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date, timedelta

import httpx

from wheel_screener.adapters.alpaca.mapper import build_chain
from wheel_screener.adapters.cache import DiskCache
from wheel_screener.adapters.errors import ALPACA, map_http_error
from wheel_screener.adapters.http import RateLimiter, run_with_retry
from wheel_screener.config import AlpacaSettings
from wheel_screener.core.errors import ProviderError, ProviderUnavailableError
from wheel_screener.core.models import ChainFilter, ChainSnapshot, OptionType, ProviderCaps

logger = logging.getLogger(__name__)

# The contracts endpoint rejects the WHOLE batch when one underlying isn't listed — but it names
# the offenders, so the recovery is to drop exactly those and retry.
_INVALID_SYMBOLS = re.compile(r"invalid underlying symbols?: ([^\"}]+)")
# The STOCK snapshots endpoint uses a different phrase and names one ticker at a time, so a
# batch of 100 has to be retried repeatedly rather than cleaned up in a single pass.
_INVALID_STOCK_SYMBOL = re.compile(r"invalid symbol: ([A-Za-z0-9.\-]+)")


def _chunks(seq: Sequence[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


class AlpacaChainProvider:
    # underlyings per bulk contracts request; 400 works, 200 keeps URLs and blast radius small
    PREFETCH_BATCH = 200

    def __init__(
        self, settings: AlpacaSettings, client: httpx.Client | None = None, timeout: float = 15.0
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=timeout)
        # the data API (snapshots) and trading API (contracts) enforce SEPARATE rate limits,
        # so each host gets its own limiter rather than sharing one budget (which would 429)
        self._data_limiter = RateLimiter(settings.calls_per_minute)
        self._trading_limiter = RateLimiter(settings.calls_per_minute)
        # Populated by prefetch() before the concurrent pull and only READ during it.
        # (window key, symbols covered, open interest by underlying).
        self._prefetched: tuple[tuple, set[str], dict[str, dict[str, int]]] | None = None
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

    @staticmethod
    def _window(from_date: date, to_date: date, side: OptionType) -> tuple:
        return (from_date, to_date, side.value)

    def _bulk_open_interest(
        self, chunk: list[str], from_date: date, to_date: date, side: OptionType,
        out: dict[str, dict[str, int]], strikes: dict[str, float] | None = None,
    ) -> None:
        """One batched, paginated contracts request, recovering from unlisted underlyings.

        ``strikes`` (optional, OCC symbol -> strike) rides along free: the batched fetch needs a
        strike per contract to decide which ones are worth quoting, and this payload already
        carries it. Fetching it separately would be a second pass over the same rows.
        """
        url = f"{self._settings.trading_base_url.rstrip('/')}/v2/options/contracts"
        remaining, token, drops = list(chunk), None, 0
        while remaining:
            params = {
                "underlying_symbols": ",".join(remaining),
                "type": side.value,
                "status": "active",
                "expiration_date_gte": from_date.isoformat(),
                "expiration_date_lte": to_date.isoformat(),
                "limit": 10000,
            }
            if token:
                params["page_token"] = token
            try:
                page = run_with_retry(
                    lambda p=params: self._get(url, p, self._trading_limiter),
                    max_attempts=self._settings.max_retries + 1,
                    multiplier=self._settings.retry_backoff_multiplier,
                )
            except httpx.HTTPStatusError as e:
                bad = self._unlisted(e)
                if not bad or drops >= 5:
                    raise
                drops += 1
                remaining = [s for s in remaining if s not in bad]
                token = None  # restart pagination; collected rows are re-added idempotently
                logger.info("alpaca: %d underlying(s) not listed, dropped from the batch: %s",
                            len(bad), ",".join(sorted(bad)[:8]))
                continue
            for c in page.get("option_contracts") or []:
                occ, under = c.get("symbol"), c.get("underlying_symbol")
                raw = c.get("open_interest")
                if not occ or not under:
                    continue
                try:
                    out.setdefault(under, {})[occ] = int(raw) if raw is not None else 0
                except (TypeError, ValueError):
                    out.setdefault(under, {})
                if strikes is not None:
                    try:
                        strikes[occ] = float(c["strike_price"])
                    except (KeyError, TypeError, ValueError):
                        pass  # a contract we cannot place against spot simply isn't batched
            token = page.get("next_page_token")
            if not token:
                return

    @staticmethod
    def _unlisted(exc: httpx.HTTPStatusError) -> set[str]:
        """The underlyings a 422 blamed, so they can be dropped rather than failing the batch."""
        if exc.response is None or exc.response.status_code != 422:
            return set()
        m = _INVALID_SYMBOLS.search(exc.response.text or "")
        return {x.strip().strip('"') for x in m.group(1).split(",") if x.strip()} if m else set()

    def prefetch(self, symbols: list[str], filt: ChainFilter) -> set[str]:
        """Bulk-load open interest for many underlyings; return those with NO contracts at all.

        ``underlying_symbols`` is plural, so several hundred names cost ~3 requests instead of one
        each. The saved requests matter less than what the answer reveals: which names have nothing
        in the window, so their snapshot call can be skipped entirely. Measured on a 400-name
        screen, 61% of names had no contracts — and we were spending a request on every one.

        Populated before the concurrent pull and only read during it.
        """
        today = date.today()
        from_date = today + timedelta(days=filt.min_dte if filt.min_dte is not None else 0)
        to_date = today + timedelta(days=filt.max_dte if filt.max_dte is not None else 60)
        side = filt.option_type
        wanted = list(dict.fromkeys(symbols))
        by_symbol: dict[str, dict[str, int]] = {}
        try:
            for i in range(0, len(wanted), self.PREFETCH_BATCH):
                self._bulk_open_interest(
                    wanted[i : i + self.PREFETCH_BATCH], from_date, to_date, side, by_symbol
                )
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, ALPACA) from e
        self._prefetched = (self._window(from_date, to_date, side), set(wanted), by_symbol)
        return {s for s in wanted if s not in by_symbol}

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

        # A matching prefetch already holds this symbol's open interest — don't re-ask for it.
        prefetched = None
        if self._prefetched is not None:
            window, covered, by_symbol = self._prefetched
            if window == self._window(from_date, to_date, side) and symbol in covered:
                prefetched = by_symbol.get(symbol, {})

        try:
            snapshots = self._snapshots(symbol, from_date, to_date, side)
            oi = (
                prefetched
                if prefetched is not None
                else self._open_interest(symbol, from_date, to_date, side)
            )
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

    # ── batched fetch ────────────────────────────────────────────────────────────────────
    def _bulk_spot(self, symbols: list[str]) -> dict[str, float]:
        """Latest price for many underlyings, 100 per request.

        Needed only to place strikes against spot. A name whose price we cannot establish is
        NOT dropped — it goes to the per-name fallback, because guessing its moneyness is the
        one thing that could silently cost a candidate.
        """
        url = f"{self._settings.data_base_url.rstrip('/')}/v2/stocks/snapshots"
        out: dict[str, float] = {}
        for chunk in _chunks(symbols, self._settings.snapshot_batch):
            remaining, drops = list(chunk), 0
            while remaining and drops <= 10:
                try:
                    page = run_with_retry(
                        lambda r=remaining: self._get(
                            url, {"symbols": ",".join(r), "feed": self._settings.stock_feed},
                            self._data_limiter,
                        ),
                        max_attempts=self._settings.max_retries + 1,
                        multiplier=self._settings.retry_backoff_multiplier,
                    )
                except httpx.HTTPStatusError as e:
                    # This endpoint names ONE bad ticker at a time (and with a different phrase
                    # than the contracts endpoint), so recovery is a loop, not a single retry.
                    bad = self._invalid_stock_symbols(e)
                    if not bad:
                        raise  # a real failure (auth/outage) must not look like a bad ticker
                    drops += 1
                    remaining = [x for x in remaining if x not in bad]
                    continue
                for sym, snap in (page or {}).items():
                    px = ((snap.get("latestTrade") or {}).get("p")
                          or (snap.get("dailyBar") or {}).get("c"))
                    if px:
                        try:
                            out[sym] = float(px)
                        except (TypeError, ValueError):
                            pass
                break
        return out

    @staticmethod
    def _invalid_stock_symbols(exc: httpx.HTTPStatusError) -> set[str]:
        """The ticker a 400 from /v2/stocks/snapshots blamed (it reports one at a time)."""
        if exc.response is None or exc.response.status_code != 400:
            return set()
        m = _INVALID_STOCK_SYMBOL.search(exc.response.text or "")
        return {m.group(1).strip()} if m else set()

    def _bulk_snapshots(
        self, option_symbols: list[str], stop: Callable[[], bool]
    ) -> dict[str, dict]:
        """Quotes/greeks/IV for many OPTION symbols, ``snapshot_batch`` per request."""
        url = f"{self._settings.data_base_url.rstrip('/')}/v1beta1/options/snapshots"
        out: dict[str, dict] = {}
        for chunk in _chunks(option_symbols, self._settings.snapshot_batch):
            if stop():
                return out
            params = {
                "symbols": ",".join(chunk),
                "feed": self._settings.feed,
                "limit": self._settings.snapshot_page_limit,
            }
            self._paginate(
                url, params, lambda page: out.update(page.get("snapshots") or {}),
                self._data_limiter,
                attempts=self._settings.max_retries + 1,
                mult=self._settings.retry_backoff_multiplier,
            )
        return out

    def get_chains(
        self, symbols: list[str], filt: ChainFilter, *,
        cancel: threading.Event | None = None, deadline: float | None = None,
    ) -> tuple[dict[str, ChainSnapshot], bool]:
        """Chains for many underlyings in a handful of requests.

        Three bulk calls replace one-request-per-name: the contracts reference (strikes + open
        interest), stock snapshots (spot), and option snapshots for just the strikes near enough
        to spot to be worth quoting. Names whose liquid contracts all fall outside that band --
        or whose spot is unknown -- are fetched one at a time by the old path, so the band only
        ever costs requests, never candidates.
        """
        today = date.today()
        from_date, to_date = self._window_dates(filt, today)
        side = filt.option_type
        min_oi = filt.min_open_interest or 0

        def stop() -> bool:
            return bool(cancel is not None and cancel.is_set()) or (
                deadline is not None and time.monotonic() >= deadline
            )

        out: dict[str, ChainSnapshot] = {}
        todo: list[str] = []
        for sym in symbols:
            cached = self._cached_batch_chain(sym, from_date, to_date, side, today)
            if cached is not None:
                out[sym] = cached
            else:
                todo.append(sym)

        oi_by_name: dict[str, dict[str, int]] = {}
        strikes: dict[str, float] = {}
        try:
            for chunk in _chunks(todo, self.PREFETCH_BATCH):
                if stop():
                    return out, False
                self._bulk_open_interest(chunk, from_date, to_date, side, oi_by_name, strikes)
            if stop():
                return out, False
            spot = self._bulk_spot([s for s in todo if oi_by_name.get(s)])
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            raise map_http_error(e, ALPACA) from e

        wanted: list[str] = []
        fallback: list[str] = []
        for sym in todo:
            contracts = oi_by_name.get(sym) or {}
            if not contracts:
                continue  # no chain at all in this window — nothing to fetch or fall back to
            liquid = [occ for occ, oi in contracts.items() if oi >= min_oi and occ in strikes]
            if not liquid:
                continue  # nothing tradeable; the per-name path would find nothing either
            px = spot.get(sym)
            in_band = [] if px is None else [
                occ for occ in liquid
                if self._settings.strike_band_lo * px
                <= strikes[occ]
                <= self._settings.strike_band_hi * px
            ]
            if in_band:
                wanted.extend(in_band)
            else:
                fallback.append(sym)

        # The vendor already told us which underlying each contract belongs to, so use that
        # rather than parsing the OCC symbol. Parsing looks equivalent and is not: an ADJUSTED
        # contract (HON2..., issued after a corporate action) carries a root no naive pattern
        # matches, and quietly dropping those made the batched path pick a different strike
        # than the per-name path for one name in 817.
        owner = {occ: under for under, per in oi_by_name.items() for occ in per}
        snaps = self._bulk_snapshots(wanted, stop)
        by_name: dict[str, dict[str, dict]] = {}
        for occ, snap in snaps.items():
            under = owner.get(occ)
            if under:
                by_name.setdefault(under, {})[occ] = snap
        for sym, per in by_name.items():
            chain = build_chain(sym, per, oi_by_name.get(sym) or {}, today, spot=spot.get(sym))
            out[sym] = chain
            self._store_batch_chain(sym, from_date, to_date, side, per, oi_by_name.get(sym) or {})

        if fallback:
            logger.info(
                "alpaca: %d/%d name(s) had no liquid strike inside the %.2f-%.2f band; "
                "fetching those individually",
                len(fallback), len(todo),
                self._settings.strike_band_lo, self._settings.strike_band_hi,
            )
        for sym in fallback:
            if stop():
                return out, False
            try:
                out[sym] = self.get_chain(sym, filt)
            except ProviderError:
                raise
            except Exception as e:  # noqa: BLE001 - one name must not sink the batch
                logger.warning("dropping %s: fallback chain fetch failed (%s)", sym, e)
        return out, not stop()

    # A batched chain holds only the strikes near spot, so it MUST NOT share a cache key with
    # get_chain — a ticker search reading that entry would silently get a truncated chain.
    def _batch_cache_key(self, symbol, from_date, to_date, side) -> str:
        return (
            f"alpaca:batch:{symbol}:{from_date}:{to_date}:{self._settings.feed}:"
            f"{side.value.upper()}:{self._settings.strike_band_lo}-{self._settings.strike_band_hi}"
        )

    def _cached_batch_chain(self, symbol, from_date, to_date, side, today):
        if self._cache is None:
            return None
        cached = self._cache.get(self._batch_cache_key(symbol, from_date, to_date, side))
        if not isinstance(cached, dict):
            return None
        return build_chain(symbol, cached.get("snapshots"), cached.get("oi"), today)

    def _store_batch_chain(self, symbol, from_date, to_date, side, snapshots, oi) -> None:
        if self._cache is not None:
            self._cache.set(self._batch_cache_key(symbol, from_date, to_date, side),
                            {"snapshots": snapshots, "oi": oi})

    def _window_dates(self, filt: ChainFilter, today: date) -> tuple[date, date]:
        return (
            today + timedelta(days=filt.min_dte if filt.min_dte is not None else 0),
            today + timedelta(days=filt.max_dte if filt.max_dte is not None else 60),
        )

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            name="alpaca",
            supports_batch_underlyings=True,  # `underlying_symbols` takes a list
            supports_batch_chains=self._settings.batch_chains,
            max_concurrency=self._settings.max_concurrency,
            server_side_filters=["type", "expiration_date_gte", "expiration_date_lte"],
            realtime=(self._settings.feed == "opra"),
        )
