"""The lite profile (SWSDB_PROFILE=lite): vectors and graphs inside PostgreSQL, with pgvector and Apache AGE.

The two stores here have the same interface as the Qdrant and FalkorDB adapters, so the rest of swsdb doesn't know
which profile runs. Postgres is then the only engine: one process to run, back up and size.

- Vectors: one table per collection in the `swsdb_lite` schema (swsdb's own, no tenant role can read it). Dense
  vectors use pgvector with an HNSW index; BM25 keywords use an inverted index (term, point, weight) scored with the
  same IDF formula Qdrant applies. Hybrid search fuses the two rankings with reciprocal rank fusion.
- Graphs: one AGE graph per collection, owned by the tenant's sandbox role. Every Cypher query runs as that role, so
  a query can only reach its own tenant's graphs, and Postgres permissions (not query parsing) keep tenants apart.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from . import timing
from .names import parse_engine_name, point_id
from .stores import (
    DISTANCES,
    Conflict,
    NotFound,
    SqlStore,
    StoreError,
    Unavailable,
    VectorSpec,
    _qdrant_filter,
    cypher_literal,
    tenant_role,
)

SCHEMA = "swsdb_lite"
MAX_DIM = 16000  # pgvector's limit for a stored vector
MAX_INDEXED_DIM = 2000  # and for an HNSW index (larger vectors are searched exactly)
RRF_K = 2  # reciprocal rank fusion: 1 / (k + rank), as Qdrant computes it

SETUP = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC;
CREATE TABLE IF NOT EXISTS {SCHEMA}.collections (
    name text PRIMARY KEY, tbl text NOT NULL UNIQUE, dim int NOT NULL, distance text NOT NULL,
    documents bool NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS {SCHEMA}.graphs (
    name text PRIMARY KEY, graph text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now());
"""
GRAPH_SETUP = "CREATE EXTENSION IF NOT EXISTS age"
# AGE identifies a graph by its schema's OID. pg_restore recreates the schemas with new OIDs, so after a restore the
# catalog points at OIDs that no longer exist: re-point it (through temporary ids, so the unique indexes never clash).
# A no-op when nothing was restored.
GRAPH_REPAIR = """
DO $repair$
BEGIN
  IF EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid) THEN
    PERFORM set_config('session_replication_role', 'replica', true);  -- the label -> graph foreign key
    CREATE TEMP TABLE swsdb_age_repair ON COMMIT DROP AS
      SELECT graphid AS old, namespace::oid AS new, (4000000000 - row_number() OVER ())::bigint::text::oid AS tmp
      FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid;
    UPDATE ag_catalog.ag_label l SET graph = r.tmp FROM swsdb_age_repair r WHERE l.graph = r.old;
    UPDATE ag_catalog.ag_graph g SET graphid = r.tmp FROM swsdb_age_repair r WHERE g.graphid = r.old;
    UPDATE ag_catalog.ag_label l SET graph = r.new FROM swsdb_age_repair r WHERE l.graph = r.tmp;
    UPDATE ag_catalog.ag_graph g SET graphid = r.new FROM swsdb_age_repair r WHERE g.graphid = r.tmp;
    PERFORM set_config('session_replication_role', 'origin', true);
  END IF;
END
$repair$;
"""

# distance -> (pgvector operator, index operator class, score as SQL over the operator's result `d`)
OPS = {
    "cosine": ("<=>", "vector_cosine_ops", "1 - d"),
    "dot": ("<#>", "vector_ip_ops", "-d"),
    "euclid": ("<->", "vector_l2_ops", "d"),
    "manhattan": ("<+>", "vector_l1_ops", "d"),
}


