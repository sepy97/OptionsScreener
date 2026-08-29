"""Long-form fundamental reports, backed by the `fundcore` analysis engine.

The engine lives in a SEPARATE, PRIVATE distribution (``stockanalysis``) that this public
repository deliberately does not depend on: it isn't in ``pyproject.toml`` or the lockfile,
so a clone stays installable by anyone and CI needs no credentials. It is installed only
into the production image and into a developer's venv.

Everything here therefore imports it LAZILY and reports :func:`engine_available` honestly,
so the Fundamentals tab can degrade to an explanatory card instead of 500-ing when the
engine is absent.
"""

from __future__ import annotations

import logging
from importlib import import_module

from wheel_screener.adapters.cache import DiskCache
from wheel_screener.config import FundcoreSettings
from wheel_screener.core.errors import (
    AuthExpiredError,
    ProviderDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from wheel_screener.core.models import FundamentalReport, ReportCell, ReportGroup, ReportRow

logger = logging.getLogger(__name__)

ENGINE_PACKAGE = "fundcore"


def engine_available() -> bool:
    """Whether the analysis engine is importable in this process."""
    try:
        import_module(ENGINE_PACKAGE)
    except ImportError:
        return False
    return True


def engine_version() -> str | None:
    try:
        return getattr(import_module(ENGINE_PACKAGE), "__version__", None)
    except ImportError:
        return None


def _status_of(exc: BaseException | None) -> int | None:
    """HTTP status behind an engine error, if it wrapped one.

    The engine raises ``FMPError(...) from <the underlying HTTP error>``, so the status
    survives on the cause. Read defensively -- this must not import the engine's HTTP
    library, which is only present when the engine is.
    """
    while exc is not None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        exc = exc.__cause__
    return None


def _map_error(exc: Exception, errors) -> ProviderError:
    """Classify an engine failure into the screener's typed hierarchy."""
    if isinstance(exc, errors.ConfigError):
        # the engine had no FMP key -- operator misconfiguration, not a data problem
        return AuthExpiredError(f"fundamentals engine is not configured: {exc}")
    if isinstance(exc, errors.InsufficientData):
        # available == 0 almost always means a typo, not a young company -- say the useful thing
        if getattr(exc, "available", None) == 0:
            symbol = getattr(exc, "symbol", None) or getattr(exc, "ticker", "that symbol")
            return ProviderDataError(f"no data found for {symbol} — is the ticker correct?")
        return ProviderDataError(str(exc))
    if isinstance(exc, errors.FMPError):
        status = _status_of(exc)
        if status in (401, 403):
            return AuthExpiredError(f"fundamentals data provider rejected our key (HTTP {status})")
        if status == 429:
            return RateLimitedError("fundamentals data provider rate limit hit (HTTP 429)")
        if status is not None and status >= 500:
            return ProviderUnavailableError(f"fundamentals data provider error (HTTP {status})")
        if status is not None:
            return ProviderDataError(f"fundamentals request failed (HTTP {status})")
        # no status at all -> transport/timeout rather than a rejected request
        return ProviderUnavailableError(f"fundamentals data provider unreachable: {exc}")
    if isinstance(exc, ValueError):  # e.g. an unsupported period
        return ProviderDataError(str(exc))
    return ProviderError(str(exc))


def _to_core(report) -> FundamentalReport:
    """Map the engine's report onto the screener's own model (the core never sees theirs)."""
    return FundamentalReport(
        symbol=report.symbol,
        period=report.period,
        periods=list(report.periods),
        partial=bool(report.partial),
        groups=[
            ReportGroup(
                key=group.key,
                label=group.label,
                rows=[
                    ReportRow(
                        key=row.key,
                        label=row.label,
                        description=row.description,
                        cells=[ReportCell(value=c.value, grade=c.grade) for c in row.cells],
                    )
                    for row in group.rows
                ],
            )
            for group in report.groups
        ],
    )


class FundcoreReportProvider:
    """A :class:`~wheel_screener.core.ports.FundamentalReportProvider` over the engine.

    Reports are disk-cached: a public page must not spend ~8 upstream calls per view of the
    same ticker, and fundamentals only move when a company files.
    """

    def __init__(
        self,
        api_key: str,
        settings: FundcoreSettings | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        self._api_key = api_key
        self._settings = settings or FundcoreSettings()
        if cache is not None:
            self._cache: DiskCache | None = cache
        elif self._settings.cache_enabled:
            self._cache = DiskCache(
                self._settings.cache_dir, self._settings.cache_ttl_seconds
            )
        else:
            self._cache = None

    def check_auth(self) -> str | None:
        """Whether reports can actually be produced here. None means healthy."""
        if not engine_available():
            return "the fundamentals engine is not installed in this deployment"
        if not self._api_key:
            return "no FMP key configured for the fundamentals engine"
        return None

    def fundamental_report(
        self, symbol: str, period: str = "annual", years: int = 10
    ) -> FundamentalReport:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ProviderDataError("no ticker given")
        # cap the request regardless of what a caller asks for: each period is upstream cost
        years = max(1, min(int(years), self._settings.max_years))

        key = f"{symbol}|{period}|{years}"
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                try:
                    return FundamentalReport.model_validate(cached)
                except Exception as e:  # noqa: BLE001 - a stale/incompatible entry is a miss
                    logger.warning("discarding unusable cached report for %s: %s", key, e)

        try:
            engine = import_module(ENGINE_PACKAGE)
            errors = import_module(f"{ENGINE_PACKAGE}.errors")
        except ImportError as e:
            raise ProviderUnavailableError(
                "the fundamentals engine is not installed in this deployment"
            ) from e

        try:
            raw = engine.build_report(symbol, period=period, years=years, api_key=self._api_key)
        except Exception as e:
            raise _map_error(e, errors) from e

        report = _to_core(raw)
        if self._cache is not None:
            self._cache.set(key, report.model_dump(mode="json"))
        return report
