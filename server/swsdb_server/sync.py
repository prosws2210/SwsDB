"""Keeping the vector and graph stores in step with the catalog.

Outbox: every accepted change is an outbox row (written with the catalog change). Workers claim rows with
`FOR UPDATE SKIP LOCKED`, one document's rows strictly in order, and apply them with idempotent steps, so a retry
after any crash just finishes the job. Errors from an engine being down are transient: retried with capped backoff
(and at once when the engine comes back). Errors in the document itself are permanent: it is marked failed.

Reconciliation: compares the catalog with what Qdrant and the graph actually hold, per collection, and queues the
work that brings them back in line (or, in dry-run, only reports it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from typing import TYPE_CHECKING

import httpx
import psycopg
import redis.exceptions
from psycopg_pool import PoolTimeout

from .embed import EmbeddingError
from .extract import ExtractionError
from .names import engine_name
from .parse import UnsupportedDocument, chunk, parse
from .stores import NotFound, StoreError, Unavailable

if TYPE_CHECKING:
    from .documents import Documents

log = logging.getLogger("swsdb.sync")

LEASE_S = 60  # a claimed row is someone's for this long; the heartbeat renews it every LEASE_S / 3 while work runs
MAX_BACKOFF_S = 120


class Permanent(Exception):
    """The document itself is the problem (unreadable, no text): retrying won't help."""


