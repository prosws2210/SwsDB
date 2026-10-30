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

    # documents: embeddings (dense model; sparse BM25 is always local) and optional entity extraction
    embeddings: str = "local"  # local | openai (any OpenAI-compatible /embeddings endpoint)
    embeddings_model: str | None = None
    embeddings_url: str | None = None
    embeddings_api_key: SecretStr | None = None
    llm_url: str | None = None  # OpenAI-compatible base URL, e.g. https://api.openai.com/v1 or http://ollama:11434/v1
    llm_model: str | None = None  # unset: no entity extraction (the graph still holds document structure)
    llm_api_key: SecretStr | None = None
    models_dir: Path = Path("/opt/swsdb/models")
    ingest_workers: int = Field(2, ge=1, le=32)
    reconcile_interval_s: float = Field(600, ge=0)  # compare catalog and stores this often (0: only on request)
    max_upload_mb: int = Field(100, ge=1, le=4096)

    @field_validator("embeddings_api_key", "llm_api_key", mode="before")
    @classmethod
    def _blank_secret(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("embeddings")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = v.lower()
        if v not in {"local", "openai"}:
            raise ValueError("SWSDB_EMBEDDINGS must be 'local' or 'openai' (any OpenAI-compatible endpoint)")
        return v

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

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
