"""Configuration from SWSDB_* environment variables."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .engines import LITE_PRODUCTS, PROFILES, EngineSelectionError, parse_engines, product

# Secrets can come from files instead (Docker and Kubernetes secrets): SWSDB_API_KEY_FILE=/run/secrets/swsdb_key.
# Environment variables show up in `docker inspect`; mounted files don't.
SECRET_FILES = ("API_KEY", "READ_ONLY_API_KEY", "LLM_API_KEY", "EMBEDDINGS_API_KEY", "BACKUP_S3_ACCESS_KEY",
                "BACKUP_S3_SECRET_KEY", "POSTGRES_URL", "QDRANT_API_KEY", "FALKORDB_URL", "NEO4J_PASSWORD")


def secrets_from_files(environ: dict | None = None) -> None:
    env = os.environ if environ is None else environ
    for name in SECRET_FILES:
        path = env.get(f"SWSDB_{name}_FILE")
        if path and not env.get(f"SWSDB_{name}"):
            try:
                env[f"SWSDB_{name}"] = Path(path).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ValueError(f"SWSDB_{name}_FILE: can't read {path}: {e.strerror}") from None


CONFIG_FILE = Path("/swsdb/config/swsdb.yaml")


def read_config_file(path: Path, fields: set[str]) -> dict:
    """swsdb.yaml: the same settings as the SWSDB_* variables, lower case without the prefix. Sections are joined with
    "_" (`backup: {s3: {bucket: x}}` is backup_s3_bucket), and `engines` may be a list. Unknown keys are an error."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of settings")
    flat: dict = {}

    def walk(prefix: str, value) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}_{k}" if prefix else str(k), v)
        else:
            flat[prefix.lower()] = value

    walk("", data)
    if "engines" in flat:
        engines = flat.pop("engines")
        flat["engines_raw"] = ",".join(engines) if isinstance(engines, list) else str(engines)
    unknown = sorted(set(flat) - fields)
    if unknown:
        raise ValueError(f"{path}: unknown settings {', '.join(unknown)} (see docs/reference/configuration.md)")
    return flat


