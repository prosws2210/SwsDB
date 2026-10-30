"""REST API v1: collections, vectors, graph and SQL."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from .documents import Documents
from .evaluate import Evaluator
from .names import DEFAULT_TENANT, NameError_, check, engine_name
from .stores import GraphStore, NotFound, SqlStore, VectorSpec, VectorStore

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


def tenant(swsdb_tenant: str | None = Header(None)) -> str:
    try:
        return check("tenant", swsdb_tenant or DEFAULT_TENANT)
    except NameError_ as e:
        raise HTTPException(422, str(e)) from None


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


def router(require_key) -> APIRouter:
    r = APIRouter(prefix="/v1")

    def admin(role: str = Depends(require_key)) -> str:
        if role != "admin":
            raise HTTPException(403, "this needs the admin API key; the read-only key can only read and search")
        return role

    # -------------------------------------------------------------- collections

    @r.get("/collections", tags=["collections"])
    async def list_collections(t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(require_key)):
        vec = set(await e.vector.list(t)) if e.has("vector") else set()
        gra = set(await e.graph.list(t)) if e.has("graph") else set()
        return {"collections": [{"name": n, "vector": n in vec, "graph": n in gra} for n in sorted(vec | gra)]}

    @r.put("/collections/{name}", tags=["collections"], status_code=201)
    async def create_collection(body: CreateCollection, name: str = Depends(collection_name),
                                t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
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
            counts = await docs.list(t, name, 1, 0, None)
            out["documents"] = {"count": counts["total"], "embedding": coll["embedding"], "dim": coll["dim"],
                                "graph": coll["graph"]}
        if out["vector"] is None and out["graph"] is None:
            raise NotFound(f"collection {name!r} not found")
        return out

    @r.get("/collections/{name}", tags=["collections"])
    async def get_collection(name: str = Depends(collection_name), t: str = Depends(tenant),
                             e: Engines = Depends(), _: str = Depends(require_key)):
        return await describe(name, t, e)

    @r.delete("/collections/{name}", tags=["collections"])
    async def delete_collection(name: str = Depends(collection_name), t: str = Depends(tenant),
                                e: Engines = Depends(), _: str = Depends(admin)):
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
                     e: Engines = Depends(), _: str = Depends(admin)):
        n = await e.vector.upsert(engine_name(t, name), [p.model_dump() for p in body.points])
        return {"upserted": n}

    @r.post("/collections/{name}/points/get", tags=["vectors"])
    async def get_points(body: Ids, name: str = Depends(collection_name), t: str = Depends(tenant),
                         e: Engines = Depends(), _: str = Depends(require_key)):
        return {"points": await e.vector.get(engine_name(t, name), body.ids)}

    @r.post("/collections/{name}/points/delete", tags=["vectors"])
    async def delete_points(body: Ids, name: str = Depends(collection_name), t: str = Depends(tenant),
                            e: Engines = Depends(), _: str = Depends(admin)):
        await e.vector.delete(engine_name(t, name), body.ids)
        return {"deleted": len(body.ids)}

    @r.post("/collections/{name}/search", tags=["vectors"])
    async def search(body: Search, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), _: str = Depends(require_key)):
        hits = await e.vector.search(engine_name(t, name), body.vector, body.limit, body.where, body.with_payload)
        return {"hits": hits}

    # -------------------------------------------------------------- graph

    @r.post("/collections/{name}/cypher", tags=["graph"])
    async def cypher(body: Cypher, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), role: str = Depends(require_key)):
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        key = engine_name(t, name)
        g = e.graph
        if not await g.exists(key):
            raise NotFound(f"collection {name!r} has no graph (create it with graph: true)")
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
                        _: str = Depends(admin)):
        """Compare the catalog with the vector and graph stores and queue repairs (dry_run: only report)."""
        e.need("vector")
        return await e.docs.sync.reconcile(None if body.all_tenants else t, dry_run=body.dry_run)

    # -------------------------------------------------------------- SQL

    @r.post("/sql", tags=["sql"])
    async def sql(body: Sql, e: Engines = Depends(), role: str = Depends(require_key)):
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        return await e.sql.query(body.query, body.params, read_only=body.read_only, timeout_s=body.timeout_s,
                                 max_rows=body.max_rows)

    return r
