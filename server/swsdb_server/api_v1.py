"""REST API v1: collections, vectors, graph and SQL."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator

from . import answer as answer_mod
from . import graph_views, observe, vector_views
from .advisors import run_advisors
from .auth import Caller
from .dataapi import query_stats, roles_of
from .documents import Documents, doc_uuid
from .evaluate import Evaluator
from .names import DEFAULT_TENANT, NameError_, check, engine_name
from .neo4j import refuse_pass_through
from .stores import (
    Forbidden,
    GraphStore,
    NotFound,
    SqlStore,
    StoreError,
    Unavailable,
    VectorSpec,
    VectorStore,
    check_pass_through_cypher,
)

MAX_POINTS = 1000


class FailedSignIns:
    """Failed sign-ins per (tenant, email, address) in the last few minutes: past the limit, sign-in waits."""

    def __init__(self, limit: int = 10, window_s: float = 300):
        self.limit, self.window_s = limit, window_s
        self.fails: dict[tuple, list[float]] = {}

    def _recent(self, key: tuple) -> list[float]:
        now = time.time()
        recent = [x for x in self.fails.get(key, []) if now - x < self.window_s]
        if recent:
            self.fails[key] = recent
        else:
            self.fails.pop(key, None)
        return recent

    def blocked(self, key: tuple) -> bool:
        return len(self._recent(key)) >= self.limit

    def add(self, key: tuple) -> None:
        self.fails.setdefault(key, []).append(time.time())
        if len(self.fails) > 50_000:  # forget the oldest rather than grow without bound
            for k in list(self.fails)[:10_000]:
                self.fails.pop(k, None)


SIGNIN_FAILS = FailedSignIns()


async def json_body(request: Request, default: Any = None) -> Any:
    raw = await request.body()
    if not raw:
        if default is not None:
            return default
        raise HTTPException(422, "send a JSON body")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"the body is not JSON: {e}") from None


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


class Browse(BaseModel):
    limit: int = Field(50, ge=1, le=500)
    offset: str | int | None = None  # the `next` of the previous page
    where: dict[str, Any] | None = None
    with_vector: bool = False


class SignUp(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    tenant: str | None = None


class SignIn(SignUp):
    pass


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    groups: list[str] | None = None
    metadata: dict[str, Any] | None = None


class UserChange(BaseModel):
    groups: list[str] | None = None
    disabled: bool | None = None
    password: str | None = Field(None, max_length=200)


class AuthSettings(BaseModel):
    signup: bool | None = None
    token_ttl_s: int | None = Field(None, ge=60, le=2_592_000)


class Expose(BaseModel):
    exposed: bool


class NewPolicy(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    command: str = "ALL"
    using: str | None = Field(None, max_length=4000)
    check: str | None = Field(None, max_length=4000)


class NewBucket(BaseModel):
    name: str
    public: bool = False


class BucketChange(BaseModel):
    public: bool


class SavedQuery(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    pinned: bool | None = None  # shown on the Reports page; unset keeps what it was


class Search(BaseModel):
    vector: list[float] = Field(min_length=1)
    limit: int = Field(10, ge=1, le=1000)
    where: dict[str, Any] | None = None
    with_payload: bool = True


class Similar(BaseModel):
    like: str | int | None = None  # a point's id: its own vector is the query (any kind: dense, sparse, multivector)
    vector: list[Any] | None = None
    using: str = ""  # which named vector ("" for the collection's only vector)
    limit: int = Field(10, ge=1, le=100)
    where: dict[str, Any] | None = None


class Recall(BaseModel):
    samples: int = Field(50, ge=1, le=200)
    k: int = Field(10, ge=1, le=100)
    using: str = ""


class MapRequest(BaseModel):
    limit: int = Field(1000, ge=3, le=3000)
    using: str = ""
    color_by: str | None = Field(None, max_length=200)
    where: dict[str, Any] | None = None


class PayloadIndex(BaseModel):
    field: str = Field(min_length=1, max_length=200)
    type: str


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
    rerank: bool | None = None  # re-rank with the cross-encoder (default: SWSDB_RERANK)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)
    k: int = Field(5, ge=1, le=20)  # passages the answer may use
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    where: dict[str, Any] | None = None
    principals: list[str] | None = None  # who is asking (admin key only)
    graph_hops: int | None = Field(None, ge=0, le=2)
    graph_k: int = Field(3, ge=0, le=20)
    rerank: bool | None = None
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
    rerank: bool = False


class EvalRun(BaseModel):
    set: str
    configs: list[EvalConfig] | None = None  # default: hybrid, dense, keyword and hybrid+graph
    k: int = Field(5, ge=1, le=50)
    principals: list[str] | None = None


class Reconcile(BaseModel):
    dry_run: bool = True
    all_tenants: bool = False


class NewWebhook(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    events: list[str] = Field(default_factory=lambda: ["document.ready", "document.failed", "document.deleted"])
    collections: list[str] | None = None  # default: every collection of the tenant
    secret: str | None = Field(None, min_length=16, max_length=200)  # default: generated, returned once


class NewKey(BaseModel):
    role: Literal["admin", "read"] = "read"
    name: str | None = Field(None, max_length=200)
    tenant: str | None = None  # default: the request's tenant


class NewSnapshot(BaseModel):
    name: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")


class Fetch(BaseModel):
    key: str = Field(min_length=1, max_length=1024)


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
        shown = {"postgres": "PostgreSQL", "qdrant": "Qdrant", "falkordb": "FalkorDB", "pgvector": "pgvector",
                 "age": "Apache AGE", "neo4j": "Neo4j"}
        names = {c: f"{'SQL' if c == 'sql' else c} ({shown[self.s.product(c)]})" for c in ("sql", "vector", "graph")}
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
        if c.role == "user":
            raise HTTPException(403, "signed-in users reach /v1/rest, /v1/rpc, /v1/storage and document search and "
                                     "answers; this needs an API key")
        return c.role

    async def member(c: Caller = Depends(caller)) -> Caller:
        """A key, or a signed-in end user of the tenant."""
        return c

    def admin(c: Caller = Depends(caller)) -> str:
        if c.role == "user":
            raise HTTPException(403, "this needs an admin API key, not a user's sign-in")
        if c.role != "admin":
            raise HTTPException(403, "this needs the admin API key; the read-only key can only read and search")
        return c.role

    def tenant(request: Request, swsdb_tenant: str | None = Header(None), c: Caller = Depends(caller)) -> str:
        """The request's tenant: the `swsdb-tenant` header, else the key's tenant, else `default`. A tenant's key
        reaches only its own tenant."""
        try:
            t = check("tenant", swsdb_tenant or c.tenant or DEFAULT_TENANT)
        except NameError_ as e:
            raise HTTPException(422, str(e)) from None
        if c.tenant is not None and t != c.tenant:
            raise HTTPException(403, f"this key belongs to tenant {c.tenant!r} and can't reach tenant {t!r}")
        request.state.tenant = t  # for the request log
        return t

    def not_in_maintenance(request: Request) -> None:
        if request.app.state.maintenance:
            raise HTTPException(503, f"a {request.app.state.maintenance} is running; this write can be retried in a "
                                     "few seconds")

    async def writable(request: Request):
        """Writes that go straight to the vector or graph store are refused while a snapshot copies them, on this
        server or (through the shared fence) on any other."""
        not_in_maintenance(request)
        async with request.app.state.cluster.changing(wait=False):
            yield

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
            out["graph"] = {"nodes": await e.graph.count_nodes(key)}
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
            await e.app.state.cluster.notify("graphs")  # other servers drop what they remember of it
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

    @r.post("/collections/{name}/points/scroll", tags=["vectors"])
    async def browse_points(body: Browse, name: str = Depends(collection_name), t: str = Depends(tenant),
                            e: Engines = Depends(), role: str = Depends(require_key)):
        """Page through a collection's points in id order (the console's vector browser)."""
        await documents_guard(t, name, e, role, "browsing raw points")
        return await e.vector.browse(engine_name(t, name), body.limit, body.offset, body.where, body.with_vector)

    @r.post("/collections/{name}/search", tags=["vectors"])
    async def search(body: Search, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), role: str = Depends(require_key)):
        await documents_guard(t, name, e, role, "raw vector search")
        hits = await e.vector.search(engine_name(t, name), body.vector, body.limit, body.where, body.with_payload)
        return {"hits": hits}

    # -------------------------------------------------------------- a closer look (the console's vector views)

    async def raw_collection(t: str, name: str, e: Engines) -> None:
        d = e.app.state.docs
        is_docs = await d.is_document_collection(t, name) if d is not None else await e.vector.is_documents(
            engine_name(t, name))
        if is_docs:
            raise HTTPException(409, f"{name!r} is a documents collection: swsdb manages its indexes")

    @r.get("/collections/{name}/inspect", tags=["vectors"])
    async def inspect_collection(name: str = Depends(collection_name), t: str = Depends(tenant),
                                 e: Engines = Depends(), _: str = Depends(require_key)):
        """How a vector collection is set up: named, sparse and multi vectors, payload indexes, segments, status."""
        return await vector_views.inspect(e.vector, engine_name(t, name))

    @r.get("/collections/{name}/memory", tags=["vectors"])
    async def collection_memory(name: str = Depends(collection_name), t: str = Depends(tenant),
                                e: Engines = Depends(), _: str = Depends(require_key)):
        """Where the collection's bytes are, per vector, payload and payload index."""
        return await vector_views.memory(e.vector, engine_name(t, name))

    @r.get("/collections/{name}/segments", tags=["vectors"])
    async def collection_segments(name: str = Depends(collection_name), t: str = Depends(tenant),
                                  e: Engines = Depends(), _: str = Depends(require_key)):
        """The collection's segments and optimizations (Qdrant)."""
        return await vector_views.segments(e.vector, engine_name(t, name))

    @r.post("/collections/{name}/similar", tags=["vectors"])
    async def similar_points(body: Similar, name: str = Depends(collection_name), t: str = Depends(tenant),
                             e: Engines = Depends(), role: str = Depends(require_key)):
        """Nearest neighbours of a point (`like`: its id) or of a vector, on one named vector."""
        if (body.like is None) == (body.vector is None):
            raise HTTPException(422, "send either like (a point id) or vector")
        await documents_guard(t, name, e, role, "raw vector search")
        hits = await vector_views.similar(e.vector, engine_name(t, name), like=body.like, vector=body.vector,
                                          using=body.using, limit=body.limit, where=body.where)
        return {"hits": hits}

    @r.post("/collections/{name}/recall", tags=["vectors"])
    async def index_recall(body: Recall, name: str = Depends(collection_name), t: str = Depends(tenant),
                           e: Engines = Depends(), role: str = Depends(require_key)):
        """How good the approximate index is: recall@k of HNSW search against an exact scan, on random points."""
        await documents_guard(t, name, e, role, "measuring the index")
        return await vector_views.recall(e.vector, engine_name(t, name), body.samples, body.k, body.using)

    @r.post("/collections/{name}/map", tags=["vectors"])
    async def vector_map(body: MapRequest, name: str = Depends(collection_name), t: str = Depends(tenant),
                         e: Engines = Depends(), role: str = Depends(require_key)):
        """Up to `limit` points projected to 2D (PCA), each with a payload value to colour it by."""
        await documents_guard(t, name, e, role, "mapping raw points")
        return await vector_views.project(e.vector, engine_name(t, name), body.limit, body.using, body.color_by,
                                          body.where)

    @r.put("/collections/{name}/index", tags=["vectors"])
    async def create_payload_index(body: PayloadIndex, name: str = Depends(collection_name),
                                   t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin),
                                   __: None = Depends(writable)):
        """Index a payload field, so filters on it are fast (Qdrant). Not for documents collections."""
        await raw_collection(t, name, e)
        await vector_views.create_index(e.vector, engine_name(t, name), body.field, body.type)
        return {"indexed": body.field, "type": body.type}

    @r.delete("/collections/{name}/index/{field}", tags=["vectors"])
    async def drop_payload_index(field: str, name: str = Depends(collection_name), t: str = Depends(tenant),
                                 e: Engines = Depends(), _: str = Depends(admin), __: None = Depends(writable)):
        await raw_collection(t, name, e)
        await vector_views.drop_index(e.vector, engine_name(t, name), field)
        return {"dropped": field}

    # -------------------------------------------------------------- graph

    @r.post("/collections/{name}/cypher", tags=["graph"])
    async def cypher(body: Cypher, request: Request, name: str = Depends(collection_name),
                     t: str = Depends(tenant), e: Engines = Depends(), role: str = Depends(require_key)):
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        check_pass_through_cypher(body.query)
        key = engine_name(t, name)
        g = e.graph
        if not g.pass_through:
            refuse_pass_through()
        if not await g.exists(key):
            raise NotFound(f"collection {name!r} has no graph (create it with graph: true)")
        await documents_guard(t, name, e, role, "Cypher")
        if body.read_only:
            return await g.query(key, body.query, body.params, read_only=True, timeout_s=body.timeout_s)
        not_in_maintenance(request)
        async with request.app.state.cluster.changing(wait=False):
            return await g.query(key, body.query, body.params, read_only=False, timeout_s=body.timeout_s)

    @r.get("/collections/{name}/graph", tags=["graph"])
    async def graph_info(name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                         _: str = Depends(require_key)):
        """What the collection's graph holds: labels and relationship types with counts, property keys, which labels
        connect to which, and memory (FalkorDB)."""
        key = engine_name(t, name)
        if not await e.graph.exists(key):
            raise NotFound(f"collection {name!r} has no graph (create it with graph: true)")
        return await graph_views.info(e.graph, key)

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
    async def list_documents(limit: int = 100, offset: int = 0, status: str | None = None, q: str | None = None,
                             meta: str | None = None, sort: str = "created",
                             name: str = Depends(collection_name), t: str = Depends(tenant), e: Engines = Depends(),
                             _: str = Depends(admin)):
        """`q`: part of the id or title. `meta`: a JSON object of metadata fields to match, e.g. {"team": "Search"}.
        `sort`: created | updated | id | chunks."""
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1-1000 and offset >= 0")
        try:
            where = json.loads(meta) if meta else None
        except json.JSONDecodeError as err:
            raise HTTPException(422, f"meta must be a JSON object: {err}") from None
        if where is not None and not isinstance(where, dict):
            raise HTTPException(422, "meta must be a JSON object")
        return await e.docs.list(t, name, limit, offset, status, q=q, meta=where, sort=sort)

    @r.get("/collections/{name}/facets", tags=["documents"])
    async def document_facets(meta: str | None = None, name: str = Depends(collection_name), t: str = Depends(tenant),
                              e: Engines = Depends(), _: str = Depends(admin)):
        """Documents by status, metadata field and value, and day added (the console's collection summary). `meta`
        (a JSON object of metadata fields) counts only the documents that match it."""
        try:
            where = json.loads(meta) if meta else None
        except json.JSONDecodeError as err:
            raise HTTPException(422, f"meta must be a JSON object: {err}") from None
        if where is not None and not isinstance(where, dict):
            raise HTTPException(422, "meta must be a JSON object")
        return await e.docs.facets(t, name, meta=where)

    @r.get("/activity", tags=["admin"])
    async def activity(minutes: int = 60, c: Caller = Depends(caller), _: str = Depends(require_key)):
        """Searches and indexed documents per minute on this server over the last hour, and search latency. A
        tenant's key sees its own tenant; a key from the environment sees every tenant."""
        if not 1 <= minutes <= 60:
            raise HTTPException(422, "minutes must be 1-60")
        return observe.ACTIVITY.summary(c.tenant, minutes)

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
                    e: Engines = Depends(), c: Caller = Depends(member)):
        """Hybrid retrieval (dense + BM25, fused) with optional graph expansion, filtered by who is asking. A signed-in
        user sees the documents they may read (readers user:<id>, their groups and *)."""
        role = c.role
        if role == "user":
            if body.principals is not None:
                raise HTTPException(403, "a signed-in user searches as themself")
            body.principals, role = c.user.principals, "read"
        elif body.principals is not None and role != "admin":
            raise HTTPException(403, "only the admin key may act for principals; a read-only key sees public "
                                     "documents only")
        if body.graph_hops and not e.has("graph"):
            raise HTTPException(409, "graph_hops needs the graph engine")
        e.need("vector")
        return await e.docs.query(t, name, body.query, k=body.k, mode=body.mode, where=body.where,
                                  principals=body.principals, role=role, graph_hops=body.graph_hops,
                                  graph_k=body.graph_k, rerank=body.rerank)

    @r.post("/collections/{name}/answer", tags=["documents"])
    async def answer(body: Ask, request: Request, name: str = Depends(collection_name), t: str = Depends(tenant),
                     e: Engines = Depends(), c: Caller = Depends(member)):
        """A short answer that cites the passages it uses as [n], from what the caller may read only."""
        role = c.role
        if role == "user":
            if body.principals is not None:
                raise HTTPException(403, "a signed-in user asks as themself")
            body.principals, role = c.user.principals, "read"
        elif body.principals is not None and role != "admin":
            raise HTTPException(403, "only the admin key may act for principals")
        if body.graph_hops and not e.has("graph"):
            raise HTTPException(409, "graph_hops needs the graph engine")
        e.need("vector")
        t0 = time.perf_counter()
        found = await e.docs.query(t, name, body.question, k=body.k, mode=body.mode, where=body.where,
                                   principals=body.principals, role=role, graph_hops=body.graph_hops,
                                   graph_k=body.graph_k, rerank=body.rerank)
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

    @r.get("/collections/{name}/original/{doc_id:path}", tags=["documents"])
    async def document_original(doc_id: str, name: str = Depends(collection_name), t: str = Depends(tenant),
                                e: Engines = Depends(), _: str = Depends(admin)):
        """The file as it was uploaded (its live version, or the version being processed). Always a download:
        an uploaded HTML file is never shown as a page."""
        doc = await e.docs.one("SELECT * FROM swsdb.documents WHERE id = %s",
                               [doc_uuid(t, name, doc_id)])
        if doc is None:
            raise NotFound(f"document {doc_id!r} not found in {name!r}")
        data = await e.docs.original(doc, doc["live_version"] or doc["version"])
        if data is None:
            raise NotFound(f"the original file of {doc_id!r} is no longer kept")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doc["filename"] or f"{doc_id}{doc['kind']}")[:120] or "document"
        return Response(data, media_type="application/octet-stream",
                        headers={"content-disposition": f'attachment; filename="{safe}"',
                                 "x-content-type-options": "nosniff"})

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

    # -------------------------------------------------------------- scheduled backups

    @r.get("/admin/backups", tags=["snapshots"])
    async def backups(request: Request, _: Caller = Depends(global_admin)):
        """The schedule, the last run, and the copies in S3-compatible storage."""
        b = request.app.state.backups
        out = {"schedule": b.status(), "remote": [], "remote_error": None}
        try:
            out["remote"] = await b.remote()
        except Exception as err:  # storage unreachable: say so, keep the rest of the page working
            out["remote_error"] = str(err)
        return out

    @r.post("/admin/backups/run", tags=["snapshots"])
    async def run_backup(request: Request, _: Caller = Depends(global_admin)):
        """Take a backup now: a snapshot, uploaded when storage is configured, then old ones pruned."""
        try:
            return await request.app.state.backups.run()
        except StoreError:
            raise
        except Exception as err:
            raise Unavailable(f"backup failed: {err}") from err

    @r.post("/admin/backups/fetch", tags=["snapshots"])
    async def fetch_backup(body: Fetch, request: Request, _: Caller = Depends(global_admin)):
        """Copy a backup from S3 into the server's snapshots; restore it with /admin/snapshots/{id}/restore."""
        return await request.app.state.backups.fetch(body.key)

    # -------------------------------------------------------------- webhooks

    @r.post("/webhooks", tags=["webhooks"], status_code=201)
    async def create_webhook(body: NewWebhook, t: str = Depends(tenant), e: Engines = Depends(),
                             _: str = Depends(admin)):
        """Be told when documents become searchable, fail or are deleted. The secret signs every request and is
        shown only now."""
        for c in body.collections or []:
            check("collection", c)
        return await e.docs.sync.webhooks.create(t, body.url, body.events, body.collections, body.secret)

    @r.get("/webhooks", tags=["webhooks"])
    async def list_webhooks(t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        return {"webhooks": await e.docs.sync.webhooks.list(t)}

    @r.delete("/webhooks/{wid}", tags=["webhooks"])
    async def delete_webhook(wid: str, t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        if not await e.docs.sync.webhooks.delete(t, wid):
            raise NotFound(f"webhook {wid!r} not found")
        return {"deleted": wid}

    @r.get("/webhooks/{wid}/deliveries", tags=["webhooks"])
    async def webhook_deliveries(wid: str, limit: int = 50, t: str = Depends(tenant), e: Engines = Depends(),
                                 _: str = Depends(admin)):
        return {"deliveries": await e.docs.sync.webhooks.deliveries(t, wid, max(1, min(limit, 500)))}

    @r.post("/webhooks/{wid}/ping", tags=["webhooks"])
    async def ping_webhook(wid: str, t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        """Send a `ping` event, to check the receiving end."""
        return await e.docs.sync.webhooks.ping(t, wid)

    # -------------------------------------------------------------- tenants

    @r.get("/admin/tenants", tags=["admin"])
    async def tenants(e: Engines = Depends(), c: Caller = Depends(caller), _: str = Depends(admin)):
        """Every tenant that holds something (collections in any store, documents, SQL, keys), with counts."""
        if not c.is_global:
            raise HTTPException(403, "listing tenants needs a key from the environment")
        out: dict[str, dict] = {}

        def entry(t: str) -> dict:
            return out.setdefault(t, {"tenant": t, "collections": set(), "documents": 0, "keys": 0, "sql": False})
        names = []
        if e.has("vector"):
            names += await e.vector.all_names()
        if e.has("graph"):
            names += await e.graph.all_names()
        for n in names:
            if n.startswith("sw_") and "__" in n:
                t, coll = n[3:].split("__", 1)
                entry(t)["collections"].add(coll)
        if e.has("sql"):
            sql = e.sql
            for row in await sql.rows("SELECT tenant, count(*) AS n FROM swsdb.documents GROUP BY tenant"):
                entry(row["tenant"])["documents"] = row["n"]
            for row in await sql.rows("SELECT tenant, count(*) AS n FROM swsdb.api_keys WHERE revoked_at IS NULL "
                                      "GROUP BY tenant"):
                entry(row["tenant"])["keys"] = row["n"]
            for row in await sql.rows("SELECT tenant FROM swsdb.sql_tenants WHERE right(tenant, 3) <> '__u'"):
                entry(row["tenant"])["sql"] = True
        return {"tenants": [{**v, "collections": sorted(v["collections"])} for _, v in sorted(out.items())]}

    # -------------------------------------------------------------- managed API keys

    # -------------------------------------------------------------- saved queries, logs, advisors

    @r.get("/sql/saved", tags=["sql"])
    async def saved_queries(t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(require_key)):
        """SQL queries saved from the console for this tenant."""
        rows = await e.sql.rows("SELECT name, query, pinned, created_at, updated_at FROM swsdb.saved_queries "
                                "WHERE tenant = %s ORDER BY name", [t])
        return {"queries": [{**r, "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}
                            for r in rows]}

    @r.put("/sql/saved/{qname}", tags=["sql"])
    async def save_query(qname: str, body: SavedQuery, t: str = Depends(tenant), e: Engines = Depends(),
                         _: str = Depends(admin)):
        if not 1 <= len(qname) <= 120 or qname.strip() != qname:
            raise HTTPException(422, "a query name is 1-120 characters, without leading or trailing spaces")
        await e.sql.execute(
            "INSERT INTO swsdb.saved_queries (tenant, name, query, pinned) VALUES (%s, %s, %s, coalesce(%s, false)) "
            "ON CONFLICT (tenant, name) DO UPDATE SET query = EXCLUDED.query, updated_at = now(), "
            "pinned = coalesce(%s, swsdb.saved_queries.pinned)", [t, qname, body.query, body.pinned, body.pinned])
        return {"saved": qname}

    @r.delete("/sql/saved/{qname}", tags=["sql"])
    async def delete_query(qname: str, t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        rows = await e.sql.rows("DELETE FROM swsdb.saved_queries WHERE tenant = %s AND name = %s RETURNING name",
                                [t, qname])
        if not rows:
            raise NotFound(f"no saved query {qname!r}")
        return {"deleted": qname}

    @r.get("/logs", tags=["admin"])
    async def logs(limit: int = 200, status: str | None = None, route: str | None = None, min_ms: float = 0,
                   c: Caller = Depends(caller), _: str = Depends(admin)):
        """Recent API requests on this server (route, status, time, tenant, key id), newest first, with time per
        route. A tenant's key sees its own tenant's requests."""
        if not 1 <= limit <= 2000:
            raise HTTPException(422, "limit must be 1-2000")
        return observe.REQUESTS.query(c.tenant, limit=limit, status=status, route=route, min_ms=min_ms)

    @r.get("/advisors", tags=["admin"])
    async def advisors(request: Request, t: str = Depends(tenant), c: Caller = Depends(caller), e: Engines = Depends(),
                       _: str = Depends(admin)):
        """Checks for security and performance problems, each with what to do about it."""
        return await run_advisors(request.app, t, c)

    # -------------------------------------------------------------- end users (Auth)

    def auth_tenant(request: Request, body_tenant: str | None) -> str:
        try:
            return check("tenant", body_tenant or request.headers.get("swsdb-tenant") or DEFAULT_TENANT)
        except NameError_ as err:
            raise HTTPException(422, str(err)) from None

    def users_store(request: Request):
        if request.app.state.endusers is None:
            raise HTTPException(409, "end users are kept in PostgreSQL, which is not enabled")
        return request.app.state.endusers

    @r.post("/auth/signup", tags=["auth"], status_code=201)
    async def signup(body: SignUp, request: Request):
        """An app's end user signs up (when the tenant allows it, see /v1/auth/settings). No API key needed."""
        t = auth_tenant(request, body.tenant)
        users = users_store(request)
        if not (await users.config(t))["signup"]:
            raise HTTPException(403, "sign-up is closed for this tenant: an admin creates users")
        u = await users.create(t, body.email, body.password)
        return await users.token(t, {**u, "groups": u["groups"]})

    @r.post("/auth/token", tags=["auth"])
    async def sign_in(body: SignIn, request: Request):
        """Sign in with email and password: an access token for `Authorization: Bearer`. No API key needed."""
        t = auth_tenant(request, body.tenant)
        ip = request.client.host if request.client else "?"
        if SIGNIN_FAILS.blocked((t, body.email.lower(), ip)):
            raise HTTPException(429, "too many failed sign-ins: wait a few minutes")
        try:
            return await users_store(request).sign_in(t, body.email, body.password)
        except Forbidden:
            SIGNIN_FAILS.add((t, body.email.lower(), ip))
            raise

    @r.get("/auth/user", tags=["auth"])
    async def me(c: Caller = Depends(member)):
        """Who the access token belongs to."""
        if c.role != "user":
            raise HTTPException(400, "this is for an end user's access token")
        return {"id": c.user.id, "email": c.user.email, "groups": list(c.user.groups), "tenant": c.tenant,
                "principals": c.user.principals}

    @r.get("/auth/users", tags=["auth"])
    async def list_users(request: Request, limit: int = 200, offset: int = 0, q: str | None = None,
                         t: str = Depends(tenant), _: str = Depends(admin)):
        return await users_store(request).list(t, min(max(limit, 1), 1000), max(offset, 0), q)

    @r.post("/auth/users", tags=["auth"], status_code=201)
    async def create_user(body: NewUser, request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        return await users_store(request).create(t, body.email, body.password, body.groups, body.metadata)

    @r.patch("/auth/users/{uid}", tags=["auth"])
    async def update_user(uid: str, body: UserChange, request: Request, t: str = Depends(tenant),
                          _: str = Depends(admin)):
        return await users_store(request).update(t, uid, groups=body.groups, disabled=body.disabled,
                                                 password=body.password)

    @r.delete("/auth/users/{uid}", tags=["auth"])
    async def delete_user(uid: str, request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        await users_store(request).delete(t, uid)
        return {"deleted": uid}

    @r.get("/auth/settings", tags=["auth"])
    async def auth_settings(request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        c = await users_store(request).config(t)
        return {"signup": c["signup"], "token_ttl_s": c["token_ttl_s"]}

    @r.put("/auth/settings", tags=["auth"])
    async def put_auth_settings(body: AuthSettings, request: Request, t: str = Depends(tenant),
                                _: str = Depends(admin)):
        out = await users_store(request).settings(t, signup=body.signup, token_ttl_s=body.token_ttl_s)
        await request.app.state.cluster.notify("auth")
        return out

    @r.post("/auth/settings/rotate", tags=["auth"])
    async def rotate_secret(request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        """A new signing secret: every access token issued so far stops working at once, on every server."""
        await users_store(request).rotate(t)
        await request.app.state.cluster.notify("auth")
        return {"rotated": True}

    # -------------------------------------------------------------- Data API, RPC and row-level security

    def data(request: Request):
        if request.app.state.dataapi is None:
            raise HTTPException(409, "the Data API needs PostgreSQL")
        return request.app.state.dataapi

    @r.get("/rest/{table}", tags=["data api"])
    async def rest_select(table: str, request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        """Rows of a table: ?select=a,b&col=eq.value&order=col.desc&limit=100&offset=0&count=exact. Keys see every
        row; a signed-in user sees the rows the table's policies allow (and only tables exposed to users)."""
        return await data(request).select(t, c, table, dict(request.query_params))

    @r.post("/rest/{table}", tags=["data api"], status_code=201)
    async def rest_insert(table: str, request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        """Insert an object or a list of objects; returns the rows as stored."""
        return await data(request).insert(t, c, table, await json_body(request))

    @r.patch("/rest/{table}", tags=["data api"])
    async def rest_update(table: str, request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        """Change the rows the filters match (at least one filter)."""
        return await data(request).update(t, c, table, dict(request.query_params), await json_body(request))

    @r.delete("/rest/{table}", tags=["data api"])
    async def rest_delete(table: str, request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        """Delete the rows the filters match (at least one filter)."""
        return await data(request).delete(t, c, table, dict(request.query_params))

    @r.post("/rpc/{function}", tags=["data api"])
    async def rpc(function: str, request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        """Call one of the tenant's SQL functions with named arguments ({"arg": value}). A signed-in user may call
        only functions exposed to users."""
        body = await json_body(request, default={})
        return await data(request).rpc(t, c, function, body)

    @r.get("/policies", tags=["data api"])
    async def policies(request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        """Tables (row-level security, whether users may reach them, policies) and functions users may call."""
        return await data(request).policies(t)

    @r.put("/policies/tables/{table}", tags=["data api"])
    async def expose_table(table: str, body: Expose, request: Request, t: str = Depends(tenant),
                           _: str = Depends(admin)):
        await data(request).expose_table(t, table, body.exposed)
        return {"table": table, "exposed": body.exposed}

    @r.post("/policies/tables/{table}", tags=["data api"], status_code=201)
    async def create_policy(table: str, body: NewPolicy, request: Request, t: str = Depends(tenant),
                            _: str = Depends(admin)):
        await data(request).create_policy(t, table, body.name, body.command, body.using, body.check)
        return {"table": table, "policy": body.name}

    @r.delete("/policies/tables/{table}/{policy}", tags=["data api"])
    async def drop_policy(table: str, policy: str, request: Request, t: str = Depends(tenant),
                          _: str = Depends(admin)):
        await data(request).drop_policy(t, table, policy)
        return {"dropped": policy}

    @r.put("/policies/functions/{function}", tags=["data api"])
    async def expose_function(function: str, body: Expose, request: Request, t: str = Depends(tenant),
                              _: str = Depends(admin)):
        await data(request).expose_function(t, function, body.exposed)
        return {"function": function, "exposed": body.exposed}

    @r.get("/query-stats", tags=["admin"])
    async def query_stats_route(request: Request, limit: int = 50, order: str = "total", t: str = Depends(tenant),
                                c: Caller = Depends(caller), e: Engines = Depends(), _: str = Depends(admin)):
        """The SQL statements that took the most time (pg_stat_statements; values show as $1). A key from the
        environment sees every role, swsdb's own work included; a tenant's key sees its own roles."""
        roles = None if c.is_global else roles_of(t, await e.sql.tenant_role(t))
        return await query_stats(e.sql, roles, min(max(limit, 1), 500), order)

    @r.post("/query-stats/reset", tags=["admin"])
    async def query_stats_reset(c: Caller = Depends(caller), e: Engines = Depends(), _: str = Depends(admin)):
        if not c.is_global:
            raise HTTPException(403, "resetting covers every tenant: it needs a key from the environment")
        await e.sql.execute("SELECT pg_stat_statements_reset()")
        return {"reset": True}

    # -------------------------------------------------------------- project connections (Postgres)

    def pg_endpoint(request: Request, sql: SqlStore) -> tuple[str, int, bool]:
        """Host and port a project's code connects to, and whether this swsdb accepts such connections."""
        s = request.app.state.settings
        if sql.dsn:  # an external Postgres: its own address; reaching it is up to its network and pg_hba
            p = psycopg.conninfo.conninfo_to_dict(sql.dsn)
            return str(p.get("host") or "localhost"), int(p.get("port") or 5432), True
        host = s.postgres_public_host or request.url.hostname or "localhost"
        return host, s.postgres_public_port, s.postgres_access or s.expose_native

    def pg_url(user: str, password: str, host: str, port: int, db: str) -> str:
        h = f"[{host}]" if ":" in host else host
        return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{h}:{port}/{quote(db, safe='')}"

    @r.get("/postgres", tags=["projects"])
    async def postgres_info(request: Request, t: str = Depends(tenant), e: Engines = Depends(),
                            _: str = Depends(admin)):
        """The project's Postgres login: user, database, schema, whether a password was issued and open sessions."""
        host, port, enabled = pg_endpoint(request, e.sql)
        info = await e.sql.login_info(t)
        url = pg_url(info["user"], "PASSWORD", host, port, info["database"])
        return {**info, "host": host, "port": port, "enabled": enabled, "url": url,
                "prisma_url": f"{url}?schema={quote(info['schema'], safe='')}"}

    @r.post("/postgres/credentials", tags=["projects"], status_code=201)
    async def postgres_credentials(request: Request, t: str = Depends(tenant), e: Engines = Depends(),
                                   _: str = Depends(admin)):
        """A connection string for the project's own code (SQLAlchemy, Prisma, Django, psql...), with a new password,
        shown only this once. It works in the project's schema with the same rights as its SQL, nothing else."""
        host, port, enabled = pg_endpoint(request, e.sql)
        if not enabled:
            raise HTTPException(409, "project connections are off (SWSDB_POSTGRES_ACCESS=false)")
        out = await e.sql.issue_login(t)
        url = pg_url(out["user"], out["password"], host, port, out["database"])
        # Prisma works in schema "public" unless the URL names one; libpq tools refuse that parameter
        return {**out, "host": host, "port": port, "url": url,
                "prisma_url": f"{url}?schema={quote(out['schema'], safe='')}"}

    @r.delete("/postgres/credentials", tags=["projects"])
    async def postgres_revoke(t: str = Depends(tenant), e: Engines = Depends(), _: str = Depends(admin)):
        """Remove the project's login: its password stops working and open sessions end."""
        if not await e.sql.revoke_login(t):
            raise NotFound("this project has no Postgres login")
        return {"revoked": True}

    # -------------------------------------------------------------- storage

    def files(request: Request):
        if request.app.state.storage is None:
            raise HTTPException(409, "storage needs PostgreSQL")
        return request.app.state.storage

    @r.get("/storage/buckets", tags=["storage"])
    async def buckets(request: Request, t: str = Depends(tenant), c: Caller = Depends(member)):
        return {"buckets": await files(request).buckets(t)}

    @r.post("/storage/buckets", tags=["storage"], status_code=201)
    async def create_bucket(body: NewBucket, request: Request, t: str = Depends(tenant), _: str = Depends(admin)):
        return await files(request).create_bucket(t, body.name, body.public)

    @r.patch("/storage/buckets/{bucket}", tags=["storage"])
    async def update_bucket(bucket: str, body: BucketChange, request: Request, t: str = Depends(tenant),
                            _: str = Depends(admin)):
        return await files(request).update_bucket(t, bucket, body.public)

    @r.delete("/storage/buckets/{bucket}", tags=["storage"])
    async def delete_bucket(bucket: str, request: Request, force: bool = False, t: str = Depends(tenant),
                            _: str = Depends(admin)):
        return await files(request).delete_bucket(t, bucket, force)

    @r.get("/storage/{bucket}", tags=["storage"])
    async def list_files(bucket: str, request: Request, prefix: str = "", t: str = Depends(tenant),
                         c: Caller = Depends(member)):
        return await files(request).list(t, c, bucket, prefix)

    @r.put("/storage/{bucket}/{path:path}", tags=["storage"], status_code=201)
    async def put_file(bucket: str, path: str, request: Request, t: str = Depends(tenant),
                       c: Caller = Depends(member)):
        """Upload the request body as the file (Content-Type is kept). Replaces a file at the same path."""
        data_ = await request.body()
        return await files(request).put(t, c, bucket, path, data_, request.headers.get("content-type"))

    @r.get("/storage/{bucket}/{path:path}", tags=["storage"])
    async def get_file(bucket: str, path: str, request: Request, t: str = Depends(tenant),
                       c: Caller = Depends(member)):
        body, ctype, headers = await files(request).get(t, c, bucket, path)
        return Response(body, media_type=ctype, headers=headers)

    @r.delete("/storage/{bucket}/{path:path}", tags=["storage"])
    async def delete_file(bucket: str, path: str, request: Request, t: str = Depends(tenant),
                          c: Caller = Depends(member)):
        return await files(request).delete(t, c, bucket, path)

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
