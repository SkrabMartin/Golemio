"""Configuration loading.

Settings come from a TOML file (``config/default.toml`` by default) and can be overridden
by environment variables with the ``HEADWAY_`` prefix, using ``__`` to separate nested
keys, for example ``HEADWAY_COLLECT__INTERVAL_S=30``. Values are not hard-coded in the
models: every setting must be present in the TOML file.

The Golemio API token is deliberately not part of the settings. It is read only from the
``GOLEMIO_TOKEN`` environment variable by :func:`golemio_token`, so it can never be loaded
from, or written back to, a configuration file.
"""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/default.toml")
ENV_PREFIX = "HEADWAY_"
TOKEN_ENV_VAR = "GOLEMIO_TOKEN"


class PathsConfig(BaseModel):
    """Locations of local data directories."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path
    raw_dir: Path
    manifest_dir: Path
    gtfs_dir: Path


class GtfsConfig(BaseModel):
    """Static GTFS snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(description="URL of the static GTFS zip.")
    timeout_s: float = Field(gt=0, description="Download timeout in seconds.")


class CollectConfig(BaseModel):
    """Parameters of the raw-data collector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(description="Vehicle-positions endpoint.")
    params: dict[str, str] = Field(description="Query parameters sent with every poll.")
    interval_s: int = Field(gt=0, description="Polling interval in seconds.")
    timeout_s: float = Field(gt=0, description="Per-request timeout in seconds.")
    max_attempts: int = Field(ge=1, description="Attempts per tick, including the first.")
    backoff_initial_s: float = Field(ge=0, description="First retry delay in seconds.")
    backoff_max_s: float = Field(ge=0, description="Upper bound of a retry delay.")
    manifest_flush_s: int = Field(gt=0, le=300, description="Max seconds between flushes.")
    stale_after_identical: int = Field(ge=2, description="Identical bodies that mean stale.")
    error_streak_alert: int = Field(ge=1, description="Failed ticks in a row that alert.")
    gap_factor: float = Field(gt=1, description="Gap = no success for factor x interval.")
    heartbeat_interval_s: int = Field(gt=0, description="Seconds between heartbeats.")
    ntfy_url: str = Field(description="ntfy topic URL; empty disables notifications.")


class IngestConfig(BaseModel):
    """Parameters of the raw-to-positions ingest (layer L1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service_day_start_min: int = Field(
        ge=0, lt=720, description="Local minutes after midnight at which a service day begins."
    )
    window_before_h: float = Field(ge=0, description="Hours of polls read before a service day.")
    window_after_h: float = Field(ge=0, description="Hours of polls read after a service day.")
    schema_check_every: int = Field(ge=1, description="Check unknown fields on every Nth poll.")
    parser_version: str = Field(description="Written to every row as parser_version.")


class TimeConfig(BaseModel):
    """Time-zone handling; data is stored in UTC."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_tz: str = Field(description="Zone used for display and GTFS time conversion.")


class Settings(BaseSettings):
    """Top-level Headway settings."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    paths: PathsConfig
    collect: CollectConfig
    gtfs: GtfsConfig
    ingest: IngestConfig
    time: TimeConfig

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Let environment variables override the values loaded from the TOML file.

        :func:`load_settings` passes the file contents as init arguments, so the
        environment source is placed first to take precedence. Dotenv and secret-file
        sources are disabled.
        """
        return (env_settings, init_settings)


def load_settings(path: Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Load settings from a TOML file, applying ``HEADWAY_`` environment overrides."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings(**data)


def golemio_token() -> SecretStr | None:
    """Return the Golemio API token from the environment, or ``None`` if it is unset."""
    value = os.environ.get(TOKEN_ENV_VAR, "").strip()
    return SecretStr(value) if value else None
