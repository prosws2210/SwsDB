"""REST API v1: collections, vectors, graph and SQL."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from . import answer as answer_mod
from .auth import Caller
from .documents import Documents
from .evaluate import Evaluator
from .names import DEFAULT_TENANT, NameError_, check, engine_name
from .stores import GraphStore, NotFound, SqlStore, VectorSpec, VectorStore, check_pass_through_cypher

MAX_POINTS = 1000


# ------------------------------------------------------------------ request bodies


class VectorConfig(BaseModel):
    size: int = Field(ge=1, le=65536)
    distance: Literal["cosine", "dot", "euclid", "manhattan"] = "cosine"


class CreateCollection(BaseModel):
    vector: VectorConfig | None = None  # raw vectors you embed yourself
    documents: bool = False  # documents swsdb chunks and embeds (dense + BM25), with optional entity graph
    graph: bool | None = None  # default: yes when the graph engine is enabled


class Point(BaseModel):
    id: str | int
    vector: list[float] = Field(min_length=1)
    payload: dict[str, Any] | None = None


class Upsert(BaseModel):
    points: list[Point] = Field(min_length=1, max_length=MAX_POINTS)


class Ids(BaseModel):
    ids: list[str | int] = Field(min_length=1, max_length=MAX_POINTS)


class Search(BaseModel):
    vector: list[float] = Field(min_length=1)
    limit: int = Field(10, ge=1, le=1000)
    where: dict[str, Any] | None = None
    with_payload: bool = True


class Cypher(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    params: dict[str, Any] | None = None
    read_only: bool = False
    timeout_s: float = Field(30, gt=0, le=600)


class TextDocument(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)
    format: Literal["markdown", "text", "html"] = "markdown"
    title: str | None = Field(None, max_length=1000)
    metadata: dict[str, Any] | None = None
    readers: list[str] | None = None


class Query(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    k: int = Field(10, ge=1, le=200)
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    where: dict[str, Any] | None = None  # metadata fields (exact match; lists mean any of); "document" for ids
    principals: list[str] | None = None  # who is asking, e.g. ["user:42", "group:support"] (admin key only)
    graph_hops: int | None = Field(None, ge=0, le=2)  # default: 1 when the collection has a graph
    graph_k: int = Field(5, ge=0, le=50)  # connected chunks added after the k search hits


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)
    k: int = Field(5, ge=1, le=20)  # passages the answer may use
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    where: dict[str, Any] | None = None
    principals: list[str] | None = None  # who is asking (admin key only)
    graph_hops: int | None = Field(None, ge=0, le=2)
    graph_k: int = Field(3, ge=0, le=20)
    generate: bool = True  # False: always the extractive answer, even with a model configured


class EvalCase(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    relevant: list[str] = Field(min_length=1)  # ids of the documents that answer it
    answer: str | None = None  # optional phrase the answer contains (checked in the returned text)


class EvalSet(BaseModel):
    cases: list[EvalCase] = Field(min_length=1, max_length=2000)


class EvalConfig(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    graph_hops: int = Field(0, ge=0, le=2)
    graph_k: int = Field(5, ge=0, le=50)
    where: dict[str, Any] | None = None


class EvalRun(BaseModel):
    set: str
    configs: list[EvalConfig] | None = None  # default: hybrid, dense, keyword and hybrid+graph
    k: int = Field(5, ge=1, le=50)
    principals: list[str] | None = None


class Reconcile(BaseModel):
    dry_run: bool = True
    all_tenants: bool = False


class NewKey(BaseModel):
    role: Literal["admin", "read"] = "read"
    name: str | None = Field(None, max_length=200)
    tenant: str | None = None  # default: the request's tenant


class NewSnapshot(BaseModel):
    name: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")


class Restore(BaseModel):
    backup_first: bool = True  # snapshot the current data first, so the restore can be undone


class Sql(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    params: list[Any] | dict[str, Any] | None = None
    read_only: bool = False
    timeout_s: float = Field(30, gt=0, le=600)
    max_rows: int = Field(1000, ge=1, le=100_000)

    @field_validator("query")
    @classmethod
    def _one(cls, v: str) -> str:
        return v.strip()


# ------------------------------------------------------------------ dependencies


def collection_name(name: str) -> str:
    try:
        return check("collection", name)
    except NameError_ as e:
        raise HTTPException(422, str(e)) from None


class Engines:
    """The stores, each only if its engine is enabled; `need` turns a missing or unready engine into a clear error."""

    def __init__(self, request: Request):
        self.app = request.app
        self.s = request.app.state.settings
        self.monitor = request.app.state.monitor

    def need(self, cap: str):
        names = {"sql": "SQL (PostgreSQL)", "vector": "vector (Qdrant)", "graph": "graph (FalkorDB)"}
        if cap not in self.s.engines:
            raise HTTPException(409, f"the {names[cap]} engine is not enabled in this swsdb "
                                     f"(SWSDB_ENGINES={','.join(self.s.engines)})")
        st = self.monitor.status.get(cap)
        if st is None or st.state in ("starting", "failed"):
            raise HTTPException(503, f"the {names[cap]} engine is {st.state if st else 'unknown'}"
                                     + (f": {st.error}" if st and st.error else ""))
        if st.state == "down":
            # the monitor's view can be seconds old (the engine may already be back): try the engine itself, which
            # answers 503 with the real error if it is still down, and refresh the monitor now
            self.monitor.recheck(cap)
        return {"sql": self.app.state.sql, "vector": self.app.state.vector, "graph": self.app.state.graph}[cap]

    @property
    def vector(self) -> VectorStore:
        return self.need("vector")

    @property
    def graph(self) -> GraphStore:
        return self.need("graph")

    @property
    def sql(self) -> SqlStore:
        return self.need("sql")

    def has(self, cap: str) -> bool:
        return cap in self.s.engines

    @property
    def docs(self) -> Documents:
        """Uploads, deletes and listing need only Postgres (the source of truth): changes are queued and applied
        to the vector and graph stores when they're available."""
        self.need("sql")
        if not self.has("vector"):
            self.need("vector")
        d = self.app.state.docs
        if d is None:
            raise HTTPException(503, "document processing is starting")
        return d


