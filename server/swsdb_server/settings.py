"""Configuration from SWSDB_* environment variables."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .engines import parse_engines


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SWSDB_", extra="ignore", populate_by_name=True)

    engines_raw: str = Field("", alias="SWSDB_ENGINES")
    api_key: SecretStr | None = None
    read_only_api_key: SecretStr | None = None
    host: str = "0.0.0.0"
    port: int = 6464
    data_dir: Path = Path("/swsdb/data")
    run_dir: Path = Path("/run/swsdb")
    expose_native: bool = False
    log_level: str = "info"
    # how long /readyz waits for an engine on start before calling it failed rather than starting
    engine_start_timeout_s: float = 120.0

    @field_validator("api_key", "read_only_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.lower()
        if v not in {"debug", "info", "warning", "error"}:
            raise ValueError("SWSDB_LOG_LEVEL must be debug, info, warning or error")
        return v

    @cached_property
    def engines(self) -> tuple[str, ...]:
        return parse_engines(self.engines_raw)

    # where things live
    @property
    def pg_data(self) -> Path:
        return self.data_dir / "postgres"

    @property
    def qdrant_data(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def graph_data(self) -> Path:
        return self.data_dir / "graph"

    @property
    def meta_dir(self) -> Path:
        return self.data_dir / "swsdb"

    @property
    def secrets_file(self) -> Path:
        return self.meta_dir / "secrets.json"

    @property
    def pg_socket_dir(self) -> Path:
        return self.run_dir / "pg"


def load() -> Settings:
    return Settings()
