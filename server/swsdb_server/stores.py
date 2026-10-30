"""Adapters over the engines. The API talks to these interfaces, never to an engine's client directly, so an engine can
be swapped (or moved out of the container) without touching the API."""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .names import parse_engine_name, point_id

# ------------------------------------------------------------------ errors the API maps to status codes


class StoreError(Exception):
    """A request the engine rejected (bad query, wrong dimensions...): the caller's to fix (HTTP 400)."""


class NotFound(StoreError):
    """HTTP 404."""


class Conflict(StoreError):
    """HTTP 409."""


class Unavailable(Exception):
    """The engine didn't answer (HTTP 503)."""


# ------------------------------------------------------------------ SQL (PostgreSQL)

# Pass-through SQL connects as this role: a plain login role (not a superuser), so a statement can't escalate back
# to swsdb's own privileges. It owns `public`; swsdb's tables live in schema `swsdb`, which it can't touch.
APP_ROLE = "swsdb_app"

MIGRATIONS: list[str] = [
    # 1: the sandbox role for user SQL (must match APP_ROLE)
    """
    REVOKE ALL ON SCHEMA swsdb FROM PUBLIC;
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'swsdb_app') THEN
        CREATE ROLE swsdb_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
      END IF;
    END $$;
    ALTER SCHEMA public OWNER TO swsdb_app;
    GRANT USAGE, CREATE ON SCHEMA public TO swsdb_app;
    """,
    # 2: the document catalog. Postgres is the source of truth: vectors and graph can be rebuilt from these rows.
    """
    CREATE TABLE swsdb.collections (
      tenant text NOT NULL, name text NOT NULL,
      embedding text NOT NULL, dim int NOT NULL, graph boolean NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant, name));
    CREATE TABLE swsdb.documents (
      id uuid PRIMARY KEY, tenant text NOT NULL, collection text NOT NULL, external_id text NOT NULL,
      title text, filename text, kind text, metadata jsonb NOT NULL DEFAULT '{}', readers text[] NOT NULL,
      version int NOT NULL DEFAULT 1, status text NOT NULL, error text,
      chunks int NOT NULL DEFAULT 0, entities int NOT NULL DEFAULT 0, bytes bigint, sha256 text,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant, collection) REFERENCES swsdb.collections ON DELETE CASCADE,
      UNIQUE (tenant, collection, external_id));
    CREATE INDEX documents_pending ON swsdb.documents (updated_at) WHERE status IN ('queued', 'processing');
    CREATE TABLE swsdb.chunks (
      id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES swsdb.documents ON DELETE CASCADE,
      version int NOT NULL, ordinal int NOT NULL, text text NOT NULL, page int, heading text, hash text NOT NULL,
      UNIQUE (document_id, version, ordinal));
    CREATE TABLE swsdb.chunk_entities (
      chunk_id uuid NOT NULL REFERENCES swsdb.chunks ON DELETE CASCADE,
      entity_key text NOT NULL, name text NOT NULL, type text NOT NULL, PRIMARY KEY (chunk_id, entity_key));
    CREATE TABLE swsdb.chunk_relations (
      chunk_id uuid NOT NULL REFERENCES swsdb.chunks ON DELETE CASCADE,
      source text NOT NULL, target text NOT NULL, type text NOT NULL, PRIMARY KEY (chunk_id, source, target, type));
    """,
    # 3: consistency. Every change is an outbox row written in the same transaction as the catalog change; workers
    # apply it to the derived stores (idempotently, with retries). live_version is the version queries may return.
    """
    ALTER TABLE swsdb.documents ADD COLUMN live_version int;
    UPDATE swsdb.documents SET live_version = version WHERE status = 'ready';
    ALTER TABLE swsdb.documents ADD COLUMN attempts int NOT NULL DEFAULT 0;
    ALTER TABLE swsdb.chunks ADD COLUMN extracted boolean NOT NULL DEFAULT false;
    UPDATE swsdb.chunks c SET extracted = true
      WHERE EXISTS (SELECT 1 FROM swsdb.chunk_entities e WHERE e.chunk_id = c.id);
    CREATE TABLE swsdb.outbox (
      id bigserial PRIMARY KEY,
      document_id uuid NOT NULL REFERENCES swsdb.documents ON DELETE CASCADE,
      op text NOT NULL CHECK (op IN ('index', 'delete', 'graph')),
      version int NOT NULL,
      attempts int NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      locked_until timestamptz,
      last_error text,
      created_at timestamptz NOT NULL DEFAULT now());
    CREATE INDEX outbox_due ON swsdb.outbox (next_attempt_at);
    CREATE INDEX outbox_document ON swsdb.outbox (document_id, id);
    INSERT INTO swsdb.outbox (document_id, op, version)
      SELECT id, 'index', version FROM swsdb.documents WHERE status IN ('queued', 'processing');
    """,
]


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


