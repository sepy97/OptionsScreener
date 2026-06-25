"""Typed configuration + secrets via pydantic-settings.

Loaded from environment and ``.env``; nested fields use a ``__`` delimiter, e.g.
``SCHWAB__CLIENT_ID``, ``FMP__API_KEY``, ``IV_RANK__SOURCE``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SchwabSettings(BaseModel):
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    callback_url: str = "https://127.0.0.1:8182"
    token_path: str = ".secrets/schwab_token.json"
    calls_per_minute: int = 110  # client-side throttle (Schwab ~120/min)
    max_retries: int = 3  # retry transient 429/5xx on a chain pull (0 = no retry)
    retry_backoff_multiplier: float = 1.0  # exponential backoff base; 0 disables the wait
    chain_cache_enabled: bool = True
    chain_cache_dir: str = ".cache/schwab"
    chain_cache_ttl_seconds: int = 300  # short TTL: quotes drift, but re-screens stay fast


class FmpSettings(BaseModel):
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://financialmodelingprep.com/stable"
    calls_per_minute: int = 250  # client-side throttle (Starter ~300/min; Free is far lower)
    # persistent HTTP cache (hishel): fundamentals change slowly, so a ~1-day TTL slashes calls
    cache_enabled: bool = True
    cache_dir: str = ".cache/fmp"
    cache_ttl_seconds: int = 86_400


class IvRankSettings(BaseModel):
    source: str = "store"  # store | orats | flashalpha
    db_path: str = "data/iv_history.sqlite"
    flashalpha_api_key: SecretStr = SecretStr("")
    orats_token: SecretStr = SecretStr("")


class LogSettings(BaseModel):
    """Diagnostic logging. The console level follows -v/-vv; the rotating file always
    captures ``file_level`` and up, so cron'd runs leave a recoverable history."""

    dir: str = "logs"
    file: str = "wheel-screener.log"
    file_level: str = "INFO"  # INFO | DEBUG | WARNING | ...
    enable_file: bool = True
    max_bytes: int = 1_000_000  # ~1 MB per file before it rotates
    backup_count: int = 5  # keep this many rotated files


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    schwab: SchwabSettings = Field(default_factory=SchwabSettings)
    fmp: FmpSettings = Field(default_factory=FmpSettings)
    iv_rank: IvRankSettings = Field(default_factory=IvRankSettings)
    log: LogSettings = Field(default_factory=LogSettings)

    # fundamentals source: "local" reads the bulk CSV store; "live" hits FMP per-symbol
    fundamentals_source: str = "local"
    data_dir: str = "data/fundamentals"
    earnings_path: str = "data/earnings_calendar.csv"  # local calendar (refresh-earnings job)
    jobs_db_path: str = "data/jobs.sqlite"  # background screen-job state (web API)