class _ConfigFile(PydanticBaseSettingsSource):
    """Settings from SWSDB_CONFIG_FILE (default /swsdb/config/swsdb.yaml), below environment variables."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple:  # pragma: no cover - __call__ is used
        return None, field_name, False

    def __call__(self) -> dict:
        path = Path(os.environ.get("SWSDB_CONFIG_FILE") or CONFIG_FILE)
        if not path.is_file():
            if os.environ.get("SWSDB_CONFIG_FILE"):
                raise ValueError(f"SWSDB_CONFIG_FILE: {path} not found")
            return {}
        values = read_config_file(path, set(self.settings_cls.model_fields))
        if "engines_raw" in values:  # the field reads SWSDB_ENGINES by alias
            values["SWSDB_ENGINES"] = values.pop("engines_raw")
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SWSDB_", extra="ignore", populate_by_name=True)

    def __init__(self, **values):
        secrets_from_files()
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # explicit arguments, then SWSDB_* variables, then swsdb.yaml
        return init_settings, env_settings, _ConfigFile(settings_cls), file_secret_settings

    engines_raw: str = Field("", alias="SWSDB_ENGINES")
    # default: Qdrant and FalkorDB beside Postgres; lite: vectors (pgvector) and graphs (Apache AGE) in Postgres
    profile: str = "default"
    api_key: SecretStr | None = None
    read_only_api_key: SecretStr | None = None
    host: str = "0.0.0.0"
    port: int = 6464
    data_dir: Path = Path("/swsdb/data")
    run_dir: Path = Path("/run/swsdb")
    expose_native: bool = False
    # host names (and browser origins) accepted besides localhost, IP addresses and the container's hostname
    allowed_hosts: str = ""
    log_level: str = "info"
    # engines running elsewhere (other containers, managed services): set one and swsdb uses it instead of its own
    postgres_url: SecretStr | None = None  # postgresql://user:password@host:5432/swsdb (user may CREATE ROLE)
    qdrant_url: str | None = None  # http://qdrant:6333 or https://xyz.cloud.qdrant.io:6333
    qdrant_api_key: SecretStr | None = None
    falkordb_url: SecretStr | None = None  # redis://:password@falkordb:6379 (rediss:// for TLS)
    # or an existing Neo4j as the graph engine (HTTP Query API, Neo4j 5.19 or later): graph retrieval works, and
    # pass-through Cypher is off because collections share one Neo4j database
    neo4j_url: str | None = None  # http://neo4j:7474 or https://...
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr | None = None
    neo4j_database: str = "neo4j"
    # HTTPS on the API port: mount a certificate and its key, e.g. -v ./tls:/tls:ro
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
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
    answer_model: str | None = None  # cited answers; default: llm_model (no model: extractive answers)
    rerank: bool = False  # re-rank search hits with the bundled cross-encoder unless a query says otherwise
    # entities for the graph: auto = the chat model when one is set, else names found in the text; llm | names | off
    entities: str = "auto"
    models_dir: Path = Path("/opt/swsdb/models")
    ingest_workers: int = Field(2, ge=1, le=32)
    reconcile_interval_s: float = Field(600, ge=0)  # compare catalog and stores this often (0: only on request)
    max_upload_mb: int = Field(100, ge=1, le=4096)
    # where uploaded originals wait to be processed: disk (this server's volume) | postgres (shared by every server
    # on the database: needed to run several servers)
    files: str = "disk"
    server_id: str | None = None  # this server's name among several (default: the host name)

    # scheduled backups: a snapshot every N hours (0: off), copied to S3-compatible storage when a bucket is set
    backup_interval_h: float = Field(0, ge=0)
    backup_keep: int = Field(7, ge=1, le=1000)
    backup_s3_url: str | None = None  # e.g. https://s3.eu-west-1.amazonaws.com or http://minio:9000
    backup_s3_bucket: str | None = None
    backup_s3_prefix: str = "swsdb"
    backup_s3_region: str = "us-east-1"
    backup_s3_access_key: SecretStr | None = None
    backup_s3_secret_key: SecretStr | None = None

    @field_validator("embeddings_api_key", "llm_api_key", mode="before")
    @classmethod
    def _blank_secret(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("files")
    @classmethod
    def _files(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"disk", "postgres"}:
            raise ValueError("SWSDB_FILES must be disk or postgres")
        return v

    @field_validator("profile")
    @classmethod
    def _profile(cls, v: str) -> str:
        v = v.strip().lower() or "default"
        if v not in PROFILES:
            raise ValueError(f"SWSDB_PROFILE must be one of {', '.join(PROFILES)}")
        return v

    @field_validator("entities")
    @classmethod
    def _entities(cls, v: str) -> str:
        v = v.lower()
        if v not in {"auto", "llm", "names", "off"}:
            raise ValueError("SWSDB_ENTITIES must be auto, llm, names or off")
        return v

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

    def in_postgres(self, cap: str) -> bool:
        """Whether a capability is served by PostgreSQL itself (the lite profile's vectors and graphs)."""
        return self.profile == "lite" and cap in LITE_PRODUCTS

    def external(self, cap: str) -> bool:
        """Whether a capability's engine runs outside this container."""
        if self.in_postgres(cap):
            return self.external("sql")
        return bool({"sql": self.postgres_url, "vector": self.qdrant_url,
                     "graph": self.falkordb_url or self.neo4j_url}.get(cap))

    def product(self, cap: str) -> str:
        if cap == "graph" and self.neo4j_url and self.profile != "lite":
            return "neo4j"
        return product(cap, self.profile)

    @property
    def local_engines(self) -> tuple[str, ...]:
        """The engine processes this container runs itself."""
        return tuple(c for c in self.engines if not self.external(c) and not self.in_postgres(c))

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def tls(self) -> bool:
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("set both SWSDB_TLS_CERT_FILE and SWSDB_TLS_KEY_FILE, or neither")
        return self.tls_cert_file is not None

    @property
    def local_url(self) -> str:
        """The API as seen from inside the container (health checks, swsdb-admin)."""
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    @cached_property
    def engines(self) -> tuple[str, ...]:
        chosen = parse_engines(self.engines_raw)
        if self.profile == "lite":
            if "sql" not in chosen:
                raise EngineSelectionError("SWSDB_PROFILE=lite keeps vectors and graphs in PostgreSQL: SWSDB_ENGINES "
                                           "must include sql")
            if self.qdrant_url or self.falkordb_url or self.neo4j_url:
                raise EngineSelectionError("SWSDB_PROFILE=lite keeps vectors and graphs in PostgreSQL: unset "
                                           "SWSDB_QDRANT_URL, SWSDB_FALKORDB_URL and SWSDB_NEO4J_URL (or use the "
                                           "default profile)")
        if self.falkordb_url and self.neo4j_url:
            raise EngineSelectionError("set SWSDB_FALKORDB_URL or SWSDB_NEO4J_URL, not both: there is one graph engine")
        return chosen

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