def router(caller) -> APIRouter:
    r = APIRouter(prefix="/v1")

    async def require_key(c: Caller = Depends(caller)) -> str:
        return c.role

    def admin(c: Caller = Depends(caller)) -> str:
        if c.role != "admin":
            raise HTTPException(403, "this needs the admin API key; the read-only key can only read and search")
        return c.role

    def tenant(swsdb_tenant: str | None = Header(None), c: Caller = Depends(caller)) -> str:
        """The request's tenant: the `swsdb-tenant` header, else the key's tenant, else `default`. A tenant's key
        reaches only its own tenant."""
        try:
            t = check("tenant", swsdb_tenant or c.tenant or DEFAULT_TENANT)
        except NameError_ as e:
            raise HTTPException(422, str(e)) from None
        if c.tenant is not None and t != c.tenant:
            raise HTTPException(403, f"this key belongs to tenant {c.tenant!r} and can't reach tenant {t!r}")
        return t

    def writable(request: Request) -> None:
        """Writes that go straight to the vector or graph store wait while a snapshot copies them."""
        if request.app.state.maintenance:
            raise HTTPException(503, f"a {request.app.state.maintenance} is running; this write can be retried in a "
                                     "few seconds")

    def global_admin(c: Caller = Depends(caller)) -> Caller:
        if c.role != "admin" or not c.is_global:
            raise HTTPException(403, "snapshots cover every tenant: they need the admin key from the environment")
        return c

    async def documents_guard(t: str, name: str, e: Engines, role: str, what: str) -> None:
        """Raw access to a documents collection skips document readers and deletes in progress: admin only."""
        if role == "admin":
            return
        d = e.app.state.docs
        if d is not None:
            is_docs = await d.is_document_collection(t, name)
        else:
            is_docs = e.has("vector") and await e.vector.is_documents(engine_name(t, name))
        if is_docs:
            raise HTTPException(403, f"{what} on a documents collection needs the admin key, since it would skip "
                                     "document readers; use /query, which applies them")

    # -------------------------------------------------------------- collections

    @r.get("/collections", tags=["collections"])
    async def list_collections(t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(require_key)):
        vec = set(await e.vector.list(t)) if e.has("vector") else set()
        gra = set(await e.graph.list(t)) if e.has("graph") else set()
        return {"collections": [{"name": n, "vector": n in vec, "graph": n in gra} for n in sorted(vec | gra)]}

    @r.put("/collections/{name}", tags=["collections"], status_code=201)
    async def create_collection(body: CreateCollection, name: str = Depends(collection_name),
                                t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin),
                                __: None = Depends(writable)):
        want_graph = body.graph if body.graph is not None else e.has("graph")
        if body.documents:
            if body.vector is not None:
                raise HTTPException(422, "a documents collection embeds for you: leave out `vector`")
            e.need("vector")
            if want_graph:
                e.need("graph")
            await e.docs.create_collection(t, name, graph=want_graph)
            return await describe(name, t, e)
        if body.vector is None and not want_graph:
            raise HTTPException(422, "a collection needs vectors (`vector: {size, distance}`), documents, a graph, "
                                     "or a combination")
        key = engine_name(t, name)
        if body.vector is not None:
            await e.vector.create(key, VectorSpec(body.vector.size, body.vector.distance))
        if want_graph:
            g = e.graph
            if not await g.exists(key):  # FalkorDB creates a graph on its first query
                await g.query(key, "RETURN 1", None, read_only=False, timeout_s=10)
        return await describe(name, t, e)

    async def describe(name: str, t: str, e: Engines) -> dict:
        key = engine_name(t, name)
        out: dict[str, Any] = {"name": name, "tenant": t, "vector": None, "graph": None}
        if e.has("vector"):
            try:
                out["vector"] = await e.vector.info(key)
            except NotFound:
                pass
        if e.has("graph") and await e.graph.exists(key):
            res = await e.graph.query(key, "MATCH (n) RETURN count(n)", None, read_only=True, timeout_s=10)
            out["graph"] = {"nodes": res["rows"][0][0]}
        docs = e.app.state.docs
        if e.has("sql") and docs and await docs.is_document_collection(t, name):
            coll = await docs.collection(t, name)
            rows = await docs.rows("SELECT status, count(*) AS n FROM swsdb.documents WHERE tenant = %s "
                                   "AND collection = %s GROUP BY status", [t, name])
            by_status = {r["status"]: r["n"] for r in rows}
            out["documents"] = {"count": sum(by_status.values()), "by_status": by_status,
                                "embedding": coll["embedding"], "dim": coll["dim"], "graph": coll["graph"],
                                "created_at": coll["created_at"].isoformat()}
        if out["vector"] is None and out["graph"] is None:
            raise NotFound(f"collection {name!r} not found")
        return out

    @r.get("/collections/{name}", tags=["collections"])
    async def get_collection(name: str = Depends(collection_name), t: str = Depends(tenant),
                             e: Engines = Depends(), _: str = Depends(require_key)):
        return await describe(name, t, e)

    @r.delete("/collections/{name}", tags=["collections"])
    async def delete_collection(name: str = Depends(collection_name), t: str = Depends(tenant),
                                e: Engines = Depends(), _: str = Depends(admin), __: None = Depends(writable)):
        key = engine_name(t, name)
        removed = {}
        if e.has("vector"):
            try:
                removed["vector"] = await e.vector.drop(key)
            except NotFound:
                removed["vector"] = False
        if e.has("graph"):
            removed["graph"] = await e.graph.drop(key)
        if e.has("sql") and e.app.state.docs:
            removed["documents"] = await e.app.state.docs.drop_collection(t, name)
        if not any(removed.values()):
            raise NotFound(f"collection {name!r} not found")
        return {"deleted": name, **removed}

    # -------------------------------------------------------------- vectors

    @r.put("/collections/{name}/points", tags=["vectors"])
    async def upsert(body: Upsert, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), _: str = Depends(admin), __: None = Depends(writable)):
        n = await e.vector.upsert(engine_name(t, name), [p.model_dump() for p in body.points])
        return {"upserted": n}

    @r.post("/collections/{name}/points/get", tags=["vectors"])
    async def get_points(body: Ids, name: str = Depends(collection_name), t: str = Depends(tenant),
                         e: Engines = Depends(), role: str = Depends(require_key)):
        await documents_guard(t, name, e, role, "reading raw points")
        return {"points": await e.vector.get(engine_name(t, name), body.ids)}

    @r.post("/collections/{name}/points/delete", tags=["vectors"])
    async def delete_points(body: Ids, name: str = Depends(collection_name), t: str = Depends(tenant),
                            e: Engines = Depends(), _: str = Depends(admin), __: None = Depends(writable)):
        await e.vector.delete(engine_name(t, name), body.ids)
        return {"deleted": len(body.ids)}

    @r.post("/collections/{name}/search", tags=["vectors"])
    async def search(body: Search, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), role: str = Depends(require_key)):
        await documents_guard(t, name, e, role, "raw vector search")
        hits = await e.vector.search(engine_name(t, name), body.vector, body.limit, body.where, body.with_payload)
        return {"hits": hits}

    # -------------------------------------------------------------- graph

    @r.post("/collections/{name}/cypher", tags=["graph"])
    async def cypher(body: Cypher, request: Request, name: str = Depends(collection_name),
                     t: str = Depends(tenant), e: Engines = Depends(), role: str = Depends(require_key)):
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        if not body.read_only:
            writable(request)
        check_pass_through_cypher(body.query)
        key = engine_name(t, name)
        g = e.graph
        if not await g.exists(key):
            raise NotFound(f"collection {name!r} has no graph (create it with graph: true)")
        await documents_guard(t, name, e, role, "Cypher")
        return await g.query(key, body.query, body.params, read_only=body.read_only, timeout_s=body.timeout_s)

    # -------------------------------------------------------------- documents

    @r.post("/collections/{name}/documents", tags=["documents"], status_code=202)
    async def upload(file: UploadFile = File(...), id: str | None = Form(None), title: str | None = Form(None),
                     metadata: str | None = Form(None), readers: str | None = Form(None),
                     name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                     _: str = Depends(admin)):
        """Upload a file (PDF, Markdown, HTML, text). `metadata` and `readers` are JSON (an object and a list)."""
        try:
            meta = json.loads(metadata) if metadata else None
            who = json.loads(readers) if readers else None
        except json.JSONDecodeError as err:
            raise HTTPException(422, f"metadata and readers must be JSON: {err}") from None
        data = await file.read()
        return await e.docs.submit(t, name, id or file.filename or "document", data, file.filename or "document.txt",
                                   title, meta, who)

    @r.post("/collections/{name}/documents/text", tags=["documents"], status_code=202)
    async def upload_text(body: TextDocument, name: str = Depends(collection_name), t: str = Depends(tenant),
                          e: Engines = Depends(), _: str = Depends(admin)):
        ext = {"markdown": ".md", "text": ".txt", "html": ".html"}[body.format]
        return await e.docs.submit(t, name, body.id, body.text.encode(), f"document{ext}", body.title,
                                   body.metadata, body.readers)

    @r.get("/collections/{name}/documents", tags=["documents"])
    async def list_documents(limit: int = 100, offset: int = 0, status: str | None = None,
                             name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                             _: str = Depends(admin)):
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1-1000 and offset >= 0")
        return await e.docs.list(t, name, limit, offset, status)

    @r.get("/collections/{name}/documents/{doc_id:path}", tags=["documents"])
    async def get_document(doc_id: str, name: str = Depends(collection_name), t: str = Depends(tenant),
                           e: Engines = Depends(), _: str = Depends(admin)):
        return await e.docs.get(t, name, doc_id)

    @r.delete("/collections/{name}/documents/{doc_id:path}", tags=["documents"])
    async def delete_document(doc_id: str, name: str = Depends(collection_name), t: str = Depends(tenant),
                              e: Engines = Depends(), _: str = Depends(admin)):
        return await e.docs.delete(t, name, doc_id)

    @r.post("/collections/{name}/query", tags=["documents"])
    async def query(body: Query, name: str = Depends(collection_name), t: str = Depends(tenant),
                    e: Engines = Depends(), role: str = Depends(require_key)):
        """Hybrid retrieval (dense + BM25, fused) with optional graph expansion, filtered by who is asking."""
        if body.principals is not None and role != "admin":
            raise HTTPException(403, "only the admin key may act for principals; a read-only key sees public "
                                     "documents only")
        if body.graph_hops and not e.has("graph"):
            raise HTTPException(409, "graph_hops needs the graph engine")
        e.need("vector")
        return await e.docs.query(t, name, body.query, k=body.k, mode=body.mode, where=body.where,
                                  principals=body.principals, role=role, graph_hops=body.graph_hops,
                                  graph_k=body.graph_k)

    @r.post("/collections/{name}/answer", tags=["documents"])
    async def answer(body: Ask, request: Request, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), role: str = Depends(require_key)):
        """A short answer that cites the passages it uses as [n], from what the caller may read only."""
        if body.principals is not None and role != "admin":
            raise HTTPException(403, "only the admin key may act for principals")
        if body.graph_hops and not e.has("graph"):
            raise HTTPException(409, "graph_hops needs the graph engine")
        e.need("vector")
        t0 = time.perf_counter()
        found = await e.docs.query(t, name, body.question, k=body.k, mode=body.mode, where=body.where,
                                   principals=body.principals, role=role, graph_hops=body.graph_hops,
                                   graph_k=body.graph_k)
        hits = found["hits"]
        retrieval_ms = round((time.perf_counter() - t0) * 1000, 1)
        model, mode, note, text = None, "extractive", None, ""
        answerer = request.app.state.answerer
        if hits and answerer is not None and body.generate:
            try:
                text = answer_mod.strip_unknown(await answerer.generate(body.question, hits), len(hits))
                model, mode = answerer.model, "generated"
            except answer_mod.AnswerError as err:
                note = f"the model failed ({err}); this is the extractive answer"
        if mode == "extractive":
            text = answer_mod.extractive(body.question, hits)
        cited = answer_mod.citations(text, len(hits))
        if not text:
            text = "The documents you can read don't answer this."
        passages = [{"n": i, "cited": i in cited, **{k: h.get(k) for k in (
            "document", "title", "page", "heading", "chunk", "chunk_id", "text", "score", "via", "graph_path")}}
            for i, h in enumerate(hits, 1)]
        return {"answer": text, "cited": cited, "passages": passages, "mode": mode, "model": model,
                **({"note": note} if note else {}), "retrieval_ms": retrieval_ms,
                "took_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @r.get("/collections/{name}/text/{doc_id:path}", tags=["documents"])
    async def document_text(doc_id: str, principals: str | None = None, name: str = Depends(collection_name),
                            t: str = Depends(tenant), e: Engines = Depends(), role: str = Depends(require_key)):
        """A document's text as it is searched (its live version), for anyone who may read it. `principals` is a
        comma-separated list (admin key only)."""
        who = [p.strip() for p in principals.split(",") if p.strip()] if principals else None
        if who is not None and role != "admin":
            raise HTTPException(403, "only the admin key may act for principals")
        return await e.docs.text(t, name, doc_id, role, who)

    # -------------------------------------------------------------- evaluation and diagnosis

    @r.get("/collections/{name}/diagnose/{doc_id:path}", tags=["documents"])
    async def diagnose(doc_id: str, name: str = Depends(collection_name), t: str = Depends(tenant),
                       e: Engines = Depends(), _: str = Depends(admin)):
        """Why a document is or isn't searchable: catalog, pending changes, vectors and graph, in plain words."""
        return await Evaluator(e.docs).diagnose(t, name, doc_id)

    @r.put("/collections/{name}/eval/sets/{set_name}", tags=["evaluation"])
    async def put_eval_set(set_name: str, body: EvalSet, name: str = Depends(collection_name),
                           t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        """Create or replace a golden set: questions and the documents that should answer them."""
        check("eval set", set_name)
        return await Evaluator(e.docs).put_set(t, name, set_name, [c.model_dump() for c in body.cases])

    @r.get("/collections/{name}/eval/sets", tags=["evaluation"])
    async def eval_sets(name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                        _: str = Depends(admin)):
        rows = await Evaluator(e.docs).sets(t, name)
        return {"sets": [{**r, "updated_at": r["updated_at"].isoformat()} for r in rows]}

    @r.post("/collections/{name}/eval/runs", tags=["evaluation"])
    async def eval_run(body: EvalRun, name: str = Depends(collection_name), t: str = Depends(tenant),
                       e: Engines = Depends(), role: str = Depends(admin)):
        """Ask every question under each configuration; report hit rate, recall, MRR, nDCG and latency."""
        e.need("vector")
        configs = [c.model_dump() for c in body.configs] if body.configs else None
        return await Evaluator(e.docs).run(t, name, body.set, configs, body.k, body.principals, role)

    @r.get("/collections/{name}/eval/runs", tags=["evaluation"])
    async def eval_runs(name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                        _: str = Depends(admin)):
        return {"runs": await Evaluator(e.docs).runs(t, name)}

    # -------------------------------------------------------------- consistency

    @r.post("/admin/reconcile", tags=["admin"])
    async def reconcile(body: Reconcile, t: str = Depends(tenant), e: Engines = Depends(),
                        _: str = Depends(admin), c: Caller = Depends(caller)):
        """Compare the catalog with the vector and graph stores and queue repairs (dry_run: only report)."""
        if body.all_tenants and not c.is_global:
            raise HTTPException(403, "all_tenants needs a key from the environment; a tenant's key covers its tenant")
        e.need("vector")
        return await e.docs.sync.reconcile(None if body.all_tenants else t, dry_run=body.dry_run)

    # -------------------------------------------------------------- SQL

    @r.post("/sql", tags=["sql"])
    async def sql(body: Sql, e: Engines = Depends(), role: str = Depends(require_key), t: str = Depends(tenant)):
        """One statement in the tenant's own schema, as a role that can reach nothing else (the default tenant uses
        `public`)."""
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        return await e.sql.query(body.query, body.params, read_only=body.read_only, timeout_s=body.timeout_s,
                                 max_rows=body.max_rows, tenant=t)

    # -------------------------------------------------------------- snapshots

    @r.post("/admin/snapshots", tags=["snapshots"], status_code=201)
    async def create_snapshot(request: Request, body: NewSnapshot | None = None, _: Caller = Depends(global_admin)):
        """One consistent snapshot of every engine and the uploaded files, as a single tar file on the volume."""
        return await request.app.state.snapshots.create(body.name if body else None)

    @r.get("/admin/snapshots", tags=["snapshots"])
    async def list_snapshots(request: Request, _: Caller = Depends(global_admin)):
        return {"snapshots": request.app.state.snapshots.list()}

    @r.get("/admin/snapshots/{sid}", tags=["snapshots"])
    async def get_snapshot(sid: str, request: Request, _: Caller = Depends(global_admin)):
        return request.app.state.snapshots.manifest(sid)

    @r.get("/admin/snapshots/{sid}/download", tags=["snapshots"])
    async def download_snapshot(sid: str, request: Request, _: Caller = Depends(global_admin)):
        snaps = request.app.state.snapshots
        snaps.manifest(sid)  # 404 / 400 before streaming
        return FileResponse(snaps.path(sid), media_type="application/x-tar", filename=f"{sid}.tar")

    @r.put("/admin/snapshots/{sid}", tags=["snapshots"], status_code=201)
    async def upload_snapshot(sid: str, request: Request, _: Caller = Depends(global_admin)):
        """Upload a snapshot file (the request body is the tar), e.g. to restore it on another machine."""
        return await request.app.state.snapshots.receive(sid, request.stream())

    @r.delete("/admin/snapshots/{sid}", tags=["snapshots"])
    async def delete_snapshot(sid: str, request: Request, _: Caller = Depends(global_admin)):
        request.app.state.snapshots.delete(sid)
        return {"deleted": sid}

    @r.post("/admin/snapshots/{sid}/restore", tags=["snapshots"])
    async def restore_snapshot(sid: str, request: Request, body: Restore | None = None,
                               _: Caller = Depends(global_admin)):
        """Replace every engine's data with the snapshot's. By default the current data is snapshotted first."""
        return await request.app.state.snapshots.restore(sid, backup_first=body.backup_first if body else True)

    # -------------------------------------------------------------- managed API keys

    @r.post("/keys", tags=["keys"], status_code=201)
    async def create_key(body: NewKey, request: Request, t: str = Depends(tenant), c: Caller = Depends(caller),
                         _: str = Depends(admin)):
        """A key for one tenant. The response holds the key itself, shown only this once (swsdb keeps a hash)."""
        if request.app.state.settings.api_key is None:
            raise HTTPException(409, "set SWSDB_API_KEY first: once a managed key exists the API stops being open, "
                                     "and someone needs a key that reaches every tenant")
        target = check("tenant", body.tenant or t)
        if c.tenant is not None and target != c.tenant:
            raise HTTPException(403, f"this key belongs to tenant {c.tenant!r}")
        return await request.app.state.keys.create(target, body.role, body.name)

    @r.get("/keys", tags=["keys"])
    async def list_keys(request: Request, all_tenants: bool = False, t: str = Depends(tenant),
                        c: Caller = Depends(caller), _: str = Depends(admin)):
        if all_tenants and not c.is_global:
            raise HTTPException(403, "all_tenants needs a key from the environment")
        return {"keys": await request.app.state.keys.list(None if all_tenants else t)}

    @r.delete("/keys/{key_id}", tags=["keys"])
    async def revoke_key(key_id: str, request: Request, c: Caller = Depends(caller), _: str = Depends(admin)):
        if not await request.app.state.keys.revoke(key_id, c.tenant):
            raise NotFound(f"key {key_id!r} not found (or already revoked)")
        return {"revoked": key_id}

    return r