def transient(e: BaseException) -> bool:
    if isinstance(e, (Unavailable, httpx.HTTPError, psycopg.OperationalError, PoolTimeout, EmbeddingError,
                      ExtractionError, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError,
                      ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    return isinstance(e, StoreError) and "loading" in str(e).lower()  # Redis/FalkorDB still loading its data


def backoff(attempts: int) -> float:
    return min(2 ** min(attempts, 10), MAX_BACKOFF_S)


class Sync:
    def __init__(self, docs: Documents):
        self.d = docs
        self.workers: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self.reconciler: asyncio.Task | None = None
        self.last_reconcile: dict | None = None

    # ------------------------------------------------------------------ lifecycle

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        await self.d.sql.migrate()
        # rows claimed by a process that died stay locked until their lease ends; this process is the only one
        await self.d.rows("UPDATE swsdb.outbox SET locked_until = NULL WHERE locked_until IS NOT NULL")
        pending = (await self.d.one("SELECT count(*) AS n FROM swsdb.outbox"))["n"]
        if pending:
            log.info("resuming %d pending changes", pending)
        self.workers = [asyncio.create_task(self._work(i), name=f"swsdb-sync-{i}")
                        for i in range(self.d.s.ingest_workers)]
        if self.d.s.reconcile_interval_s > 0:
            self.reconciler = asyncio.create_task(self._reconcile_loop(), name="swsdb-reconcile")

    async def stop(self) -> None:
        tasks = [*self.workers, *([self.reconciler] if self.reconciler else [])]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def engine_recovered(self, cap: str) -> None:
        """An engine is back: retry what was waiting for it now, not at the end of its backoff."""
        with contextlib.suppress(Exception):
            n = await self.d.rows("UPDATE swsdb.outbox SET next_attempt_at = now() WHERE attempts > 0 "
                                  "AND (locked_until IS NULL OR locked_until < now()) RETURNING id")
            if n:
                log.info("%s is back: retrying %d waiting changes now", cap, len(n))
            self.wake()

    async def stats(self) -> dict:
        r = await self.d.one("""SELECT count(*) AS pending, count(*) FILTER (WHERE attempts > 0) AS retrying,
                                       extract(epoch FROM now() - min(created_at)) AS oldest_s
                                FROM swsdb.outbox""")
        return {"pending": r["pending"], "retrying": r["retrying"],
                "oldest_s": round(float(r["oldest_s"]), 1) if r["oldest_s"] is not None else None,
                "last_reconcile": self.last_reconcile}

    # ------------------------------------------------------------------ the worker loop

    async def _claim(self) -> dict | None:
        return await self.d.one(f"""
            WITH c AS (
              SELECT o.id FROM swsdb.outbox o
              WHERE o.next_attempt_at <= now() AND (o.locked_until IS NULL OR o.locked_until < now())
                AND NOT EXISTS (SELECT 1 FROM swsdb.outbox p WHERE p.document_id = o.document_id AND p.id < o.id)
              ORDER BY o.id LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE swsdb.outbox o SET locked_until = now() + interval '{LEASE_S} seconds', attempts = o.attempts + 1
            FROM c WHERE o.id = c.id RETURNING o.*""")  # noqa: S608 - LEASE_S is a constant

    async def _work(self, n: int) -> None:
        while True:
            try:
                row = await self._claim()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Postgres itself unavailable: wait and try again
                log.debug("worker %d can't claim: %s", n, e)
                row = None
            if row is None:
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                continue
            await self._run(row)

    async def _run(self, row: dict) -> None:
        beat = asyncio.create_task(self._heartbeat(row["id"]))
        try:
            await {"index": self.index, "delete": self.delete, "graph": self.graph_op}[row["op"]](
                str(row["document_id"]), row["version"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            beat.cancel()
            await self._failed(row, e)
            return
        finally:
            beat.cancel()
        await self._done(row["id"])

    async def _heartbeat(self, row_id: int) -> None:
        while True:
            await asyncio.sleep(LEASE_S / 3)
            with contextlib.suppress(Exception):
                await self.d.rows(f"UPDATE swsdb.outbox SET locked_until = now() + interval '{LEASE_S} seconds' "  # noqa: S608
                                  "WHERE id = %s", [row_id])

    async def _done(self, row_id: int) -> None:
        for attempt in range(30):  # Postgres may be the thing that's down; the work is done, record it
            try:
                await self.d.rows("DELETE FROM swsdb.outbox WHERE id = %s", [row_id])
                return
            except Exception:
                await asyncio.sleep(min(2 ** attempt, 10))

    async def _failed(self, row: dict, e: BaseException) -> None:
        """Record the failure. If Postgres is the thing that's down, keep trying until it's back: giving up would
        leave the row claimed until its lease ends, stalling the document for that long."""
        for attempt in range(120):
            try:
                await self._record_failure(row, e)
                return
            except Exception:
                if attempt == 0:
                    log.warning("can't record the failure of outbox row %s yet (Postgres unavailable); retrying",
                                row["id"])
                await asyncio.sleep(min(2 ** attempt, 5))
        log.error("gave up recording the failure of outbox row %s; it is retried when its lease ends", row["id"])

    async def _record_failure(self, row: dict, e: BaseException) -> None:
        msg = f"{type(e).__name__}: {e}".strip()[:2000]
        doc = str(row["document_id"])
        if transient(e) or row["op"] != "index":
            wait = backoff(row["attempts"])
            log.warning("%s %s v%d failed (attempt %d, retry in %.0fs): %s", row["op"], doc, row["version"],
                        row["attempts"], wait, msg)
            await self.d.rows("UPDATE swsdb.outbox SET locked_until = NULL, last_error = %s, "
                              "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                              [msg, wait, row["id"]])
            if row["op"] == "index":
                await self.d.rows("UPDATE swsdb.documents SET error = %s, attempts = %s, updated_at = now() "
                                  "WHERE id = %s AND version = %s",
                                  [f"retrying: {msg}", row["attempts"], doc, row["version"]])
        else:
            log.error("index %s v%d failed permanently: %s", doc, row["version"], msg)
            async with self.d.sql.pool.connection() as conn, conn.transaction():
                await conn.execute("UPDATE swsdb.documents SET status = 'failed', error = %s, updated_at = now()"
                                   " WHERE id = %s AND version = %s AND status <> 'deleting'",
                                   [msg, doc, row["version"]])
                await conn.execute("DELETE FROM swsdb.outbox WHERE id = %s", [row["id"]])

    # ------------------------------------------------------------------ operations (idempotent)

    async def index(self, doc_id: str, version: int) -> None:
        d = self.d
        doc = await d.one("SELECT * FROM swsdb.documents WHERE id = %s", [doc_id])
        if doc is None or doc["status"] == "deleting" or doc["version"] != version:
            return  # deleted or superseded: nothing to do for this version
        if doc["status"] == "queued":
            await d.rows("UPDATE swsdb.documents SET status = 'processing', updated_at = now() "
                         "WHERE id = %s AND version = %s", [doc_id, version])
        tenant, collection = doc["tenant"], doc["collection"]
        coll = await d.collection(tenant, collection)
        if coll["embedding"] != d.embedder.name:
            raise Permanent(f"collection {collection!r} was created with embeddings {coll['embedding']}, but this "
                            f"server uses {d.embedder.name}; set SWSDB_EMBEDDINGS / SWSDB_EMBEDDINGS_MODEL back")
        key = engine_name(tenant, collection)

        chunks = await self._chunks(doc, version)
        await self._vectors(key, coll, doc, chunks)
        if d.extractor and coll["graph"]:
            await self._extract(chunks)
        if coll["graph"] and d.graph:
            await self.write_graph(key, doc, version)
        await self._switch_on(key, doc, version, len(chunks))

    async def _chunks(self, doc: dict, version: int) -> list[dict]:
        """The version's chunks from the catalog; parsed from the stored file the first time."""
        d = self.d
        rows = await d.rows("SELECT * FROM swsdb.chunks WHERE document_id = %s AND version = %s ORDER BY ordinal",
                            [doc["id"], version])
        if rows:
            return rows
        path = d.file(doc["tenant"], doc["collection"], str(doc["id"]), version, doc["kind"])
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise Permanent(f"the uploaded file for version {version} is missing ({path.name})") from None
        try:
            sections = await asyncio.to_thread(parse, data, doc["filename"] or f"x{doc['kind']}")
        except UnsupportedDocument as e:
            raise Permanent(str(e)) from None
        parts = chunk(sections)
        if not parts:
            raise Permanent("the document has no text")
        from .documents import chunk_uuid
        async with d.sql.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM swsdb.chunks WHERE document_id = %s AND version = %s", [doc["id"], version])
            await cur.executemany(
                "INSERT INTO swsdb.chunks (id, document_id, version, ordinal, text, page, heading, hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(chunk_uuid(str(doc["id"]), version, c.ordinal), doc["id"], version, c.ordinal, c.text, c.page,
                  c.heading, c.hash) for c in parts])
        return await d.rows("SELECT * FROM swsdb.chunks WHERE document_id = %s AND version = %s ORDER BY ordinal",
                            [doc["id"], version])

    async def _vectors(self, key: str, coll: dict, doc: dict, chunks: list[dict]) -> None:
        """Embed and upsert, unless Qdrant already holds exactly these chunks (a retry after a later step failed)."""
        d = self.d
        doc_id, version = str(doc["id"]), chunks[0]["version"]
        version_filter = {"must": [{"key": "document_id", "match": {"value": doc_id}},
                                   {"key": "version", "match": {"value": version}}]}
        try:
            if await d.vector.count(key, version_filter) == len(chunks):
                return
        except NotFound:  # the Qdrant collection itself is gone (lost volume, manual delete): recreate it
            log.warning("vector collection %s missing: recreating", key)
            await d.vector.create_documents(key, coll["dim"])
        texts = [f"{c['heading']}\n{c['text']}" if c["heading"] else c["text"] for c in chunks]
        dense = await d.embedder.dense(texts)
        sparse = await d.embedder.sparse(texts)
        live = doc["live_version"] == version  # a repair of the version already being served stays visible
        base = {"document_id": doc_id, "doc": doc["external_id"], "title": doc["title"], "version": version,
                "readers": doc["readers"], "meta": doc["metadata"], "live": live}
        await d.vector.upsert_chunks(key, [
            {"id": str(c["id"]), "vector": {"dense": dv, "sparse": {"indices": sv.indices, "values": sv.values}},
             "payload": {**base, "chunk": c["ordinal"], "text": c["text"], "page": c["page"], "heading": c["heading"]}}
            for c, dv, sv in zip(chunks, dense, sparse, strict=True)])

    async def _extract(self, chunks: list[dict]) -> None:
        """Entities for chunks not extracted yet. Results are stored, so a retry never pays for the LLM twice."""
        d = self.d
        todo = [c for c in chunks if not c["extracted"]]
        if not todo:
            return
        results = await asyncio.gather(*(d.extractor.extract(c["text"]) for c in todo), return_exceptions=True)
        failed = None
        async with d.sql.pool.connection() as conn:
            for c, ex in zip(todo, results, strict=True):
                if isinstance(ex, BaseException):
                    failed = failed or ex
                    continue
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO swsdb.chunk_entities (chunk_id, entity_key, name, type) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", [(c["id"], e["key"], e["name"], e["type"]) for e in ex.entities])
                    await cur.executemany(
                        "INSERT INTO swsdb.chunk_relations (chunk_id, source, target, type) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", [(c["id"], r["source"], r["target"], r["type"])
                                                   for r in ex.relations])
                    await cur.execute("UPDATE swsdb.chunks SET extracted = true WHERE id = %s", [c["id"]])
        if failed is not None:
            raise failed

    async def write_graph(self, key: str, doc: dict, version: int) -> None:
        """The document's subgraph, rebuilt from the catalog (no LLM calls): document, chunks in order, the entities
        they mention and the relations between those entities."""
        d = self.d
        g = d.graph
        doc_id = str(doc["id"])

        async def q(cypher: str, params: dict | None = None) -> None:
            await g.query(key, cypher, params, read_only=False, timeout_s=120)

        if not await g.exists(key):
            await d.ensure_graph_indexes(key)
        chunks = await d.rows("SELECT id::text AS id, ordinal, page FROM swsdb.chunks WHERE document_id = %s "
                              "AND version = %s ORDER BY ordinal", [doc_id, version])
        ents = await d.rows("SELECT ce.chunk_id::text AS chunk, ce.entity_key AS key, ce.name, ce.type "
                            "FROM swsdb.chunk_entities ce JOIN swsdb.chunks c ON c.id = ce.chunk_id "
                            "WHERE c.document_id = %s AND c.version = %s", [doc_id, version])
        rels = await d.rows("SELECT DISTINCT cr.source, cr.target, cr.type FROM swsdb.chunk_relations cr "
                            "JOIN swsdb.chunks c ON c.id = cr.chunk_id WHERE c.document_id = %s AND c.version = %s",
                            [doc_id, version])
        await q("MATCH (d:Document {id: $doc})-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c", {"doc": doc_id})
        await q("MERGE (d:Document {id: $doc}) SET d.external_id = $ext, d.title = $title",
                {"doc": doc_id, "ext": doc["external_id"], "title": doc["title"] or doc["external_id"]})
        rows = [{"id": c["id"], "ordinal": c["ordinal"], "page": c["page"] if c["page"] is not None else -1}
                for c in chunks]
        for b in range(0, len(rows), 500):
            await q("MATCH (d:Document {id: $doc}) UNWIND $rows AS r "
                    "CREATE (d)-[:HAS_CHUNK]->(:Chunk {id: r.id, ordinal: r.ordinal, page: r.page})",
                    {"doc": doc_id, "rows": rows[b:b + 500]})
        pairs = [[chunks[i]["id"], chunks[i + 1]["id"]] for i in range(len(chunks) - 1)]
        for b in range(0, len(pairs), 500):
            await q("UNWIND $pairs AS p MATCH (a:Chunk {id: p[0]}), (b:Chunk {id: p[1]}) CREATE (a)-[:NEXT]->(b)",
                    {"pairs": pairs[b:b + 500]})
        unique = list({e["key"]: {"key": e["key"], "name": e["name"], "type": e["type"]} for e in ents}.values())
        for b in range(0, len(unique), 500):
            await q("UNWIND $ents AS e MERGE (n:Entity {key: e.key}) ON CREATE SET n.name = e.name, n.type = e.type",
                    {"ents": unique[b:b + 500]})
        mentions = [{"chunk": e["chunk"], "key": e["key"]} for e in ents]
        for b in range(0, len(mentions), 500):
            await q("UNWIND $m AS x MATCH (c:Chunk {id: x.chunk}), (n:Entity {key: x.key}) "
                    "CREATE (c)-[:MENTIONS]->(n)", {"m": mentions[b:b + 500]})
        for b in range(0, len(rels), 500):
            await q("UNWIND $r AS x MATCH (a:Entity {key: x.source}), (b:Entity {key: x.target}) "
                    "MERGE (a)-[:RELATED {type: x.type}]->(b)", {"r": rels[b:b + 500]})
        await q("MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e")

    async def _switch_on(self, key: str, doc: dict, version: int, n_chunks: int) -> None:
        """Make `version` the one queries return. Order matters: (1) its points visible in Qdrant, (2) the catalog
        says it's live (queries are checked against the catalog, so before this they still return the old version
        and after it only the new one), (3) the old version's points removed."""
        d = self.d
        doc_id = str(doc["id"])
        doc_f = {"key": "document_id", "match": {"value": doc_id}}
        ver_f = {"key": "version", "match": {"value": version}}
        await d.vector.set_payload(key, {"live": True}, {"must": [doc_f, ver_f]})
        n_entities = (await d.one("SELECT count(DISTINCT ce.entity_key) AS n FROM swsdb.chunk_entities ce "
                                  "JOIN swsdb.chunks c ON c.id = ce.chunk_id WHERE c.document_id = %s "
                                  "AND c.version = %s", [doc_id, version]))["n"]
        async with d.sql.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE swsdb.documents SET status = 'ready', live_version = %s, error = NULL, attempts = 0, "
                "chunks = %s, entities = %s, updated_at = now() WHERE id = %s AND version = %s "
                "AND status <> 'deleting' RETURNING id", [version, n_chunks, n_entities, doc_id, version])
            if await cur.fetchone() is None:
                return  # superseded or being deleted meanwhile: that change's own row finishes the job
            await conn.execute("DELETE FROM swsdb.chunks WHERE document_id = %s AND version <> %s", [doc_id, version])
        await d.vector.delete_where(key, {"must": [doc_f], "must_not": [ver_f]})
        folder = d.file(doc["tenant"], doc["collection"], doc_id, version, doc["kind"]).parent
        for old in folder.glob("v*"):
            if old.name != f"v{version}{doc['kind']}":
                old.unlink(missing_ok=True)
        log.info("ready %s/%s/%s v%d: %d chunks, %d entities", doc["tenant"], doc["collection"],
                 doc["external_id"], version, n_chunks, n_entities)

    async def delete(self, doc_id: str, version: int) -> None:
        d = self.d
        doc = await d.one("SELECT * FROM swsdb.documents WHERE id = %s", [doc_id])
        if doc is None or doc["status"] != "deleting":
            return  # already gone, or uploaded again after the delete
        coll = await d.collection(doc["tenant"], doc["collection"])
        key = engine_name(doc["tenant"], doc["collection"])
        with contextlib.suppress(NotFound):
            await d.vector.delete_where(key, {"must": [{"key": "document_id", "match": {"value": doc_id}}]})
        if coll["graph"] and d.graph:
            await self.remove_graph(key, doc_id)
        await asyncio.to_thread(shutil.rmtree, d.s.files_dir / doc["tenant"] / doc["collection"] / doc_id, True)
        await d.rows("DELETE FROM swsdb.documents WHERE id = %s AND status = 'deleting'", [doc_id])
        log.info("deleted %s/%s/%s", doc["tenant"], doc["collection"], doc["external_id"])

    async def remove_graph(self, key: str, doc_id: str) -> None:
        g = self.d.graph
        if not await g.exists(key):
            return
        await g.query(key, "MATCH (d:Document {id: $doc}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
                           "DETACH DELETE c, d", {"doc": doc_id}, read_only=False, timeout_s=120)
        await g.query(key, "MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e", None, read_only=False,
                      timeout_s=120)

    async def graph_op(self, doc_id: str, version: int) -> None:
        d = self.d
        doc = await d.one("SELECT * FROM swsdb.documents WHERE id = %s", [doc_id])
        if doc is None or doc["status"] != "ready" or doc["live_version"] != version:
            return
        await self.write_graph(engine_name(doc["tenant"], doc["collection"]), doc, version)

    # ------------------------------------------------------------------ reconciliation

    async def _reconcile_loop(self) -> None:
        await asyncio.sleep(min(60, self.d.s.reconcile_interval_s))
        while True:
            try:
                report = await self.reconcile(None, dry_run=False)
                if report["issues"]:
                    log.warning("reconciliation found and queued repairs: %s", report["totals"])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # an engine down: next round
                log.info("reconciliation skipped: %s", e)
            await asyncio.sleep(self.d.s.reconcile_interval_s)

    async def reconcile(self, tenant: str | None, *, dry_run: bool) -> dict:
        """Compare the catalog with the stores. Repairs are queued as outbox rows (or only reported in dry-run)."""
        d = self.d
        t0 = time.monotonic()
        if tenant:
            colls = await d.rows("SELECT * FROM swsdb.collections WHERE tenant = %s ORDER BY name", [tenant])
        else:
            colls = await d.rows("SELECT * FROM swsdb.collections ORDER BY tenant, name")
        out = []
        for coll in colls:
            out.append(await self._reconcile_collection(coll, dry_run))
        totals: dict[str, int] = {}
        for c in out:
            for k, v in c["found"].items():
                totals[k] = totals.get(k, 0) + v
        report = {"dry_run": dry_run, "collections": out, "totals": totals, "issues": sum(totals.values()),
                  "took_s": round(time.monotonic() - t0, 2), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if not dry_run:
            self.last_reconcile = {k: report[k] for k in ("issues", "totals", "took_s", "at")}
            self.wake()
        return report

    async def _reconcile_collection(self, coll: dict, dry_run: bool) -> dict:
        d = self.d
        tenant, name = coll["tenant"], coll["name"]
        key = engine_name(tenant, name)
        found = {"missing_vectors": 0, "stale_vectors": 0, "orphan_vectors": 0, "graph_mismatch": 0,
                 "orphan_graph": 0, "stuck": 0, "missing_vector_collection": 0}
        docs = {str(r["id"]): r for r in await d.rows(
            "SELECT id, status, version, live_version, chunks FROM swsdb.documents WHERE tenant = %s "
            "AND collection = %s", [tenant, name])}
        busy = {str(r["document_id"]) for r in await d.rows(
            "SELECT DISTINCT o.document_id FROM swsdb.outbox o JOIN swsdb.documents dd ON dd.id = o.document_id "
            "WHERE dd.tenant = %s AND dd.collection = %s", [tenant, name])}
        to_index: set[str] = set()
        to_graph: set[str] = set()

        # changes that were accepted but have no outbox row (shouldn't happen; repair if it does)
        for doc_id, r in docs.items():
            if doc_id not in busy and r["status"] in ("queued", "processing", "deleting"):
                found["stuck"] += 1
                if not dry_run:
                    op = "delete" if r["status"] == "deleting" else "index"
                    await d.rows("INSERT INTO swsdb.outbox (document_id, op, version) VALUES (%s, %s, %s)",
                                 [doc_id, op, r["version"]])

        # vectors
        counts: dict[tuple[str, int, bool], int] = {}
        try:
            async for p in d.vector.scan(key, ["document_id", "version", "live"]):
                pl = p.get("payload") or {}
                k = (pl.get("document_id"), pl.get("version"), bool(pl.get("live")))
                counts[k] = counts.get(k, 0) + 1
        except NotFound:
            found["missing_vector_collection"] = 1
            if not dry_run:
                await d.vector.create_documents(key, coll["dim"])
        orphans = sorted({doc for doc, _, _ in counts if doc not in docs})
        found["orphan_vectors"] = sum(n for (doc, _, _), n in counts.items() if doc not in docs)
        if orphans and not dry_run:
            await d.vector.delete_where(key, {"must": [{"key": "document_id", "match": {"any": orphans}}]})
        for doc_id, r in docs.items():
            if r["status"] != "ready" or doc_id in busy:
                continue
            live = r["live_version"]
            if counts.get((doc_id, live, True), 0) != r["chunks"] or counts.get((doc_id, live, False), 0):
                found["missing_vectors"] += 1
                to_index.add(doc_id)
            stale = sum(n for (doc, ver, _), n in counts.items() if doc == doc_id and ver != live)
            if stale:
                found["stale_vectors"] += stale
                if not dry_run:
                    await d.vector.delete_where(key, {
                        "must": [{"key": "document_id", "match": {"value": doc_id}}],
                        "must_not": [{"key": "version", "match": {"value": live}}]})

        # graph
        if coll["graph"] and d.graph:
            g_counts: dict[str, int] = {}
            if await d.graph.exists(key):
                res = await d.graph.query(key, "MATCH (dd:Document) OPTIONAL MATCH (dd)-[:HAS_CHUNK]->(c:Chunk) "
                                               "RETURN dd.id, count(c)", None, read_only=True, timeout_s=120)
                g_counts = {doc: n for doc, n in res["rows"]}
            for doc in g_counts:
                if doc not in docs:
                    found["orphan_graph"] += 1
                    if not dry_run:
                        await self.remove_graph(key, doc)
            for doc_id, r in docs.items():
                if r["status"] == "ready" and doc_id not in busy and g_counts.get(doc_id) != r["chunks"]:
                    found["graph_mismatch"] += 1
                    if doc_id not in to_index:
                        to_graph.add(doc_id)

        if not dry_run:
            for doc_id in to_index:
                await d.rows("INSERT INTO swsdb.outbox (document_id, op, version) VALUES (%s, 'index', %s)",
                             [doc_id, docs[doc_id]["live_version"]])
            for doc_id in to_graph:
                await d.rows("INSERT INTO swsdb.outbox (document_id, op, version) VALUES (%s, 'graph', %s)",
                             [doc_id, docs[doc_id]["live_version"]])
        return {"tenant": tenant, "collection": name, "documents": len(docs), "found": found,
                "queued": {"index": len(to_index), "graph": len(to_graph)} if not dry_run else {}}