def _sql_error(e: psycopg.Error, engine: str) -> Exception:
    if isinstance(e, psycopg.errors.QueryCanceled):
        return StoreError("statement timed out")
    if isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)) and \
            not (isinstance(e, psycopg.errors.DatabaseError) and e.sqlstate and not e.sqlstate.startswith("08")):
        return Unavailable(f"{engine}: {e}")
    return StoreError(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".strip())


def _vec(v: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _id_out(pid: str) -> str | int:
    return int(pid) if pid.isdigit() else pid


# ------------------------------------------------------------------ Qdrant-style filters as SQL

def _contains(path: list[str], value: Any) -> str:
    nested: Any = value
    for k in reversed(path):
        nested = {k: nested}
    return json.dumps(nested)


def filter_sql(flt: dict | None, params: list) -> str:
    """A Qdrant filter (`must`, `must_not`, `should`; `match` on a payload field or `has_id`) as a condition on
    `p.payload` / `p.id`. A field holding a list matches when any element does, as in Qdrant."""
    if not flt:
        return "true"
    parts = [_cond(c, params) for c in flt.get("must") or []]
    parts += [f"NOT ({_cond(c, params)})" for c in flt.get("must_not") or []]
    if flt.get("should"):
        parts.append("(" + " OR ".join(_cond(c, params) for c in flt["should"]) + ")")
    unknown = set(flt) - {"must", "must_not", "should"}
    if unknown:
        raise StoreError(f"unsupported filter clause {sorted(unknown)}")
    return " AND ".join(parts) or "true"


def _cond(c: dict, params: list) -> str:
    if "has_id" in c:
        params.append([str(point_id(i)) for i in c["has_id"]])
        return "p.id = ANY(%s::text[])"
    if any(k in c for k in ("must", "must_not", "should")):
        return f"({filter_sql(c, params)})"
    key, m = c.get("key"), c.get("match") or {}
    if not isinstance(key, str) or not key:
        raise StoreError(f"unsupported filter condition {c!r}")
    path = key.split(".")
    if "value" in m:
        values = [m["value"]]
    elif "any" in m:
        values = list(m["any"])
        if not values:
            return "false"
    else:
        raise StoreError(f"unsupported filter condition {c!r}")
    # {"readers": "x"} matches a scalar field; {"readers": ["x"]} a list holding it. Both use the GIN index.
    params.append([_contains(path, v) for v in values] + [_contains(path, [v]) for v in values])
    return "p.payload @> ANY(%s::jsonb[])"


# ------------------------------------------------------------------ vectors (pgvector)

class PgVectorStore:
    """Vector collections as tables. Point ids, payloads, filters and scores behave as with Qdrant."""

    in_postgres = True
    engine = "pgvector"

    def __init__(self, sql: SqlStore):
        self.sql = sql
        sql.add_setup(SETUP)

    async def close(self) -> None:
        pass

    @timing.timed("pgvector")
    async def _rows(self, statement: str | pgsql.Composable, params: Any = None) -> list[dict]:
        await self.sql.migrate()
        try:
            async with self.sql.pool.connection(timeout=10) as conn:
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(statement, params)
                return await cur.fetchall() if cur.description else []
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available") from e
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e

    async def _coll(self, name: str) -> dict:
        rows = await self._rows("SELECT * FROM swsdb_lite.collections WHERE name = %s", [name])
        if not rows:
            raise NotFound(f"Collection `{name}` doesn't exist!")
        return rows[0]

    @staticmethod
    def _tables(coll: dict) -> tuple[pgsql.Composable, pgsql.Composable]:
        return pgsql.Identifier(SCHEMA, coll["tbl"]), pgsql.Identifier(SCHEMA, coll["tbl"] + "_t")

    async def all_names(self) -> list[str]:
        return [r["name"] for r in await self._rows("SELECT name FROM swsdb_lite.collections ORDER BY name")]

    async def list(self, tenant: str) -> list[str]:
        return sorted(n for c in await self.all_names() if (n := parse_engine_name(c, tenant)))

    async def _create(self, name: str, dim: int, distance: str, documents: bool) -> None:
        if not 1 <= dim <= MAX_DIM:
            raise StoreError(f"vector size must be between 1 and {MAX_DIM} on the lite profile")
        tbl = "c_" + hashlib.sha256(name.encode()).hexdigest()[:24]
        opclass = OPS[distance][1]
        t, terms = pgsql.Identifier(SCHEMA, tbl), pgsql.Identifier(SCHEMA, tbl + "_t")
        await self.sql.migrate()
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"swsdb_lite:{name}"])
                if await (await conn.execute("SELECT 1 FROM swsdb_lite.collections WHERE name = %s",
                                             [name])).fetchone():
                    raise Conflict(f"Collection `{name}` already exists!")
                await conn.execute(pgsql.SQL("DROP TABLE IF EXISTS {}, {}").format(terms, t))
                await conn.execute(pgsql.SQL("CREATE TABLE {} (id text PRIMARY KEY, vec vector({}), "
                                             "payload jsonb NOT NULL DEFAULT '{{}}')").format(t, pgsql.Literal(dim)))
                await conn.execute(pgsql.SQL("CREATE INDEX ON {} USING gin (payload jsonb_path_ops)").format(t))
                if dim <= MAX_INDEXED_DIM:
                    await conn.execute(pgsql.SQL("CREATE INDEX ON {} USING hnsw (vec {})").format(
                        t, pgsql.SQL(opclass)))
                if documents:
                    await conn.execute(pgsql.SQL(
                        "CREATE TABLE {} (term int NOT NULL, id text NOT NULL REFERENCES {} (id) ON DELETE CASCADE, "
                        "weight real NOT NULL, PRIMARY KEY (term, id))").format(terms, t))
                    await conn.execute(pgsql.SQL("CREATE INDEX ON {} (id)").format(terms))
                await conn.execute("INSERT INTO swsdb_lite.collections (name, tbl, dim, distance, documents) "
                                   "VALUES (%s, %s, %s, %s, %s)", [name, tbl, dim, distance, documents])
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e

    async def create(self, name: str, spec: VectorSpec) -> None:
        if spec.distance not in DISTANCES:
            raise StoreError(f"distance must be one of {', '.join(DISTANCES)}")
        await self._create(name, spec.size, spec.distance, documents=False)

    async def create_documents(self, name: str, dim: int) -> None:
        await self._create(name, dim, "cosine", documents=True)

    async def is_documents(self, name: str) -> bool:
        return bool((await self._coll(name))["documents"])

    async def info(self, name: str) -> dict:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        n = (await self._rows(pgsql.SQL("SELECT count(*) AS n FROM {}").format(t)))[0]["n"]
        out = {"size": coll["dim"], "distance": coll["distance"], "points": n, "status": "green"}
        if coll["documents"]:
            out["sparse"] = ["sparse"]
        return out

    async def drop(self, name: str) -> bool:
        try:
            coll = await self._coll(name)
        except NotFound:
            return False
        t, terms = self._tables(coll)
        await self._rows(pgsql.SQL("DROP TABLE IF EXISTS {}, {}").format(terms, t))
        await self._rows("DELETE FROM swsdb_lite.collections WHERE name = %s", [name])
        return True

    # -- plain vector collections (the /points API)

    async def upsert(self, name: str, points: list[dict]) -> int:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        rows = []
        for p in points:
            payload = dict(p.get("payload") or {})
            if any(k.startswith("_") for k in payload):
                raise StoreError("payload keys starting with '_' are reserved for swsdb")
            payload["_id"] = p["id"]
            rows.append((str(point_id(p["id"])), _vec(p["vector"]), json.dumps(payload)))
        await self._many(pgsql.SQL("INSERT INTO {} (id, vec, payload) VALUES (%s, %s::vector, %s::jsonb) "
                                   "ON CONFLICT (id) DO UPDATE SET vec = EXCLUDED.vec, payload = EXCLUDED.payload"
                                   ).format(t), rows)
        return len(rows)

    async def _many(self, statement: pgsql.Composable, rows: list[tuple], before: list | None = None) -> None:
        await self.sql.migrate()
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction(), conn.cursor() as cur:
                for stmt, params in before or []:
                    await cur.execute(stmt, params)
                if rows:
                    await cur.executemany(statement, rows)
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available") from e
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e

    async def search(self, name: str, vector: list[float], limit: int, where: dict | None,
                     with_payload: bool = True) -> list[dict]:
        hits = await self.query(name, {"query": vector, "limit": limit, "filter": _qdrant_filter(where)})
        out = []
        for h in hits:
            payload = dict(h.get("payload") or {})
            original = payload.pop("_id", h["id"])
            out.append({"id": original, "score": h["score"], **({"payload": payload} if with_payload else {})})
        return out

    async def get(self, name: str, ids: list[str | int]) -> list[dict]:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        rows = await self._rows(pgsql.SQL("SELECT id, vec::text AS vec, payload FROM {} WHERE id = ANY(%s::text[])"
                                          ).format(t), [[str(point_id(i)) for i in ids]])
        out = []
        for r in rows:
            payload = dict(r["payload"])
            out.append({"id": payload.pop("_id", _id_out(r["id"])),
                        "vector": json.loads(r["vec"]) if r["vec"] else None, "payload": payload})
        return out

    async def delete(self, name: str, ids: list[str | int]) -> None:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        await self._rows(pgsql.SQL("DELETE FROM {} WHERE id = ANY(%s::text[])").format(t),
                         [[str(point_id(i)) for i in ids]])

    # -- document collections: dense vectors plus BM25 terms

    async def upsert_chunks(self, name: str, points: list[dict]) -> None:
        coll = await self._coll(name)
        t, terms = self._tables(coll)
        ids = [str(p["id"]) for p in points]
        main = [(str(p["id"]), _vec(p["vector"]["dense"]), json.dumps(p["payload"])) for p in points]
        term_rows = [(int(i), str(p["id"]), float(w)) for p in points
                     for i, w in zip(p["vector"]["sparse"]["indices"], p["vector"]["sparse"]["values"], strict=True)]
        await self._many(
            pgsql.SQL("INSERT INTO {} (id, vec, payload) VALUES (%s, %s::vector, %s::jsonb) ON CONFLICT (id) "
                      "DO UPDATE SET vec = EXCLUDED.vec, payload = EXCLUDED.payload").format(t), main,
            before=[(pgsql.SQL("DELETE FROM {} WHERE id = ANY(%s::text[])").format(terms), [ids])])
        await self._many(pgsql.SQL("INSERT INTO {} (term, id, weight) VALUES (%s, %s, %s) ON CONFLICT (term, id) "
                                   "DO UPDATE SET weight = EXCLUDED.weight").format(terms), term_rows)

    async def set_payload(self, name: str, payload: dict, flt: dict) -> None:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        params: list = [json.dumps(payload)]
        where = filter_sql(flt, params)
        await self._rows(pgsql.SQL("UPDATE {} p SET payload = p.payload || %s::jsonb WHERE ").format(t)
                         + pgsql.SQL(where), params)

    async def delete_where(self, name: str, flt: dict) -> None:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        params: list = []
        where = filter_sql(flt, params)
        await self._rows(pgsql.SQL("DELETE FROM {} p WHERE ").format(t) + pgsql.SQL(where), params)

    async def count(self, name: str, flt: dict) -> int:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        params: list = []
        where = filter_sql(flt, params)
        rows = await self._rows(pgsql.SQL("SELECT count(*) AS n FROM {} p WHERE ").format(t) + pgsql.SQL(where),
                                params)
        return int(rows[0]["n"])

    async def scan(self, name: str, fields: list[str], page: int = 1000):
        """Every point's id and the given payload fields, page by page (for reconciliation)."""
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        after = ""
        while True:
            rows = await self._rows(pgsql.SQL(
                "SELECT id, (SELECT coalesce(jsonb_object_agg(k, payload -> k), '{{}}') FROM unnest(%s::text[]) k "
                "WHERE payload ? k) AS payload FROM {} WHERE id > %s ORDER BY id LIMIT %s").format(t),
                [fields, after, page])
            for r in rows:
                yield {"id": _id_out(r["id"]), "payload": r["payload"]}
            if len(rows) < page:
                return
            after = rows[-1]["id"]

    async def browse(self, name: str, limit: int, offset: str | int | None, where: dict | None,
                     with_vector: bool = False) -> dict:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        params: list = [str(offset) if offset is not None else ""]
        cond = filter_sql(_qdrant_filter(where), params)
        vec = pgsql.SQL(", p.vec::text AS vec" if with_vector else "")
        rows = await self._rows(pgsql.SQL("SELECT p.id, p.payload{} FROM {} p WHERE p.id > %s AND ").format(vec, t)
                                + pgsql.SQL(cond) + pgsql.SQL(" ORDER BY p.id LIMIT %s"), [*params, limit + 1])
        out = []
        for r in rows[:limit]:
            payload = dict(r["payload"])
            p = {"id": payload.pop("_id", _id_out(r["id"])), "point_id": _id_out(r["id"]), "payload": payload}
            if with_vector and r.get("vec"):
                p["vector"] = json.loads(r["vec"])
            out.append(p)
        return {"points": out, "next": rows[limit - 1]["id"] if len(rows) > limit else None}

    async def scroll(self, name: str, flt: dict, limit: int) -> list[dict]:
        coll = await self._coll(name)
        t, _ = self._tables(coll)
        params: list = []
        where = filter_sql(flt, params)
        rows = await self._rows(pgsql.SQL("SELECT p.id, p.payload FROM {} p WHERE ").format(t) + pgsql.SQL(where)
                                + pgsql.SQL(" ORDER BY p.id LIMIT %s"), [*params, limit])
        return [{"id": _id_out(r["id"]), "payload": r["payload"]} for r in rows]

    async def query(self, name: str, body: dict) -> list[dict]:
        """The subset of Qdrant's query API swsdb uses: one dense or sparse query, or several fused with RRF."""
        coll = await self._coll(name)
        limit = int(body.get("limit") or 10)
        if "prefetch" in body:
            if (body.get("query") or {}).get("fusion") != "rrf":
                raise StoreError("only RRF fusion is supported")
            fused: dict[str, float] = {}
            payloads: dict[str, dict] = {}
            for sub in body["prefetch"]:
                for rank, h in enumerate(await self._one(coll, sub)):
                    fused[h["id"]] = fused.get(h["id"], 0.0) + 1.0 / (RRF_K + rank)
                    payloads[h["id"]] = h["payload"]
            top = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
            return [{"id": _id_out(pid), "score": score, "payload": payloads[pid]} for pid, score in top]
        hits = await self._one(coll, {**body, "limit": limit})
        return [{**h, "id": _id_out(h["id"])} for h in hits]

    async def _one(self, coll: dict, q: dict) -> list[dict]:
        t, terms = self._tables(coll)
        limit = int(q.get("limit") or 10)
        params: list = []
        where = filter_sql(q.get("filter"), params)
        query = q.get("query")
        if isinstance(query, dict) and "indices" in query:
            if not coll["documents"]:
                raise StoreError("this collection has no sparse vectors")
            if not query["indices"]:
                return []
            # BM25 with Qdrant's IDF modifier: idf = ln(1 + (N - n + 0.5) / (n + 0.5)), N points, n holding the term
            stmt = pgsql.SQL(
                "WITH q AS (SELECT * FROM unnest(%s::int[], %s::real[]) AS q(term, qw)), "
                "n AS (SELECT count(*)::float8 AS n FROM {t}), "
                "df AS (SELECT term, count(*)::float8 AS df FROM {terms} JOIN q USING (term) GROUP BY term), "
                "s AS (SELECT tt.id, sum(q.qw * tt.weight * ln(1 + (n.n - df.df + 0.5) / (df.df + 0.5))) AS score "
                "      FROM q JOIN df USING (term) JOIN {terms} tt USING (term) CROSS JOIN n GROUP BY tt.id) "
                "SELECT p.id, p.payload, s.score FROM s JOIN {t} p ON p.id = s.id WHERE ").format(t=t, terms=terms)
            rows = await self._rows(stmt + pgsql.SQL(where) + pgsql.SQL(" ORDER BY s.score DESC, p.id LIMIT %s"),
                                    [[int(i) for i in query["indices"]], [float(v) for v in query["values"]],
                                     *params, limit])
            return [{"id": r["id"], "score": float(r["score"]), "payload": r["payload"]} for r in rows]
        if not isinstance(query, list):
            raise StoreError("unsupported query")
        op, _, score = OPS[coll["distance"]]
        v = _vec(query)
        stmt = pgsql.SQL("SELECT p.id, p.payload, ") + pgsql.SQL(score.replace("d", f"(p.vec {op} %s::vector)")) + \
            pgsql.SQL(" AS score FROM {} p WHERE ").format(t) + pgsql.SQL(where) + \
            pgsql.SQL(f" ORDER BY p.vec {op} %s::vector LIMIT %s")
        await self.sql.migrate()
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction():
                # filtered HNSW scans keep going until enough rows pass the filter (pgvector >= 0.8)
                await conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true), "
                                   "set_config('hnsw.ef_search', %s, true)", [str(min(max(limit * 2, 40), 1000))])
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(stmt, [v, *params, v, limit])
                rows = await cur.fetchall()
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available") from e
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e
        rows.sort(key=lambda r: -float(r["score"]) if coll["distance"] in ("cosine", "dot") else float(r["score"]))
        return [{"id": r["id"], "score": float(r["score"]), "payload": r["payload"]} for r in rows]


