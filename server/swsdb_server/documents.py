"""Documents: catalog (Postgres), ingestion (parse -> chunk -> embed -> vectors + graph) and hybrid retrieval.

Postgres is the source of truth for documents, chunks and extracted entities; the vector and graph stores are derived
from it. A new version of a document is written invisibly (`live: false`) and switched on in one step once all of it
is stored, so searches never see half a document or two versions at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .embed import Embedder
from .extract import Extraction, ExtractionError, Extractor
from .names import ID_NAMESPACE, engine_name
from .parse import UnsupportedDocument, chunk, kind_of, parse
from .settings import Settings
from .stores import Conflict, GraphStore, NotFound, SqlStore, StoreError, VectorStore

log = logging.getLogger("swsdb.documents")

PUBLIC = "*"  # a reader entry meaning "everyone in the tenant"
GRAPH_WEIGHTS = {"next": 0.30, "entity": 0.55, "relation": 0.40}  # expanded chunk score = seed score x weight


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
    return {
        "id": row["external_id"], "title": row["title"], "filename": row["filename"], "status": row["status"],
        "version": row["version"], "chunks": row["chunks"], "entities": row["entities"], "error": row["error"],
        "readers": row["readers"], "metadata": row["metadata"], "bytes": row["bytes"],
        "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
    }


class Documents:
    def __init__(self, s: Settings, sql: SqlStore, vector: VectorStore, graph: GraphStore | None,
                 embedder: Embedder, extractor: Extractor | None):
        self.s, self.sql, self.vector, self.graph = s, sql, vector, graph
        self.embedder, self.extractor = embedder, extractor
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []

    # ------------------------------------------------------------------ catalog helpers

    async def _rows(self, sql: str, params: Any = None) -> list[dict]:
        await self.sql.migrate()
        async with self.sql.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def _one(self, sql: str, params: Any = None) -> dict | None:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    async def collection(self, tenant: str, name: str) -> dict:
        row = await self._one("SELECT * FROM swsdb.collections WHERE tenant = %s AND name = %s", [tenant, name])
        if row is None:
            raise NotFound(f"collection {name!r} is not a document collection (create it with \"documents\": true)")
        return row

    async def create_collection(self, tenant: str, name: str, graph: bool) -> dict:
        if await self._one("SELECT 1 FROM swsdb.collections WHERE tenant = %s AND name = %s", [tenant, name]):
            raise Conflict(f"collection {name!r} already exists")
        dim = await self.embedder.dim()
        key = engine_name(tenant, name)
        await self.vector.create_documents(key, dim)
        if graph and self.graph:
            for idx in ("CREATE INDEX FOR (d:Document) ON (d.id)", "CREATE INDEX FOR (c:Chunk) ON (c.id)",
                        "CREATE INDEX FOR (e:Entity) ON (e.key)"):
                try:
                    await self.graph.query(key, idx, None, read_only=False, timeout_s=30)
                except StoreError as e:
                    if "already indexed" not in str(e).lower():
                        raise
        await self._rows("INSERT INTO swsdb.collections (tenant, name, embedding, dim, graph) VALUES (%s,%s,%s,%s,%s)",
                         [tenant, name, self.embedder.name, dim, bool(graph and self.graph)])
        return {"embedding": self.embedder.name, "dim": dim, "graph": bool(graph and self.graph)}

    async def drop_collection(self, tenant: str, name: str) -> bool:
        rows = await self._rows("DELETE FROM swsdb.collections WHERE tenant = %s AND name = %s RETURNING name",
                                [tenant, name])
        shutil.rmtree(self.s.files_dir / tenant / name, ignore_errors=True)
        return bool(rows)

    async def is_document_collection(self, tenant: str, name: str) -> bool:
        return bool(await self._one("SELECT 1 FROM swsdb.collections WHERE tenant = %s AND name = %s",
                                    [tenant, name]))

    # ------------------------------------------------------------------ submit / list / delete

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
        same = await self._one(
            "SELECT * FROM swsdb.documents WHERE id = %s AND sha256 = %s AND readers = %s AND metadata = %s "
            "AND title IS NOT DISTINCT FROM %s AND status IN ('ready', 'queued', 'processing')",
            [doc_id, sha, readers, Jsonb(metadata), title])
        if same:  # identical re-upload: nothing to do
            return {**doc_public(same), "unchanged": True}
        row = await self._one("""
            INSERT INTO swsdb.documents (id, tenant, collection, external_id, title, filename, kind, metadata,
                                         readers, version, status, bytes, sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'queued', %s, %s)
            ON CONFLICT (tenant, collection, external_id) DO UPDATE SET
              title = EXCLUDED.title, filename = EXCLUDED.filename, kind = EXCLUDED.kind,
              metadata = EXCLUDED.metadata, readers = EXCLUDED.readers, bytes = EXCLUDED.bytes,
              sha256 = EXCLUDED.sha256, version = swsdb.documents.version + 1, status = 'queued', error = NULL,
              updated_at = now()
            RETURNING *""", [doc_id, tenant, collection, external_id, title, filename, kind, Jsonb(metadata),
                             readers, len(data), sha])
        path = self._file(tenant, collection, doc_id, row["version"], kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        await self.queue.put(doc_id)
        log.info("queued %s/%s/%s v%d (%d bytes, embedding %s)", tenant, collection, external_id, row["version"],
                 len(data), coll["embedding"])
        return {**doc_public(row), "unchanged": False}

    def _file(self, tenant: str, collection: str, doc_id: str, version: int, kind: str) -> Path:
        return self.s.files_dir / tenant / collection / doc_id / f"v{version}{kind}"

    async def list(self, tenant: str, collection: str, limit: int, offset: int, status: str | None) -> dict:
        await self.collection(tenant, collection)
        where, params = "tenant = %s AND collection = %s", [tenant, collection]
        if status:
            where += " AND status = %s"
            params.append(status)
        rows = await self._rows(f"SELECT * FROM swsdb.documents WHERE {where} ORDER BY created_at, external_id "  # noqa: S608
                                "LIMIT %s OFFSET %s", [*params, limit, offset])
        total = (await self._one(f"SELECT count(*) AS n FROM swsdb.documents WHERE {where}", params))["n"]  # noqa: S608
        return {"documents": [doc_public(r) for r in rows], "total": total}

    async def get(self, tenant: str, collection: str, external_id: str) -> dict:
        row = await self._one("SELECT * FROM swsdb.documents WHERE id = %s",
                              [doc_uuid(tenant, collection, external_id)])
        if row is None:
            raise NotFound(f"document {external_id!r} not found in {collection!r}")
        return doc_public(row)

    async def delete(self, tenant: str, collection: str, external_id: str) -> dict:
        coll = await self.collection(tenant, collection)
        doc_id = doc_uuid(tenant, collection, external_id)
        if not await self._one("SELECT 1 FROM swsdb.documents WHERE id = %s", [doc_id]):
            raise NotFound(f"document {external_id!r} not found in {collection!r}")
        key = engine_name(tenant, collection)
        # derived stores first: if one fails, the catalog row stays and the delete can be retried
        await self.vector.delete_where(key, {"must": [{"key": "document_id", "match": {"value": doc_id}}]})
        if coll["graph"] and self.graph:
            await self._graph_remove_document(key, doc_id)
        await self._rows("DELETE FROM swsdb.documents WHERE id = %s", [doc_id])
        shutil.rmtree(self.s.files_dir / tenant / collection / doc_id, ignore_errors=True)
        return {"deleted": external_id}

    # ------------------------------------------------------------------ the worker

    async def start(self) -> None:
        await self.sql.migrate()
        pending = await self._rows("SELECT id FROM swsdb.documents WHERE status IN ('queued', 'processing') "
                                   "ORDER BY updated_at")
        for r in pending:  # a restart interrupted these
            await self.queue.put(str(r["id"]))
        if pending:
            log.info("resuming %d unfinished documents", len(pending))
        self.workers = [asyncio.create_task(self._work(i), name=f"swsdb-ingest-{i}")
                        for i in range(self.s.ingest_workers)]

    async def stop(self) -> None:
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def _work(self, n: int) -> None:
        while True:
            doc_id = await self.queue.get()
            try:
                await self.process(doc_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("document %s failed", doc_id)
                await self._fail(doc_id, f"{type(e).__name__}: {e}")
            finally:
                self.queue.task_done()

    async def _fail(self, doc_id: str, message: str) -> None:
        await self._rows("UPDATE swsdb.documents SET status = 'failed', error = %s, updated_at = now() "
                         "WHERE id = %s AND status IN ('queued', 'processing')", [message[:2000], doc_id])

    async def process(self, doc_id: str) -> None:
        row = await self._one("UPDATE swsdb.documents SET status = 'processing', updated_at = now() "
                              "WHERE id = %s AND status IN ('queued', 'processing') RETURNING *", [doc_id])
        if row is None:
            return  # deleted, or already done by another run
        tenant, collection, version = row["tenant"], row["collection"], row["version"]
        coll = await self.collection(tenant, collection)
        key = engine_name(tenant, collection)
        if coll["embedding"] != self.embedder.name:
            raise StoreError(f"collection {collection!r} was created with embeddings {coll['embedding']}, but this "
                             f"server uses {self.embedder.name}; set SWSDB_EMBEDDINGS / SWSDB_EMBEDDINGS_MODEL back")

        data = await asyncio.to_thread(self._file(tenant, collection, doc_id, version, row["kind"]).read_bytes)
        try:
            sections = await asyncio.to_thread(parse, data, row["filename"] or f"x{row['kind']}")
        except UnsupportedDocument as e:
            await self._fail(doc_id, str(e))
            return
        chunks = chunk(sections)
        if not chunks:
            await self._fail(doc_id, "the document has no text")
            return
        ids = [chunk_uuid(doc_id, version, c.ordinal) for c in chunks]

        # 1. the chunks, in the source of truth
        async with self.sql.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM swsdb.chunks WHERE document_id = %s AND version = %s", [doc_id, version])
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO swsdb.chunks (id, document_id, version, ordinal, text, page, heading, hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(i, doc_id, version, c.ordinal, c.text, c.page, c.heading, c.hash) for i, c in zip(ids, chunks,
                                                                                                    strict=True)])

        # 2. vectors: dense + sparse, written invisible
        texts = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks]
        dense = await self.embedder.dense(texts)
        sparse = await self.embedder.sparse(texts)
        base = {"document_id": doc_id, "doc": row["external_id"], "title": row["title"], "version": version,
                "readers": row["readers"], "meta": row["metadata"], "live": False}
        await self.vector.upsert_chunks(key, [
            {"id": i, "vector": {"dense": d, "sparse": {"indices": sp.indices, "values": sp.values}},
             "payload": {**base, "chunk": c.ordinal, "text": c.text, "page": c.page, "heading": c.heading}}
            for i, c, d, sp in zip(ids, chunks, dense, sparse, strict=True)])

        # 3. entities (optional) and the graph
        extracted: list[Extraction] = []
        if self.extractor and coll["graph"]:
            try:
                extracted = list(await asyncio.gather(*(self.extractor.extract(c.text) for c in chunks)))
            except ExtractionError as e:
                await self._fail(doc_id, str(e))
                return
            async with self.sql.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO swsdb.chunk_entities (chunk_id, entity_key, name, type) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    [(i, e["key"], e["name"], e["type"]) for i, ex in zip(ids, extracted, strict=True)
                     for e in ex.entities])
                await cur.executemany(
                    "INSERT INTO swsdb.chunk_relations (chunk_id, source, target, type) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    [(i, r["source"], r["target"], r["type"]) for i, ex in zip(ids, extracted, strict=True)
                     for r in ex.relations])
        if coll["graph"] and self.graph:
            await self._graph_write(key, doc_id, row, ids, chunks, extracted)

        # 4. switch on: unless a newer version arrived meanwhile (then that run finishes the job)
        current = await self._one("SELECT version, status FROM swsdb.documents WHERE id = %s", [doc_id])
        if current is None or current["version"] != version:
            log.info("document %s v%d superseded while processing", doc_id, version)
            return
        doc_filter = {"key": "document_id", "match": {"value": doc_id}}
        await self.vector.delete_where(key, {"must": [doc_filter], "must_not": [{"key": "version",
                                                                                 "match": {"value": version}}]})
        await self.vector.set_payload(key, {"live": True}, {"must": [doc_filter, {"key": "version",
                                                                                  "match": {"value": version}}]})
        n_entities = len({e["key"] for ex in extracted for e in ex.entities})
        done = await self._rows("""
            WITH d AS (UPDATE swsdb.documents SET status = 'ready', error = NULL, chunks = %s, entities = %s,
                         updated_at = now() WHERE id = %s AND version = %s RETURNING id)
            DELETE FROM swsdb.chunks WHERE document_id IN (SELECT id FROM d) AND version <> %s RETURNING id""",
                                [len(chunks), n_entities, doc_id, version, version])
        for old in self._file(tenant, collection, doc_id, version, row["kind"]).parent.glob("v*"):
            if old.name != f"v{version}{row['kind']}":
                old.unlink(missing_ok=True)
        log.info("ready %s/%s/%s v%d: %d chunks, %d entities (%d old chunks removed)", tenant, collection,
                 row["external_id"], version, len(chunks), n_entities, len(done))

    # ------------------------------------------------------------------ graph

    async def _g(self, key: str, q: str, params: dict | None = None, read_only: bool = False) -> dict:
        return await self.graph.query(key, q, params, read_only=read_only, timeout_s=120)

    async def _graph_remove_document(self, key: str, doc_id: str) -> None:
        if not await self.graph.exists(key):
            return
        await self._g(key, "MATCH (d:Document {id: $doc}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
                           "DETACH DELETE c, d", {"doc": doc_id})
        await self._g(key, "MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e")

    async def _graph_write(self, key: str, doc_id: str, row: dict, ids: list[str], chunks, extracted) -> None:
        await self._g(key, "MATCH (d:Document {id: $doc})-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c", {"doc": doc_id})
        await self._g(key, "MERGE (d:Document {id: $doc}) SET d.external_id = $ext, d.title = $title",
                      {"doc": doc_id, "ext": row["external_id"], "title": row["title"] or row["external_id"]})
        rows = [{"id": i, "ordinal": c.ordinal, "page": c.page if c.page is not None else -1}
                for i, c in zip(ids, chunks, strict=True)]
        for b in range(0, len(rows), 500):
            await self._g(key, "MATCH (d:Document {id: $doc}) UNWIND $rows AS r "
                               "CREATE (d)-[:HAS_CHUNK]->(:Chunk {id: r.id, ordinal: r.ordinal, page: r.page})",
                          {"doc": doc_id, "rows": rows[b:b + 500]})
        pairs = [[ids[i], ids[i + 1]] for i in range(len(ids) - 1)]
        for b in range(0, len(pairs), 500):
            await self._g(key, "UNWIND $pairs AS p MATCH (a:Chunk {id: p[0]}), (b:Chunk {id: p[1]}) "
                               "CREATE (a)-[:NEXT]->(b)", {"pairs": pairs[b:b + 500]})
        if not extracted:
            await self._g(key, "MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e")
            return
        ents = {e["key"]: e for ex in extracted for e in ex.entities}
        if ents:
            await self._g(key, "UNWIND $ents AS e MERGE (n:Entity {key: e.key}) "
                               "ON CREATE SET n.name = e.name, n.type = e.type", {"ents": list(ents.values())})
        mentions = [{"chunk": i, "key": e["key"]} for i, ex in zip(ids, extracted, strict=True) for e in ex.entities]
        for b in range(0, len(mentions), 500):
            await self._g(key, "UNWIND $m AS x MATCH (c:Chunk {id: x.chunk}), (n:Entity {key: x.key}) "
                               "CREATE (c)-[:MENTIONS]->(n)", {"m": mentions[b:b + 500]})
        rels = list({(r["source"], r["target"], r["type"]): r for ex in extracted for r in ex.relations}.values())
        for b in range(0, len(rels), 500):
            await self._g(key, "UNWIND $r AS x MATCH (a:Entity {key: x.source}), (b:Entity {key: x.target}) "
                               "MERGE (a)-[:RELATED {type: x.type}]->(b)", {"r": rels[b:b + 500]})
        await self._g(key, "MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e")

    # ------------------------------------------------------------------ retrieval

    def _filter(self, principals: list[str] | None, role: str, where: dict | None) -> dict:
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

    async def query(self, tenant: str, collection: str, text: str, *, k: int, mode: str, where: dict | None,
                    principals: list[str] | None, role: str, graph_hops: int | None, graph_k: int = 5) -> dict:
        """The top `k` search hits, then up to `graph_k` chunks connected to them in the graph (marked via=graph)."""
        coll = await self.collection(tenant, collection)
        key = engine_name(tenant, collection)
        flt = self._filter(principals, role, where)
        pre = max(k * 4, 20)
        dense_q = (await self.embedder.dense([text], query=True))[0] if mode in ("hybrid", "dense") else None
        sparse_q = (await self.embedder.sparse([text], query=True))[0] if mode in ("hybrid", "sparse") else None
        sp = {"indices": sparse_q.indices, "values": sparse_q.values} if sparse_q else None
        if mode == "hybrid":
            body = {"prefetch": [{"query": dense_q, "using": "dense", "limit": pre, "filter": flt},
                                 {"query": sp, "using": "sparse", "limit": pre, "filter": flt}],
                    "query": {"fusion": "rrf"}, "limit": k, "with_payload": True}
        else:
            body = {"query": dense_q if mode == "dense" else sp, "using": mode, "limit": k, "filter": flt,
                    "with_payload": True}
        if mode == "sparse" and not sp["indices"]:
            return {"hits": [], "graph": {"used": False}}
        points = await self.vector.query(key, body)
        hits = [self._hit(p, p["score"], mode) for p in points]

        hops = (1 if coll["graph"] else 0) if graph_hops is None else graph_hops
        graph_info: dict[str, Any] = {"used": False}
        if hops and coll["graph"] and self.graph and hits:
            seen = {h["chunk_id"] for h in hits}
            added = sorted((h for h in await self._expand(key, hits, hops, flt) if h["chunk_id"] not in seen),
                           key=lambda h: h["score"], reverse=True)[:graph_k]
            graph_info = {"used": True, "hops": hops, "added": len(added)}
            hits += added
        return {"hits": hits, "graph": graph_info}

    @staticmethod
    def _hit(p: dict, score: float, via: str, path: list[str] | None = None) -> dict:
        pl = p.get("payload") or {}
        return {"chunk_id": str(p["id"]), "score": round(score, 6), "via": via, "text": pl.get("text"),
                "document": pl.get("doc"), "title": pl.get("title"), "page": pl.get("page"),
                "heading": pl.get("heading"), "chunk": pl.get("chunk"), "metadata": pl.get("meta") or {},
                **({"graph_path": path} if path else {})}

    async def _expand(self, key: str, hits: list[dict], hops: int, flt: dict) -> list[dict]:
        """Chunks connected to the top hits: the passage before/after, chunks mentioning the same entities and (2
        hops) chunks about related entities. Every candidate passes the same permission filter as the search."""
        seeds = {h["chunk_id"]: h["score"] for h in hits}
        ids = list(seeds)
        cand: dict[str, tuple[float, list[str]]] = {}

        def offer(src: str, dst: str, kind: str, path: list[str], paths: int = 1) -> None:
            if dst in seeds:
                return
            score = seeds[src] * GRAPH_WEIGHTS[kind] * (1 + 0.1 * (paths - 1))
            if dst not in cand or score > cand[dst][0]:
                cand[dst] = (score, path)

        r = await self._g(key, "MATCH (c:Chunk)-[:NEXT]-(n:Chunk) WHERE c.id IN $ids RETURN c.id, n.id",
                          {"ids": ids}, read_only=True)
        for src, dst in r["rows"]:
            offer(src, dst, "next", ["adjacent passage"])
        # one row per shared entity (FalkorDB's verbose replies turn lists into text, so aggregate here)
        r = await self._g(key, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(n:Chunk) WHERE c.id IN $ids "
                               "AND n.id <> c.id RETURN DISTINCT c.id, n.id, e.name LIMIT 2000", {"ids": ids},
                          read_only=True)
        shared: dict[tuple[str, str], list[str]] = {}
        for src, dst, name in r["rows"]:
            shared.setdefault((src, dst), []).append(name)
        for (src, dst), names in shared.items():
            offer(src, dst, "entity", [f"mentions {', '.join(sorted(names)[:3])}"], len(names))
        if hops >= 2:
            r = await self._g(key, "MATCH (c:Chunk)-[:MENTIONS]->(a:Entity)-[rel:RELATED]-(b:Entity)"
                                   "<-[:MENTIONS]-(n:Chunk) WHERE c.id IN $ids AND n.id <> c.id "
                                   "RETURN c.id, n.id, a.name, rel.type, b.name LIMIT 500", {"ids": ids},
                              read_only=True)
            for src, dst, a, typ, b in r["rows"]:
                offer(src, dst, "relation", [f"{a} {typ} {b}"])
        if not cand:
            return []
        top = sorted(cand.items(), key=lambda kv: kv[1][0], reverse=True)[:100]
        allowed = await self.vector.scroll(key, {"must": [*flt["must"], {"has_id": [c for c, _ in top]}]},
                                           limit=len(top))
        return [self._hit(p, cand[str(p["id"])][0], "graph", cand[str(p["id"])][1]) for p in allowed]
