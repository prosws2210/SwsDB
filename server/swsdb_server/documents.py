"""Documents: the catalog (Postgres), uploads and deletes, and hybrid retrieval.

Postgres is the source of truth. Uploading or deleting a document changes the catalog and records an outbox entry in
one transaction; `sync.Sync` applies the entry to the vector and graph stores (idempotently, with retries), so a
change accepted here is never lost and never half-applied for long. Queries are checked against the catalog, so they
only return the version the catalog says is current and never a document being deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import observe
from .embed import Embedder
from .extract import Extractor
from .names import ID_NAMESPACE, engine_name
from .parse import UnsupportedDocument, kind_of
from .settings import Settings
from .stores import Conflict, GraphStore, NotFound, SqlStore, StoreError, VectorStore

log = logging.getLogger("swsdb.documents")

PUBLIC = "*"  # a reader entry meaning "everyone in the tenant"
GRAPH_WEIGHTS = {"next": 0.30, "entity": 0.55, "relation": 0.40}  # expanded chunk score = seed score x weight
GRAPH_INDEXES = ("CREATE INDEX FOR (d:Document) ON (d.id)", "CREATE INDEX FOR (c:Chunk) ON (c.id)",
                 "CREATE INDEX FOR (e:Entity) ON (e.key)")


def doc_uuid(tenant: str, collection: str, external_id: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"doc/{tenant}/{collection}/{external_id}"))


def chunk_uuid(doc_id: str, version: int, ordinal: int) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"chunk/{doc_id}/{version}/{ordinal}"))


def check_readers(readers: list[str] | None) -> list[str]:
    if readers is None:
        return [PUBLIC]
    if not isinstance(readers, list) or not readers or len(readers) > 1000:
        raise StoreError("readers must be a non-empty list (use [\"*\"] for everyone in the tenant)")
    out = []
    for r in readers:
        if not isinstance(r, str) or not 1 <= len(r) <= 200 or r != r.strip():
            raise StoreError(f"invalid reader {r!r}: use strings like \"user:42\" or \"group:support\"")
        out.append(r)
    return sorted(set(out))


def doc_public(row: dict) -> dict:
    out = {
        "id": row["external_id"], "title": row["title"], "filename": row["filename"], "status": row["status"],
        "version": row["version"], "live_version": row.get("live_version"), "chunks": row["chunks"],
        "entities": row["entities"], "error": row["error"], "attempts": row.get("attempts", 0),
        "readers": row["readers"], "metadata": row["metadata"], "bytes": row["bytes"],
        "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
    }
    return out


class Documents:
    def __init__(self, s: Settings, sql: SqlStore, vector: VectorStore, graph: GraphStore | None,
                 embedder: Embedder, extractor: Extractor | None):
        self.s, self.sql, self.vector, self.graph = s, sql, vector, graph
        self.embedder, self.extractor = embedder, extractor
        from .sync import Sync
        self.sync = Sync(self)

    # ------------------------------------------------------------------ catalog helpers

    async def rows(self, sql: str, params: Any = None) -> list[dict]:
        await self.sql.migrate()
        async with self.sql.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def one(self, sql: str, params: Any = None) -> dict | None:
        rows = await self.rows(sql, params)
        return rows[0] if rows else None

    def file(self, tenant: str, collection: str, doc_id: str, version: int, kind: str) -> Path:
        return self.s.files_dir / tenant / collection / doc_id / f"v{version}{kind}"

    async def collection(self, tenant: str, name: str) -> dict:
        row = await self.one("SELECT * FROM swsdb.collections WHERE tenant = %s AND name = %s", [tenant, name])
        if row is None:
            raise NotFound(f"collection {name!r} is not a document collection (create it with \"documents\": true)")
        return row

    async def is_document_collection(self, tenant: str, name: str) -> bool:
        return bool(await self.one("SELECT 1 FROM swsdb.collections WHERE tenant = %s AND name = %s",
                                   [tenant, name]))

    async def ensure_graph_indexes(self, key: str) -> None:
        for idx in GRAPH_INDEXES:
            try:
                await self.graph.query(key, idx, None, read_only=False, timeout_s=30)
            except StoreError as e:
                if "already indexed" not in str(e).lower():
                    raise

    async def create_collection(self, tenant: str, name: str, graph: bool) -> dict:
        if await self.one("SELECT 1 FROM swsdb.collections WHERE tenant = %s AND name = %s", [tenant, name]):
            raise Conflict(f"collection {name!r} already exists")
        dim = await self.embedder.dim()
        key = engine_name(tenant, name)
        await self.vector.create_documents(key, dim)
        with_graph = bool(graph and self.graph)
        if with_graph:
            await self.ensure_graph_indexes(key)
        await self.rows("INSERT INTO swsdb.collections (tenant, name, embedding, dim, graph) VALUES (%s,%s,%s,%s,%s)",
                        [tenant, name, self.embedder.name, dim, with_graph])
        return {"embedding": self.embedder.name, "dim": dim, "graph": with_graph}

    async def drop_collection(self, tenant: str, name: str) -> bool:
        rows = await self.rows("DELETE FROM swsdb.collections WHERE tenant = %s AND name = %s RETURNING name",
                               [tenant, name])
        shutil.rmtree(self.s.files_dir / tenant / name, ignore_errors=True)
        return bool(rows)

    # ------------------------------------------------------------------ uploads, deletes, listing

    async def submit(self, tenant: str, collection: str, external_id: str, data: bytes, filename: str,
                     title: str | None, metadata: dict | None, readers: list[str] | None) -> dict:
        coll = await self.collection(tenant, collection)
        if not isinstance(external_id, str) or not 1 <= len(external_id) <= 512:
            raise StoreError("document id must be 1-512 characters")
        if len(data) > self.s.max_upload_mb * 1024 * 1024:
            raise StoreError(f"document is larger than SWSDB_MAX_UPLOAD_MB ({self.s.max_upload_mb} MB)")
        try:
            kind = kind_of(filename)
        except UnsupportedDocument as e:
            raise StoreError(str(e)) from None
        readers = check_readers(readers)
        metadata = metadata or {}
        if not isinstance(metadata, dict) or len(json.dumps(metadata)) > 64_000:
            raise StoreError("metadata must be a JSON object under 64 KB")
        doc_id = doc_uuid(tenant, collection, external_id)
        sha = hashlib.sha256(data).hexdigest()
        same = await self.one(
            "SELECT * FROM swsdb.documents WHERE id = %s AND sha256 = %s AND readers = %s AND metadata = %s "
            "AND title IS NOT DISTINCT FROM %s AND status IN ('ready', 'queued', 'processing')",
            [doc_id, sha, readers, Jsonb(metadata), title])
        if same:  # identical re-upload: nothing to do
            return {**doc_public(same), "unchanged": True}
        await self.sql.migrate()
        # catalog row, file and outbox entry commit together: once this returns, the change can't be lost
        async with self.sql.pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("""
                INSERT INTO swsdb.documents (id, tenant, collection, external_id, title, filename, kind, metadata,
                                             readers, version, status, bytes, sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'queued', %s, %s)
                ON CONFLICT (tenant, collection, external_id) DO UPDATE SET
                  title = EXCLUDED.title, filename = EXCLUDED.filename, kind = EXCLUDED.kind,
                  metadata = EXCLUDED.metadata, readers = EXCLUDED.readers, bytes = EXCLUDED.bytes,
                  sha256 = EXCLUDED.sha256, version = swsdb.documents.version + 1, status = 'queued', error = NULL,
                  attempts = 0, updated_at = now()
                RETURNING *""", [doc_id, tenant, collection, external_id, title, filename, kind, Jsonb(metadata),
                                 readers, len(data), sha])
            row = await cur.fetchone()
            path = self.file(tenant, collection, doc_id, row["version"], kind)
            await asyncio.to_thread(_write_file, path, data)
            await cur.execute("INSERT INTO swsdb.outbox (document_id, op, version) VALUES (%s, 'index', %s)",
                              [doc_id, row["version"]])
        self.sync.wake()
        log.info("queued %s/%s/%s v%d (%d bytes, embedding %s)", tenant, collection, external_id, row["version"],
                 len(data), coll["embedding"])
        return {**doc_public(row), "unchanged": False}

    async def delete(self, tenant: str, collection: str, external_id: str) -> dict:
        await self.collection(tenant, collection)
        doc_id = doc_uuid(tenant, collection, external_id)
        await self.sql.migrate()
        async with self.sql.pool.connection() as conn, conn.transaction():
            cur = await conn.execute("UPDATE swsdb.documents SET status = 'deleting', updated_at = now() "
                                     "WHERE id = %s RETURNING version", [doc_id])
            row = await cur.fetchone()
            if row is None:
                raise NotFound(f"document {external_id!r} not found in {collection!r}")
            await conn.execute("INSERT INTO swsdb.outbox (document_id, op, version) VALUES (%s, 'delete', %s)",
                               [doc_id, row[0]])
        self.sync.wake()
        # from here on queries no longer return it (they're checked against the catalog), even before the engines
        # have removed it
        return {"deleted": external_id, "status": "deleting"}

    async def list(self, tenant: str, collection: str, limit: int, offset: int, status: str | None) -> dict:
        await self.collection(tenant, collection)
        where, params = "tenant = %s AND collection = %s", [tenant, collection]
        if status:
            where += " AND status = %s"
            params.append(status)
        rows = await self.rows(f"SELECT * FROM swsdb.documents WHERE {where} ORDER BY created_at, external_id "  # noqa: S608
                               "LIMIT %s OFFSET %s", [*params, limit, offset])
        total = (await self.one(f"SELECT count(*) AS n FROM swsdb.documents WHERE {where}", params))["n"]  # noqa: S608
        return {"documents": [doc_public(r) for r in rows], "total": total}

    async def get(self, tenant: str, collection: str, external_id: str) -> dict:
        row = await self.one("SELECT * FROM swsdb.documents WHERE id = %s",
                             [doc_uuid(tenant, collection, external_id)])
        if row is None:
            raise NotFound(f"document {external_id!r} not found in {collection!r}")
        return doc_public(row)

    # ------------------------------------------------------------------ retrieval

    def filter(self, principals: list[str] | None, role: str, where: dict | None) -> dict:
        must: list[dict] = [{"key": "live", "match": {"value": True}}]
        if principals is not None:
            must.append({"key": "readers", "match": {"any": sorted({*principals, PUBLIC})}})
        elif role != "admin":  # a read-only key without principals sees only what is public in the tenant
            must.append({"key": "readers", "match": {"any": [PUBLIC]}})
        for k, v in (where or {}).items():
            if not isinstance(k, str) or not k or k.startswith("_"):
                raise StoreError(f"invalid filter field {k!r}")
            field = "doc" if k == "document" else f"meta.{k}"
            if isinstance(v, list):
                must.append({"key": field, "match": {"any": v}})
            elif isinstance(v, (str, int, bool)):
                must.append({"key": field, "match": {"value": v}})
            else:
                raise StoreError(f"filter on {k!r}: use a string, integer, boolean or a list of them")
        return {"must": must}

    _filter = filter  # kept for callers and tests from before the rename

    async def query(self, tenant: str, collection: str, text: str, *, k: int, mode: str, where: dict | None,
                    principals: list[str] | None, role: str, graph_hops: int | None, graph_k: int = 5) -> dict:
        """The top `k` search hits, then up to `graph_k` chunks connected to them in the graph (marked via=graph).
        Every hit is checked against the catalog: only the document's live version, never one being deleted."""
        t_start = time.perf_counter()
        coll = await self.collection(tenant, collection)
        key = engine_name(tenant, collection)
        flt = self.filter(principals, role, where)
        fetch = k * 2  # head-room for hits the catalog check drops
        pre = max(fetch * 2, 20)
        dense_q = (await self.embedder.dense([text], query=True))[0] if mode in ("hybrid", "dense") else None
        sparse_q = (await self.embedder.sparse([text], query=True))[0] if mode in ("hybrid", "sparse") else None
        sp = {"indices": sparse_q.indices, "values": sparse_q.values} if sparse_q else None
        if mode == "sparse" and not sp["indices"]:
            return {"hits": [], "graph": {"used": False}}
        if mode == "hybrid":
            body = {"prefetch": [{"query": dense_q, "using": "dense", "limit": pre, "filter": flt},
                                 {"query": sp, "using": "sparse", "limit": pre, "filter": flt}],
                    "query": {"fusion": "rrf"}, "limit": fetch, "with_payload": True}
        else:
            body = {"query": dense_q if mode == "dense" else sp, "using": mode, "limit": fetch, "filter": flt,
                    "with_payload": True}
        hits = (await self.current([self._hit(p, p["score"], mode) for p in await self.vector.query(key, body)]))[:k]

        hops = (1 if coll["graph"] else 0) if graph_hops is None else graph_hops
        graph_info: dict[str, Any] = {"used": False}
        if hops and coll["graph"] and self.graph and hits:
            seen = {h["chunk_id"] for h in hits}
            added = await self.current([h for h in await self._expand(key, hits, hops, flt)
                                        if h["chunk_id"] not in seen])
            added = sorted(added, key=lambda h: h["score"], reverse=True)[:graph_k]
            graph_info = {"used": True, "hops": hops, "added": len(added)}
            hits += added
        for h in hits:
            h.pop("_doc", None)
            h.pop("_version", None)
        took = time.perf_counter() - t_start
        observe.QUERY_SECONDS.labels(mode, "yes" if graph_info["used"] else "no").observe(took)
        observe.QUERY_HITS.observe(len(hits))
        return {"hits": hits, "graph": graph_info, "took_ms": round(took * 1000, 1)}

    async def current(self, hits: list[dict]) -> list[dict]:
        """Keep hits from each document's live version only, and none from documents being deleted."""
        ids = sorted({h["_doc"] for h in hits if h.get("_doc")})
        if not ids:
            return []
        rows = await self.rows("SELECT id::text AS id, live_version, status FROM swsdb.documents "
                               "WHERE id = ANY(%s::uuid[])", [ids])
        live = {r["id"]: r["live_version"] for r in rows if r["status"] != "deleting"}
        return [h for h in hits if h.get("_doc") in live and h.get("_version") == live[h["_doc"]]]

    @staticmethod
    def _hit(p: dict, score: float, via: str, path: list[str] | None = None) -> dict:
        pl = p.get("payload") or {}
        return {"chunk_id": str(p["id"]), "score": round(score, 6), "via": via, "text": pl.get("text"),
                "document": pl.get("doc"), "title": pl.get("title"), "page": pl.get("page"),
                "heading": pl.get("heading"), "chunk": pl.get("chunk"), "metadata": pl.get("meta") or {},
                "_doc": pl.get("document_id"), "_version": pl.get("version"),
                **({"graph_path": path} if path else {})}

    async def _expand(self, key: str, hits: list[dict], hops: int, flt: dict) -> list[dict]:
        """Chunks connected to the top hits: the passage before/after, chunks mentioning the same entities and (2
        hops) chunks about related entities. Every candidate passes the same permission filter as the search."""
        g = self.graph
        seeds = {h["chunk_id"]: h["score"] for h in hits}
        ids = list(seeds)
        cand: dict[str, tuple[float, list[str]]] = {}

        def offer(src: str, dst: str, kind: str, path: list[str], paths: int = 1) -> None:
            if dst in seeds:
                return
            score = seeds[src] * GRAPH_WEIGHTS[kind] * (1 + 0.1 * (paths - 1))
            if dst not in cand or score > cand[dst][0]:
                cand[dst] = (score, path)

        async def q(cypher: str) -> list:
            return (await g.query(key, cypher, {"ids": ids}, read_only=True, timeout_s=30))["rows"]

        for src, dst in await q("MATCH (c:Chunk)-[:NEXT]-(n:Chunk) WHERE c.id IN $ids RETURN c.id, n.id"):
            offer(src, dst, "next", ["adjacent passage"])
        # one row per shared entity (FalkorDB's verbose replies turn lists into text, so aggregate here)
        shared: dict[tuple[str, str], list[str]] = {}
        for src, dst, name in await q("MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(n:Chunk) "
                                      "WHERE c.id IN $ids AND n.id <> c.id RETURN DISTINCT c.id, n.id, e.name "
                                      "LIMIT 2000"):
            shared.setdefault((src, dst), []).append(name)
        for (src, dst), names in shared.items():
            offer(src, dst, "entity", [f"mentions {', '.join(sorted(names)[:3])}"], len(names))
        if hops >= 2:
            for src, dst, a, typ, b in await q("MATCH (c:Chunk)-[:MENTIONS]->(a:Entity)-[rel:RELATED]-(b:Entity)"
                                               "<-[:MENTIONS]-(n:Chunk) WHERE c.id IN $ids AND n.id <> c.id "
                                               "RETURN c.id, n.id, a.name, rel.type, b.name LIMIT 500"):
                offer(src, dst, "relation", [f"{a} {typ} {b}"])
        if not cand:
            return []
        top = sorted(cand.items(), key=lambda kv: kv[1][0], reverse=True)[:100]
        allowed = await self.vector.scroll(key, {"must": [*flt["must"], {"has_id": [c for c, _ in top]}]},
                                           limit=len(top))
        return [self._hit(p, cand[str(p["id"])][0], "graph", cand[str(p["id"])][1]) for p in allowed]


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