# ------------------------------------------------------------------ graph results: agtype as JSON

_SUFFIX = re.compile(r"::(vertex|edge|path|numeric|float|integer)")


class _Agtype:
    """AGE's text output: JSON with `::vertex`, `::edge`, `::path` (and numeric) annotations after values."""

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def parse(self) -> Any:
        v = self.value()
        self.ws()
        if self.i != len(self.s):
            raise ValueError(f"unexpected {self.s[self.i:self.i + 20]!r}")
        return v

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        c = self.s[self.i]
        if c == "{":
            self.i += 1
            out: dict = {}
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1
            else:
                while True:
                    self.ws()
                    k = self.string()
                    self.ws()
                    assert self.s[self.i] == ":"
                    self.i += 1
                    out[k] = self.value()
                    self.ws()
                    if self.s[self.i] == ",":
                        self.i += 1
                        continue
                    assert self.s[self.i] == "}"
                    self.i += 1
                    break
            v: Any = out
        elif c == "[":
            self.i += 1
            items: list = []
            self.ws()
            if self.s[self.i] == "]":
                self.i += 1
            else:
                while True:
                    items.append(self.value())
                    self.ws()
                    if self.s[self.i] == ",":
                        self.i += 1
                        continue
                    assert self.s[self.i] == "]"
                    self.i += 1
                    break
            v = items
        elif c == '"':
            v = self.string()
        else:
            m = re.compile(r"-?Infinity|NaN|true|false|null|-?\d+(\.\d+)?([eE][+-]?\d+)?").match(self.s, self.i)
            if not m:
                raise ValueError(f"unexpected {self.s[self.i:self.i + 20]!r}")
            self.i = m.end()
            tok = m.group(0)
            v = {"true": True, "false": False, "null": None}.get(tok, tok)
            if isinstance(v, str):
                v = float(tok) if any(ch in tok for ch in ".eEIN") else int(tok)
        m = _SUFFIX.match(self.s, self.i)
        if m:
            self.i = m.end()
            v = _shape(m.group(1), v)
        return v

    def string(self) -> str:
        assert self.s[self.i] == '"'
        j = self.i + 1
        while self.s[j] != '"':
            j += 2 if self.s[j] == "\\" else 1
        out = json.loads(self.s[self.i:j + 1])
        self.i = j + 1
        return out