class SqlStore:
    def __init__(self, socket_dir: str, dbname: str = "swsdb", max_size: int = 10):
        def pool(user: str, size: int) -> AsyncConnectionPool:
            return AsyncConnectionPool(
                conninfo=psycopg.conninfo.make_conninfo(host=socket_dir, dbname=dbname, user=user, connect_timeout=5),
                min_size=0, max_size=size, open=False, kwargs={"autocommit": True})
        self.pool = pool("swsdb", 4)  # swsdb's own work: migrations, catalog
        self.app = pool(APP_ROLE, max_size)  # pass-through SQL
        self._migrated = False

    async def open(self) -> None:
        await self.pool.open(wait=False)
        await self.app.open(wait=False)

    async def close(self) -> None:
        await self.app.close()
        await self.pool.close()

    async def migrate(self) -> None:
        """Apply swsdb's own schema migrations once per process (idempotent and recorded)."""
        if self._migrated:
            return
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7367736462)")  # one migrator at a time
            await conn.execute("CREATE SCHEMA IF NOT EXISTS swsdb")
            await conn.execute("CREATE TABLE IF NOT EXISTS swsdb.migrations (version int PRIMARY KEY, "
                               "applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r[0] for r in await (await conn.execute("SELECT version FROM swsdb.migrations")).fetchall()}
            for version, ddl in enumerate(MIGRATIONS, start=1):
                if version not in done:
                    await conn.execute(ddl)
                    await conn.execute("INSERT INTO swsdb.migrations (version) VALUES (%s)", [version])
        self._migrated = True

    async def query(self, statement: str, params: list | dict | None, *, read_only: bool, timeout_s: float,
                    max_rows: int) -> dict:
        """One statement as the sandbox role, in its own transaction. Server-side prepared, so a string holding several
        statements is rejected by Postgres (no `COMMIT; ...` escaping read-only mode or the timeout)."""
        await self.migrate()
        try:
            async with self.app.connection(timeout=10) as conn, conn.transaction():
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SELECT set_config('statement_timeout', %s, true)", [f"{int(timeout_s * 1000)}"])
                cur = await conn.execute(statement, params or None, prepare=True)
                if cur.description is None:
                    return {"columns": [], "rows": [], "row_count": cur.rowcount, "truncated": False}
                rows = await cur.fetchmany(max_rows + 1)
                return {
                    "columns": [d.name for d in cur.description],
                    "rows": [[_json_safe(v) for v in r] for r in rows[:max_rows]],
                    "row_count": min(len(rows), max_rows),
                    "truncated": len(rows) > max_rows,
                }
        except psycopg.errors.QueryCanceled as e:  # statement_timeout: the query's fault, not an outage
            raise StoreError(f"statement timed out after {timeout_s:g}s") from e
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available (pool exhausted or server unreachable)") from e
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            if isinstance(e, psycopg.errors.DatabaseError) and e.sqlstate and not e.sqlstate.startswith("08"):
                raise StoreError(f"{type(e).__name__}: {e}".strip()) from e  # e.g. out of memory for this query
            raise Unavailable(f"PostgreSQL: {e}") from e
        except psycopg.Error as e:
            raise StoreError(f"{type(e).__name__}: {e}".strip()) from e


# ------------------------------------------------------------------ vectors (Qdrant)

DISTANCES = {"cosine": "Cosine", "dot": "Dot", "euclid": "Euclid", "manhattan": "Manhattan"}


@dataclass
class VectorSpec:
    size: int
    distance: str = "cosine"


def _qdrant_filter(where: dict[str, Any] | None) -> dict | None:
    """`{"team": "support", "year": [2024, 2025]}` -> a Qdrant filter: every key must match (lists mean any of)."""
    if not where:
        return None
    must = []
    for key, value in where.items():
        if not isinstance(key, str) or not key or key.startswith("_"):
            raise StoreError(f"invalid filter field {key!r}")
        if isinstance(value, list):
            must.append({"key": key, "match": {"any": value}})
        elif isinstance(value, (str, int, bool)):
            must.append({"key": key, "match": {"value": value}})
        else:
            raise StoreError(f"filter on {key!r}: use a string, integer, boolean or a list of them")
    return {"must": must}


class VectorStore:
    def __init__(self, url: str, api_key: str):
        self.http = httpx.AsyncClient(base_url=url, timeout=30.0, headers={"api-key": api_key})

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise Unavailable(f"Qdrant: {type(e).__name__}") from e
        if r.status_code == 404:
            raise NotFound(r.json().get("status", {}).get("error", "not found") if r.content else "not found")
        if r.status_code == 409:
            raise Conflict(r.json().get("status", {}).get("error", "already exists"))
        if r.status_code >= 500:
            raise Unavailable(f"Qdrant answered {r.status_code}")
        body = r.json()
        if r.status_code >= 400:
            raise StoreError(body.get("status", {}).get("error", f"Qdrant answered {r.status_code}"))
        return body

    async def list(self, tenant: str) -> list[str]:
        cols = (await self._call("GET", "/collections"))["result"]["collections"]
        return sorted(n for c in cols if (n := parse_engine_name(c["name"], tenant)))

    async def create(self, name: str, spec: VectorSpec) -> None:
        if spec.distance not in DISTANCES:
            raise StoreError(f"distance must be one of {', '.join(DISTANCES)}")
        if not 1 <= spec.size <= 65536:
            raise StoreError("vector size must be between 1 and 65536")
        await self._call("PUT", f"/collections/{name}",
                         json={"vectors": {"size": spec.size, "distance": DISTANCES[spec.distance]}})

    async def info(self, name: str) -> dict:
        r = (await self._call("GET", f"/collections/{name}"))["result"]
        params = r["config"]["params"]
        v = params["vectors"]
        if "size" not in v and "dense" in v:  # a documents collection: named dense + sparse vectors
            v = {**v["dense"], "sparse": list(params.get("sparse_vectors") or {})}
        dist = {q: k for k, q in DISTANCES.items()}.get(v.get("distance"), v.get("distance"))
        out = {"size": v.get("size"), "distance": dist, "points": r.get("points_count"), "status": r.get("status")}
        if v.get("sparse"):
            out["sparse"] = v["sparse"]
        return out

    async def drop(self, name: str) -> bool:
        return bool((await self._call("DELETE", f"/collections/{name}"))["result"])

    async def upsert(self, name: str, points: list[dict]) -> int:
        out = []
        for p in points:
            pid = point_id(p["id"])
            payload = dict(p.get("payload") or {})
            if any(k.startswith("_") for k in payload):
                raise StoreError("payload keys starting with '_' are reserved for swsdb")
            payload["_id"] = p["id"]
            out.append({"id": pid, "vector": p["vector"], "payload": payload})
        await self._call("PUT", f"/collections/{name}/points", params={"wait": "true"}, json={"points": out})
        return len(out)

    async def search(self, name: str, vector: list[float], limit: int, where: dict | None,
                     with_payload: bool = True) -> list[dict]:
        body: dict[str, Any] = {"query": vector, "limit": limit, "with_payload": True}
        if (f := _qdrant_filter(where)) is not None:
            body["filter"] = f
        hits = (await self._call("POST", f"/collections/{name}/points/query", json=body))["result"]["points"]
        out = []
        for h in hits:
            payload = dict(h.get("payload") or {})
            original = payload.pop("_id", h["id"])
            out.append({"id": original, "score": h["score"], **({"payload": payload} if with_payload else {})})
        return out

    async def get(self, name: str, ids: list[str | int]) -> list[dict]:
        r = await self._call("POST", f"/collections/{name}/points",
                             json={"ids": [point_id(i) for i in ids], "with_payload": True, "with_vector": True})
        out = []
        for p in r["result"]:
            payload = dict(p.get("payload") or {})
            out.append({"id": payload.pop("_id", p["id"]), "vector": p.get("vector"), "payload": payload})
        return out

    async def delete(self, name: str, ids: list[str | int]) -> None:
        await self._call("POST", f"/collections/{name}/points/delete", params={"wait": "true"},
                         json={"points": [point_id(i) for i in ids]})

    # -- document collections: named dense + sparse (BM25) vectors, chunks invisible until their document is ready

    async def create_documents(self, name: str, dim: int) -> None:
        await self._call("PUT", f"/collections/{name}", json={
            "vectors": {"dense": {"size": dim, "distance": "Cosine"}},
            "sparse_vectors": {"sparse": {"modifier": "idf"}},
            "on_disk_payload": True})
        for field, schema in (("document_id", "keyword"), ("readers", "keyword"), ("live", "bool"),
                              ("version", "integer")):
            await self._call("PUT", f"/collections/{name}/index", params={"wait": "true"},
                             json={"field_name": field, "field_schema": schema})

    async def is_documents(self, name: str) -> bool:
        r = (await self._call("GET", f"/collections/{name}"))["result"]["config"]["params"]
        return isinstance(r.get("vectors"), dict) and "dense" in r["vectors"] and "sparse" in (r.get("sparse_vectors")
                                                                                             or {})

    async def upsert_chunks(self, name: str, points: list[dict]) -> None:
        for i in range(0, len(points), 256):
            await self._call("PUT", f"/collections/{name}/points", params={"wait": "true"},
                             json={"points": points[i:i + 256]})

    async def set_payload(self, name: str, payload: dict, flt: dict) -> None:
        await self._call("POST", f"/collections/{name}/points/payload", params={"wait": "true"},
                         json={"payload": payload, "filter": flt})

    async def delete_where(self, name: str, flt: dict) -> None:
        await self._call("POST", f"/collections/{name}/points/delete", params={"wait": "true"}, json={"filter": flt})

    async def count(self, name: str, flt: dict) -> int:
        body = {"filter": flt, "exact": True}
        return int((await self._call("POST", f"/collections/{name}/points/count", json=body))["result"]["count"])

    async def scan(self, name: str, fields: list[str], page: int = 1000):
        """Every point's id and the given payload fields, page by page (for reconciliation)."""
        offset = None
        while True:
            body: dict[str, Any] = {"limit": page, "with_payload": {"include": fields}, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = (await self._call("POST", f"/collections/{name}/points/scroll", json=body))["result"]
            for pt in r["points"]:
                yield pt
            offset = r.get("next_page_offset")
            if offset is None:
                return

    async def query(self, name: str, body: dict) -> list[dict]:
        return (await self._call("POST", f"/collections/{name}/points/query", json=body))["result"]["points"]

    async def scroll(self, name: str, flt: dict, limit: int) -> list[dict]:
        body = {"filter": flt, "limit": limit, "with_payload": True, "with_vector": False}
        return (await self._call("POST", f"/collections/{name}/points/scroll", json=body))["result"]["points"]


# ------------------------------------------------------------------ graph (FalkorDB)


def cypher_literal(v: Any) -> str:
    """A Python value as a Cypher literal, for FalkorDB's `CYPHER name=value` parameter header."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise StoreError("graph parameters can't be NaN or infinite")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # double-quoted with backslash escapes, which Cypher accepts
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(cypher_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if not isinstance(k, str) or not k.replace("_", "a").isalnum():
                raise StoreError(f"graph map keys must be identifiers, got {k!r}")
            parts.append(f"{k}: {cypher_literal(x)}")
        return "{" + ", ".join(parts) + "}"
    raise StoreError(f"unsupported graph parameter type {type(v).__name__}")


def _decode(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, list):
        return [_decode(x) for x in v]
    return v


def _value(v: Any) -> Any:
    """FalkorDB's verbose result values: nodes and edges come back as [[key, value], ...] pairs."""
    v = _decode(v)
    if isinstance(v, list) and v and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in v):
        d = {k: x for k, x in v}
        if "properties" in d and ("labels" in d or "type" in d):
            out = {"id": d.get("id"), "properties": {k: _value(x) for k, x in d["properties"]}}
            if "labels" in d:
                out["labels"] = d["labels"]
            else:
                out.update(type=d["type"], src=d.get("src_node"), dst=d.get("dest_node"))
            return out
    if isinstance(v, list):
        return [_value(x) for x in v]
    return v


class GraphStore:
    def __init__(self, host: str, port: int, password: str):
        self.r = aioredis.Redis(host=host, port=port, password=password, socket_timeout=60, socket_connect_timeout=5)

    async def close(self) -> None:
        await self.r.aclose()

    async def list(self, tenant: str) -> list[str]:
        names = await self._cmd("GRAPH.LIST")
        return sorted(n for g in names if (n := parse_engine_name(_decode(g), tenant)))

    async def _cmd(self, *args):
        try:
            return await self.r.execute_command(*args)
        except aioredis.ResponseError as e:
            msg = str(e)
            if "empty key" in msg.lower() or "invalid graph operation on empty key" in msg.lower():
                raise NotFound("graph not found") from e
            raise StoreError(msg) from e
        except (aioredis.ConnectionError, aioredis.TimeoutError) as e:
            raise Unavailable(f"FalkorDB: {type(e).__name__}") from e

    async def query(self, name: str, query: str, params: dict | None, *, read_only: bool, timeout_s: float) -> dict:
        header = ""
        if params:
            for k in params:
                if not isinstance(k, str) or not k.replace("_", "a").isalnum() or k[0].isdigit():
                    raise StoreError(f"invalid parameter name {k!r}")
            header = "CYPHER " + " ".join(f"{k}={cypher_literal(v)}" for k, v in params.items()) + " "
        res = await self._cmd("GRAPH.RO_QUERY" if read_only else "GRAPH.QUERY", name, header + query,
                              "--timeout", int(timeout_s * 1000))
        if len(res) == 1:  # write-only query: just statistics
            return {"columns": [], "rows": [], "stats": [_decode(s) for s in res[0]]}
        columns = [_decode(c) for c in res[0]]
        return {"columns": columns, "rows": [[_value(v) for v in row] for row in res[1]],
                "stats": [_decode(s) for s in res[2]]}

    async def drop(self, name: str) -> bool:
        try:
            await self._cmd("GRAPH.DELETE", name)
            return True
        except NotFound:
            return False
        except StoreError as e:
            if "no such" in str(e).lower() or "empty key" in str(e).lower():
                return False
            raise

    async def exists(self, name: str) -> bool:
        return bool(await self.r.exists(name))
