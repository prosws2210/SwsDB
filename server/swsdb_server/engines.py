"""The storage engines swsdb runs, and how a deployment selects them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Engine:
    capability: str  # what the user asks for: sql | vector | graph
    product: str  # what runs it
    ports: tuple[int, ...]  # native ports (localhost unless SWSDB_EXPOSE_NATIVE)
    service: str  # s6 service name


ENGINES: dict[str, Engine] = {
    "sql": Engine("sql", "postgres", (5432,), "svc-postgres"),
    "vector": Engine("vector", "qdrant", (6333, 6334), "svc-qdrant"),
    "graph": Engine("graph", "falkordb", (6379,), "svc-falkordb"),
}

# What people type: a capability, an engine name, or "all"
ALIASES = {
    "sql": "sql", "relational": "sql", "postgres": "sql", "postgresql": "sql", "pg": "sql",
    "vector": "vector", "vectors": "vector", "qdrant": "vector",
    "graph": "graph", "falkordb": "graph",
}


# SWSDB_PROFILE=lite: vectors and graphs live in PostgreSQL (pgvector, Apache AGE), so Postgres is the only process
LITE_PRODUCTS = {"vector": "pgvector", "graph": "age"}
PROFILES = ("default", "lite")


def product(cap: str, profile: str = "default") -> str:
    """What serves a capability in a profile."""
    if profile == "lite" and cap in LITE_PRODUCTS:
        return LITE_PRODUCTS[cap]
    return ENGINES[cap].product


class EngineSelectionError(ValueError):
    pass


def parse_engines(value: str | None) -> tuple[str, ...]:
    """SWSDB_ENGINES -> capabilities in a stable order. Empty or unset means all; unknown names are an error, not a
    silent drop, because a typo would otherwise start a database without the store the user expects."""
    raw = [p.strip().lower() for p in (value or "").replace(";", ",").replace(" ", ",").split(",") if p.strip()]
    if not raw or raw == ["all"]:
        return tuple(ENGINES)
    unknown = [p for p in raw if p not in ALIASES and p != "all"]
    if unknown:
        valid = ", ".join(sorted({*ENGINES, *ALIASES, "all"}))
        raise EngineSelectionError(f"SWSDB_ENGINES: unknown engine {', '.join(map(repr, unknown))}. Valid: {valid}")
    if "all" in raw:
        return tuple(ENGINES)
    chosen = {ALIASES[p] for p in raw}
    return tuple(c for c in ENGINES if c in chosen)