def _shape(kind: str, v: Any) -> Any:
    """AGE's graph values in the shape the FalkorDB adapter returns them."""
    if kind == "vertex":
        return {"id": v["id"], "labels": [v["label"]], "properties": v.get("properties") or {}}
    if kind == "edge":
        return {"id": v["id"], "type": v["label"], "src": v["start_id"], "dst": v["end_id"],
                "properties": v.get("properties") or {}}
    if kind == "path":
        return {"nodes": v[0::2], "edges": v[1::2]}
    return v


def agtype(text: str | None) -> Any:
    return None if text is None else _Agtype(text).parse()


# ------------------------------------------------------------------ which columns a Cypher query returns

_KEYWORD = re.compile(r"\b(RETURN|ORDER\s+BY|SKIP|LIMIT|UNION|DISTINCT)\b", re.IGNORECASE)


def _mask(query: str) -> str:
    """The query with comments and the inside of string literals blanked, same length: positions still line up."""
    out, i, n = list(query), 0, len(query)
    while i < n:
        if query.startswith("//", i):
            j = query.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = " " * (j - i)
            i = j
        elif query[i] in "'\"`":
            q, j = query[i], i + 1
            while j < n and query[j] != q:
                j += 2 if query[j] == "\\" and q != "`" else 1
            out[i + 1:min(j, n)] = "x" * (min(j, n) - i - 1)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def return_columns(query: str) -> list[str]:
    """The names of the columns the query's last top-level RETURN produces ([] when it returns nothing). AGE needs
    them declared up front; a wrong count only makes Postgres refuse the query, it can't change what runs."""
    code = _mask(query)
    depth, top = 0, []  # positions at nesting depth 0
    for i, ch in enumerate(code):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            top.append(i)
    top_set = set(top)
    returns = [m for m in re.finditer(r"\bRETURN\b", code, re.IGNORECASE) if m.start() in top_set]
    if not returns:
        return []
    start = returns[-1].end()
    m = re.compile(r"\s*DISTINCT\b", re.IGNORECASE).match(code, start)
    if m:
        start = m.end()
    end = len(code)
    for k in _KEYWORD.finditer(code, start):
        if k.start() in top_set and k.group(1).upper().split()[0] in ("ORDER", "SKIP", "LIMIT", "UNION"):
            end = k.start()
            break
    if ";" in code[start:end]:
        end = start + code[start:end].index(";")
    items, depth, last = [], 0, start
    for i in range(start, end):
        ch = code[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append((last, i))
            last = i + 1
    items.append((last, end))
    names = []
    for a, b in items:
        text, masked = query[a:b].strip(), code[a:b]
        if text == "*":
            raise StoreError("RETURN * isn't supported by the lite profile's graph engine: name the columns")
        alias = None
        for am in re.finditer(r"\bAS\s+(`[^`]+`|\w+)\s*$", masked, re.IGNORECASE):
            alias = query[a:b][am.start(1):am.end(1)].strip("`")
        names.append(alias or text)
    return names


# ------------------------------------------------------------------ graphs (Apache AGE)

def _quote(s: str, q: str) -> str:
    return q + s.replace(q, q + q) + q


def _tenant_of(name: str) -> str:
    return name[3:].split("__", 1)[0]


def _graph_name(name: str) -> str:
    """AGE graph names are schema names: at most 63 bytes."""
    return name if len(name) <= 63 else f"{name[:50]}_{hashlib.sha256(name.encode()).hexdigest()[:12]}"


class AgeGraphStore:
    """One AGE graph per collection, owned by the tenant's sandbox role; queries run as that role."""

    in_postgres = True
    engine = "age"
    pass_through = True

    def __init__(self, sql: SqlStore):
        self.sql = sql
        sql.add_setup(SETUP)
        sql.add_setup(GRAPH_SETUP)
        sql.add_setup(GRAPH_REPAIR)
        self._known: dict[str, str] = {}

    async def close(self) -> None:
        pass

    def forget(self) -> None:
        self._known.clear()

    async def _graph(self, name: str) -> str | None:
        if name in self._known:
            return self._known[name]
        rows = await self.sql.rows("SELECT graph FROM swsdb_lite.graphs WHERE name = %s", [name])
        if rows:
            self._known[name] = rows[0]["graph"]
            return rows[0]["graph"]
        return None

    async def _make(self, name: str) -> str:
        """The graph, created on first write (as FalkorDB does) and handed to the tenant's role."""
        role = await self.sql.tenant_role(_tenant_of(name))
        graph = _graph_name(name)
        await self.sql.migrate()
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"swsdb_lite:{name}"])
                row = await (await conn.execute("SELECT graph FROM swsdb_lite.graphs WHERE name = %s",
                                                [name])).fetchone()
                if row:
                    return row[0]
                await conn.execute("SELECT set_config('search_path', 'ag_catalog, public', true)")
                if await (await conn.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
                                             [graph])).fetchone():
                    await conn.execute("SELECT ag_catalog.drop_graph(%s, true)", [graph])  # left by a failed drop
                await conn.execute("SELECT ag_catalog.create_graph(%s)", [graph])
                await self._hand_over(conn, graph, role)
                await conn.execute("INSERT INTO swsdb_lite.graphs (name, graph) VALUES (%s, %s)", [name, graph])
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e
        self._known[name] = graph
        return graph

    @staticmethod
    async def _hand_over(conn, graph: str, role: str) -> None:
        r, g = pgsql.Identifier(role), pgsql.Identifier(graph)
        await conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA ag_catalog TO {}").format(r))
        await conn.execute(pgsql.SQL("ALTER SCHEMA {} OWNER TO {}").format(g, r))
        for (t,) in await (await conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind = 'r' AND pg_get_userbyid(c.relowner) <> %s",
                [graph, role])).fetchall():
            await conn.execute(pgsql.SQL("ALTER TABLE {} OWNER TO {}").format(pgsql.Identifier(graph, t), r))
        await conn.execute(pgsql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(g, r))

    async def all_names(self) -> list[str]:
        return [r["name"] for r in await self.sql.rows("SELECT name FROM swsdb_lite.graphs ORDER BY name")]

    async def list(self, tenant: str) -> list[str]:
        return sorted(n for g in await self.all_names() if (n := parse_engine_name(g, tenant)))

    async def exists(self, name: str) -> bool:
        return await self._graph(name) is not None

    async def drop(self, name: str) -> bool:
        graph = await self._graph(name)
        if graph is None:
            return False
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction():
                await conn.execute("SELECT set_config('search_path', 'ag_catalog, public', true)")
                await conn.execute("SELECT ag_catalog.drop_graph(%s, true)", [graph])
                await conn.execute("DELETE FROM swsdb_lite.graphs WHERE name = %s", [name])
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e
        self._known.pop(name, None)
        return True

    async def index(self, name: str, label: str, prop: str) -> None:
        """Property lookups on a label: AGE matches `{prop: value}` by containment, which a GIN index serves."""
        graph = await self._graph(name) or await self._make(name)
        role = tenant_role(_tenant_of(name))
        try:
            async with self.sql.pool.connection(timeout=10) as conn, conn.transaction():
                await conn.execute("SELECT set_config('search_path', 'ag_catalog, public', true)")
                if not await (await conn.execute(
                        "SELECT 1 FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
                        "WHERE g.name = %s AND l.name = %s", [graph, label])).fetchone():
                    await conn.execute("SELECT ag_catalog.create_vlabel(%s, %s)", [graph, label])
                    await self._hand_over(conn, graph, role)
                await conn.execute(pgsql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING gin (properties)").format(
                    pgsql.Identifier(f"{label.lower()}_props"), pgsql.Identifier(graph, label)))
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e
        del prop

    async def delete_orphan_entities(self, name: str) -> None:
        await self.query(name, "MATCH (e:Entity) WHERE NOT exists((e)<-[:MENTIONS]-()) DETACH DELETE e", None,
                         read_only=False, timeout_s=120)

    async def count_nodes(self, name: str) -> int:
        return (await self.query(name, "MATCH (n) RETURN count(n)", None, read_only=True, timeout_s=10))["rows"][0][0]

    @timing.timed("graph")
    async def query(self, name: str, query: str, params: dict | None, *, read_only: bool, timeout_s: float) -> dict:
        graph = await self._graph(name)
        if graph is None:
            if read_only:
                raise NotFound("graph not found")
            graph = await self._make(name)
        if params:
            for k, v in params.items():
                if not isinstance(k, str) or not k.replace("_", "a").isalnum() or k[0].isdigit():
                    raise StoreError(f"invalid parameter name {k!r}")
                cypher_literal(v)  # the same value rules as the FalkorDB adapter (identifier map keys, no NaN)
        columns = return_columns(query)
        # the query goes in dollar quotes it can't close: the tag is derived from the query (so repeated queries are
        # the same statement) and changes if the query happens to contain it
        salt = 0
        while (tag := f"swsdb_{hashlib.sha256(f'{salt}:{query}'.encode()).hexdigest()[:12]}") in query:
            salt += 1
        coldef = ", ".join(f"c{i} ag_catalog.agtype" for i in range(max(1, len(columns))))
        body = query.replace("%", "%%") if params else query  # with parameters, psycopg reads % as placeholders
        # the graph name is swsdb's own and the query sits in dollar quotes it can't close
        statement = (f"SELECT * FROM ag_catalog.cypher({_quote(graph, chr(39))}, ${tag}${body}${tag}$"  # noqa: S608
                     + (", %s" if params else "") + f") AS ({coldef})")
        tenant = _tenant_of(name)
        role = await self.sql.tenant_role(tenant)
        try:
            pool = await self.sql._pool_for(tenant)
            async with pool.connection(timeout=10) as conn, conn.transaction():
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('search_path', %s, true)",
                                   [f"{int(timeout_s * 1000)}", f"ag_catalog, {_quote(role, chr(34))}"])
                if not read_only:  # one writer per graph: MERGE and the orphan clean-up stay correct under load
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"swsdb_graph:{graph}"])
                cur = await conn.execute(statement, [json.dumps(params)] if params else None)
                rows = await cur.fetchall() if cur.description else []
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available") from e
        except psycopg.errors.ReadOnlySqlTransaction as e:
            raise StoreError("a read_only query can't change the graph") from e
        except psycopg.errors.QueryCanceled as e:
            raise StoreError(f"query timed out after {timeout_s:g}s") from e
        except psycopg.Error as e:
            raise _sql_error(e, "PostgreSQL") from e
        if not columns:
            return {"columns": [], "rows": [], "stats": []}
        return {"columns": columns, "rows": [[agtype(v) for v in r] for r in rows], "stats": []}
