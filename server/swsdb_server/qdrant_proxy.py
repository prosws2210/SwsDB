"""Qdrant's own REST API at /qdrant, for a project's code using the standard Qdrant client, limited to its tenant.

    QdrantClient(url="http://localhost:6464", prefix="qdrant", api_key="<the project's swsdb key>")

The client's `api-key` header is the swsdb key, so the tenant and role come from it. Collection names are the
project's own (`products`), stored in Qdrant as `sw_<tenant>__products`, so they show up as swsdb's raw vector
collections and are in snapshots. A project sees and reaches only its own collections:
- the path's collection name is rewritten, and so are `lookup_from` / `with_lookup` in bodies (they name a second
  collection); `init_from` (copy another collection) is refused;
- only collection endpoints answer: no cluster, aliases, snapshots or shards (a snapshot recovery would make Qdrant
  fetch any URL), and nothing global;
- the read-only key reads and searches; writes need the admin key;
- documents collections are swsdb's to manage: the admin key may read them (like /search, it skips document readers),
  nobody may write them here;
- end users' sign-in tokens get nothing.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response

from . import timing
from .auth import Caller
from .names import DEFAULT_TENANT, NameError_, check, engine_name, parse_engine_name

MAX_BODY = 64 * 1024 * 1024
READ_POINTS = {"search", "query", "recommend", "discover", "scroll", "count"}
CLOSED = {"snapshots", "shards", "cluster", "aliases"}


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def is_read(method: str, rest: list[str]) -> bool:
    """Whether a call on a collection only reads: GETs, and the POSTs that retrieve, search, count or facet."""
    if method == "GET":
        return True
    if method != "POST":
        return False
    if rest == ["facet"]:
        return True
    return bool(rest) and rest[0] == "points" and (len(rest) == 1 or rest[1] in READ_POINTS)


def rewrite_body(body: Any, tenant: str) -> Any:
    """Point the collection names a body may hold at the tenant's own collections. Payloads and vectors are data and
    left alone."""
    if isinstance(body, list):
        return [rewrite_body(x, tenant) for x in body]
    if not isinstance(body, dict):
        return body
    out = {}
    for k, v in body.items():
        if k == "init_from":
            raise Refused(403, "init_from (copying another collection) isn't available through swsdb")
        if k in ("payload", "vector", "vectors"):
            out[k] = v
        elif k == "with_lookup" and isinstance(v, str):
            out[k] = _name(v, tenant)
        elif k in ("lookup_from", "with_lookup") and isinstance(v, dict) and isinstance(v.get("collection"), str):
            out[k] = {**v, "collection": _name(v["collection"], tenant)}
        else:
            out[k] = rewrite_body(v, tenant)
    return out


def _name(collection: str, tenant: str) -> str:
    try:
        return engine_name(tenant, collection)
    except NameError_ as e:
        raise Refused(422, str(e)) from None


def router(caller) -> APIRouter:
    r = APIRouter(prefix="/qdrant", tags=["qdrant"])

    def tenant(c: Caller, header: str | None) -> str:
        try:
            t = check("tenant", header or c.tenant or DEFAULT_TENANT)
        except NameError_ as e:
            raise HTTPException(422, str(e)) from None
        if c.tenant is not None and t != c.tenant:
            raise HTTPException(403, f"this key belongs to tenant {c.tenant!r} and can't reach tenant {t!r}")
        return t

    def store(request: Request):
        s = request.app.state.settings
        v = request.app.state.vector
        if "vector" not in s.engines or v is None:
            raise HTTPException(409, "the vector engine is not enabled in this swsdb")
        if getattr(v, "engine", None) != "qdrant":
            raise HTTPException(409, "this swsdb keeps vectors in Postgres (the lite profile): there is no Qdrant API; "
                                     "use /v1/collections/{name}/points and /search")
        return v

    async def forward(request: Request, v, method: str, path: str, body: bytes | None, prefix: str | None) -> Response:
        try:
            with timing.stage("qdrant"):
                resp = await v.http.request(method, path, params=list(request.query_params.multi_items()),
                                            content=body, timeout=60.0,
                                            headers={"content-type": "application/json"} if body else None)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Qdrant: {type(e).__name__}") from None
        content = resp.content
        if resp.status_code >= 400 and prefix:  # error messages name the collection: show the project's name
            content = content.replace(prefix.encode(), b"")
        return Response(content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    @r.get("")
    @r.get("/")
    async def root(request: Request, c: Caller = Depends(caller)):
        """Qdrant's version (the client checks it when it connects)."""
        if c.role == "user":
            raise HTTPException(403, "the Qdrant API needs an API key, not a user's sign-in")
        return await forward(request, store(request), "GET", "/", None, None)

    @r.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request, c: Caller = Depends(caller),
                    swsdb_tenant: str | None = Header(None)):
        if c.role == "user":
            raise HTTPException(403, "the Qdrant API needs an API key, not a user's sign-in")
        t = tenant(c, swsdb_tenant)
        request.state.tenant = t
        v = store(request)
        parts = [p for p in path.split("/") if p]
        method = request.method
        if not parts or parts[0] != "collections":
            raise HTTPException(403, "through swsdb, Qdrant's API covers collections and their points only")
        prefix = f"sw_{t}__"
        if len(parts) == 1:
            if method != "GET":
                raise HTTPException(405, "list collections with GET")
            names = sorted(n for n in await v.all_names() if parse_engine_name(n, t))
            return {"result": {"collections": [{"name": n[len(prefix):]} for n in names]}, "status": "ok", "time": 0}
        name, rest = parts[1], parts[2:]
        if name in CLOSED or (rest and rest[0] in CLOSED):
            raise HTTPException(403, f"{(rest or [name])[0]} isn't available through swsdb")
        try:
            internal = engine_name(t, name)
        except NameError_ as e:
            raise HTTPException(422, str(e)) from None
        read = is_read(method, rest)
        if not read and c.role != "admin":
            raise HTTPException(403, "this needs the admin API key; the read-only key can only read and search")
        d = request.app.state.docs
        is_docs = await d.is_document_collection(t, name) if d is not None else await v.is_documents(internal)
        if is_docs:
            if not read:
                raise HTTPException(403, f"{name!r} is a documents collection: swsdb keeps its vectors in step with "
                                         "its documents, so change it through /v1/collections/{name}/documents")
            if c.role != "admin":
                raise HTTPException(403, "reading a documents collection directly skips document readers: it needs "
                                         "the admin key; use /v1/collections/{name}/query, which applies them")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, f"the body is larger than {MAX_BODY // (1024 * 1024)} MB")
        if body:
            try:
                body = json.dumps(rewrite_body(json.loads(body), t)).encode()
            except json.JSONDecodeError as e:
                raise HTTPException(422, f"the body is not JSON: {e}") from None
            except Refused as e:
                raise HTTPException(e.status, e.message) from None
        target = "/".join(["collections", internal, *rest])
        if read:
            return await forward(request, v, method, f"/{target}", body or None, prefix)
        if request.app.state.maintenance:
            raise HTTPException(503, f"a {request.app.state.maintenance} is running; retry in a few seconds")
        async with request.app.state.cluster.changing(wait=False):
            return await forward(request, v, method, f"/{target}", body or None, prefix)

    return r
