"""Retrieval evaluation and per-document diagnosis.

Evaluation: a golden set is a list of questions, each with the documents that should answer it (and optionally a phrase
the answer contains). A run asks every question under each configuration and reports, per configuration: hit rate,
recall and MRR over the documents in the returned evidence, nDCG, how often the answer phrase was in the evidence,
the evidence size, and p50/p95 latency. Runs are stored, so changes can be compared over time.

Diagnosis: everything about one document across the three stores, and a plain verdict on whether queries can find it.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from .names import engine_name
from .stores import NotFound, StoreError

if TYPE_CHECKING:
    from .documents import Documents

DEFAULT_CONFIGS = [
    {"name": "hybrid", "mode": "hybrid", "graph_hops": 0},
    {"name": "dense", "mode": "dense", "graph_hops": 0},
    {"name": "keyword", "mode": "sparse", "graph_hops": 0},
    {"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1},
]


def score_case(returned_docs: list[str], texts: list[str], relevant: list[str], answer: str | None) -> dict:
    """Metrics for one question. `returned_docs` is the document of each returned hit, in order."""
    ranked: list[str] = []
    for d in returned_docs:  # rank by first appearance of each document
        if d not in ranked:
            ranked.append(d)
    rel = set(relevant)
    found = [d for d in ranked if d in rel]
    first = next((i for i, d in enumerate(ranked) if d in rel), None)
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked) if d in rel)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(rel), len(ranked)) or 1))
    out = {
        "hit": 1.0 if found else 0.0,
        "recall": len(found) / len(rel) if rel else 0.0,
        "mrr": 1 / (first + 1) if first is not None else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
        "evidence": len(returned_docs),
    }
    if answer:
        out["answer_found"] = 1.0 if any(answer.lower() in (t or "").lower() for t in texts) else 0.0
    return out


def summarise(cases: list[dict], latencies: list[float]) -> dict:
    keys = [k for k in ("hit", "recall", "mrr", "ndcg", "answer_found", "evidence") if any(k in c for c in cases)]
    out = {k: round(statistics.fmean(c[k] for c in cases if k in c), 4) for k in keys}
    lat = sorted(latencies)
    out["latency_p50_ms"] = round(lat[len(lat) // 2] * 1000, 1)
    out["latency_p95_ms"] = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000, 1)
    return out


class Evaluator:
    def __init__(self, docs: Documents):
        self.d = docs

    async def put_set(self, tenant: str, collection: str, name: str, cases: list[dict]) -> dict:
        await self.d.collection(tenant, collection)
        if not cases or len(cases) > 2000:
            raise StoreError("a golden set needs 1-2000 questions")
        for c in cases:
            if not isinstance(c.get("query"), str) or not c["query"].strip():
                raise StoreError("every case needs a query")
            if not isinstance(c.get("relevant"), list) or not c["relevant"]:
                raise StoreError(f"case {c['query'][:60]!r}: list the documents that answer it in `relevant`")
        async with self.d.sql.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO swsdb.eval_sets (tenant, collection, name) VALUES (%s, %s, %s) "
                "ON CONFLICT (tenant, collection, name) DO UPDATE SET updated_at = now() RETURNING id",
                [tenant, collection, name])
            set_id = (await cur.fetchone())[0]
            await conn.execute("DELETE FROM swsdb.eval_cases WHERE set_id = %s", [set_id])
            async with conn.cursor() as c2:
                await c2.executemany(
                    "INSERT INTO swsdb.eval_cases (set_id, ordinal, query, relevant, answer) VALUES (%s,%s,%s,%s,%s)",
                    [(set_id, i, c["query"], [str(x) for x in c["relevant"]], c.get("answer"))
                     for i, c in enumerate(cases)])
        return {"set": name, "cases": len(cases)}

    async def sets(self, tenant: str, collection: str) -> list[dict]:
        return await self.d.rows(
            "SELECT s.name, count(c.*) AS cases, s.updated_at FROM swsdb.eval_sets s "
            "LEFT JOIN swsdb.eval_cases c ON c.set_id = s.id WHERE s.tenant = %s AND s.collection = %s "
            "GROUP BY s.id ORDER BY s.name", [tenant, collection])

    async def run(self, tenant: str, collection: str, set_name: str, configs: list[dict] | None, k: int,
                  principals: list[str] | None, role: str) -> dict:
        s = await self.d.one("SELECT id FROM swsdb.eval_sets WHERE tenant = %s AND collection = %s AND name = %s",
                             [tenant, collection, set_name])
        if s is None:
            raise NotFound(f"golden set {set_name!r} not found in {collection!r}")
        cases = await self.d.rows("SELECT query, relevant, answer FROM swsdb.eval_cases WHERE set_id = %s "
                                  "ORDER BY ordinal", [s["id"]])
        coll = await self.d.collection(tenant, collection)
        configs = configs or [c for c in DEFAULT_CONFIGS if coll["graph"] or not c["graph_hops"]]
        t0 = time.monotonic()
        results = []
        for cfg in configs:
            scored, latencies, misses = [], [], []
            for c in cases:
                q0 = time.perf_counter()
                res = await self.d.query(tenant, collection, c["query"], k=k, mode=cfg.get("mode", "hybrid"),
                                         where=cfg.get("where"), principals=principals, role=role,
                                         graph_hops=cfg.get("graph_hops", 0), graph_k=cfg.get("graph_k", 5),
                                         rerank=cfg.get("rerank", False))
                latencies.append(time.perf_counter() - q0)
                hits = res["hits"]
                sc = score_case([h["document"] for h in hits], [h["text"] for h in hits], c["relevant"], c["answer"])
                scored.append(sc)
                if not sc["hit"]:
                    misses.append({"query": c["query"], "expected": c["relevant"],
                                   "got": list(dict.fromkeys(h["document"] for h in hits))[:5]})
            results.append({"config": cfg, "metrics": summarise(scored, latencies), "misses": misses[:20]})
        report = {"set": set_name, "collection": collection, "cases": len(cases), "k": k, "results": results,
                  "best": max(results, key=lambda r: (r["metrics"]["recall"], r["metrics"]["mrr"]))["config"]["name"],
                  "took_s": round(time.monotonic() - t0, 2)}
        row = await self.d.one("INSERT INTO swsdb.eval_runs (set_id, report) VALUES (%s, %s) "
                               "RETURNING id, created_at", [s["id"], Jsonb(report)])
        return {"id": row["id"], "created_at": row["created_at"].isoformat(), **report}

    async def runs(self, tenant: str, collection: str, limit: int = 20) -> list[dict]:
        rows = await self.d.rows(
            "SELECT r.id, r.created_at, r.report FROM swsdb.eval_runs r JOIN swsdb.eval_sets s ON s.id = r.set_id "
            "WHERE s.tenant = %s AND s.collection = %s ORDER BY r.id DESC LIMIT %s", [tenant, collection, limit])
        return [{"id": r["id"], "created_at": r["created_at"].isoformat(), **r["report"]} for r in rows]

    # ------------------------------------------------------------------ diagnosis

    async def diagnose(self, tenant: str, collection: str, external_id: str) -> dict:
        from .documents import doc_public, doc_uuid
        d = self.d
        coll = await d.collection(tenant, collection)
        doc_id = doc_uuid(tenant, collection, external_id)
        row = await d.one("SELECT * FROM swsdb.documents WHERE id = %s", [doc_id])
        if row is None:
            raise NotFound(f"document {external_id!r} not found in {collection!r}")
        key = engine_name(tenant, collection)
        pending = await d.rows("SELECT op, version, attempts, last_error, next_attempt_at, locked_until IS NOT NULL "
                               "AND locked_until > now() AS running FROM swsdb.outbox WHERE document_id = %s "
                               "ORDER BY id", [doc_id])
        out: dict[str, Any] = {"document": doc_public(row), "collection": {"graph": coll["graph"],
                                                                           "embedding": coll["embedding"]},
                               "pending": [{**p, "next_attempt_at": p["next_attempt_at"].isoformat()}
                                           for p in pending]}
        live = row["live_version"]
        doc_f = {"key": "document_id", "match": {"value": doc_id}}
        try:
            v_live = await d.vector.count(key, {"must": [doc_f, {"key": "version", "match": {"value": live}},
                                                         {"key": "live", "match": {"value": True}}]}) if live else 0
            v_all = await d.vector.count(key, {"must": [doc_f]})
            out["vectors"] = {"live": v_live, "all_versions": v_all}
        except Exception as e:
            out["vectors"] = {"error": f"{type(e).__name__}: {e}"}
        if coll["graph"] and d.graph:
            try:
                g = await d.graph.query(key, "MATCH (dd:Document {id: $doc}) OPTIONAL MATCH (dd)-[:HAS_CHUNK]->"
                                             "(c:Chunk) OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
                                             "RETURN count(DISTINCT dd), count(DISTINCT c), count(DISTINCT e)",
                                        {"doc": doc_id}, read_only=True, timeout_s=30) \
                    if await d.graph.exists(key) else {"rows": [[0, 0, 0]]}
                out["graph"] = dict(zip(("document", "chunks", "entities"), g["rows"][0], strict=True))
            except Exception as e:
                out["graph"] = {"error": f"{type(e).__name__}: {e}"}
        out["verdict"], out["reasons"] = verdict(row, pending, out.get("vectors", {}), out.get("graph"), coll)
        return out


def verdict(row: dict, pending: list[dict], vectors: dict, graph: dict | None, coll: dict) -> tuple[str, list[str]]:
    """'searchable', 'not searchable' or 'searchable, with issues', and why, in plain words."""
    reasons: list[str] = []
    st = row["status"]
    retrying = [p for p in pending if p["attempts"] > 0 and p["last_error"]]
    if st == "deleting":
        return "not searchable", ["it is being deleted; queries already exclude it"]
    if st == "failed":
        return "not searchable", [f"processing failed: {row['error']}", "fix the file and upload it again"]
    if st in ("queued", "processing") and row["live_version"] is None:
        reasons.append(f"the first version is still {st}")
        if retrying:
            reasons.append(f"retrying after {retrying[0]['attempts']} attempts: {retrying[0]['last_error'][:300]}")
        return "not searchable yet", reasons
    if st in ("queued", "processing"):
        reasons.append(f"version {row['version']} is {st}; queries still return version {row['live_version']}")
        if retrying:
            reasons.append(f"retrying: {retrying[0]['last_error'][:300]}")
    if "error" in vectors:
        reasons.append(f"the vector engine can't be checked: {vectors['error']}")
    elif vectors.get("live", 0) != row["chunks"]:
        reasons.append(f"Qdrant holds {vectors.get('live', 0)} live vectors but the catalog has {row['chunks']} chunks:"
                       " run swsdb doctor --repair")
    if graph is not None:
        if "error" in graph:
            reasons.append(f"the graph engine can't be checked: {graph['error']}")
        elif graph.get("chunks") != row["chunks"]:
            reasons.append(f"the graph holds {graph.get('chunks')} chunks, the catalog {row['chunks']} (graph "
                           "expansion may miss it): run swsdb doctor --repair")
    if row["readers"] != ["*"]:
        reasons.append(f"only readers {', '.join(row['readers'])} (and admin requests without principals) see it")
    if not reasons:
        return "searchable", ["every store holds the live version"]
    hard = any("live vectors" in r for r in reasons)
    return ("not fully searchable" if hard else "searchable"), reasons
