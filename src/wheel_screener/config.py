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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    schwab: SchwabSettings = Field(default_factory=SchwabSettings)
    fmp: FmpSettings = Field(default_factory=FmpSettings)
    iv_rank: IvRankSettings = Field(default_factory=IvRankSettings)
