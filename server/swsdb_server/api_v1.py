"""REST API v1: collections, vectors, graph and SQL."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .names import DEFAULT_TENANT, NameError_, check, engine_name
from .stores import GraphStore, NotFound, SqlStore, VectorSpec, VectorStore

MAX_POINTS = 1000


# ------------------------------------------------------------------ request bodies


class VectorConfig(BaseModel):
    size: int = Field(ge=1, le=65536)
    distance: Literal["cosine", "dot", "euclid", "manhattan"] = "cosine"


class CreateCollection(BaseModel):
    vector: VectorConfig | None = None
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
        if st is None or st.state != "ready":
            raise HTTPException(503, f"the {names[cap]} engine is {st.state if st else 'unknown'}"
                                     + (f": {st.error}" if st and st.error else ""))
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
        if body.vector is None and not want_graph:
            raise HTTPException(422, "a collection needs vectors (`vector: {size, distance}`), a graph, or both")
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

    # -------------------------------------------------------------- SQL

    @r.post("/sql", tags=["sql"])
    async def sql(body: Sql, e: Engines = Depends(), role: str = Depends(require_key)):
        if role != "admin" and not body.read_only:
            raise HTTPException(403, "the read-only key can only run read_only queries")
        return await e.sql.query(body.query, body.params, read_only=body.read_only, timeout_s=body.timeout_s,
                                 max_rows=body.max_rows)

    return r
