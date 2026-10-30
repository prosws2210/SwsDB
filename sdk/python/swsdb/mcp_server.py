"""swsdb as an MCP server: search, cited answers, documents, SQL and graph for agents, plus long-term memory.

Every tool calls the swsdb REST API with the caller's key, so an agent gets exactly the tenant, permissions and
read-only limits of that key: nothing more.

    swsdb mcp                  # stdio, read-only tools (SWSDB_URL, SWSDB_API_KEY, SWSDB_TENANT)
    swsdb mcp --read-write     # also add and delete documents, remember and forget

The swsdb server serves the same tools over streamable HTTP: /mcp (read-only) and /mcp/rw (read-write, admin key).
Needs the `mcp` package: pip install "swsdb[mcp]".
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import SwsDB, SwsDBError

INSTRUCTIONS = """swsdb holds this user's documents, searchable by meaning and keywords, with a knowledge graph.
Use `ask` for a direct question (it answers with [n] citations), `search` to read the passages yourself, and
`get_document` to read a whole document. Cite the document ids you rely on. `recall` searches what was remembered
earlier; with write access, `remember` stores a fact for later conversations."""

MEMORY = "memory"


def _fail(e: SwsDBError) -> ToolError:
    hint = {401: " (the API key is missing or wrong)", 403: " (this key may not do that)",
            404: " (check the collection or document name with list_collections / search)"}.get(e.status, "")
    return ToolError(f"swsdb {e.status} {e.code}: {e.message}{hint}")


def _cite(hits: list[dict]) -> str:
    if not hits:
        return "No passages found."
    out = []
    for n, h in enumerate(hits, 1):
        where = ", ".join(x for x in (f"document {h['document']}", h.get("title"),
                                      f"page {h['page']}" if h.get("page") is not None else None,
                                      h.get("heading")) if x)
        via = f" · found via the graph: {' → '.join(h['graph_path'])}" if h.get("graph_path") else ""
        out.append(f"[{n}] {where} (score {h['score']:.3f}{via})\n{(h.get('text') or '').strip()}")
    return "\n\n".join(out)


def build(client: Callable[[Context | None], SwsDB], *, read_write: bool, name: str = "swsdb",
          per_request: bool = False, **settings: Any) -> FastMCP:
    """The MCP server. `client(ctx)` gives the SwsDB to call for a request: one fixed connection (stdio), or one per
    request built from the caller's headers (HTTP, per_request=True: it is closed afterwards)."""
    mcp = FastMCP(name, instructions=INSTRUCTIONS, **settings)

    async def call(ctx: Context | None, fn: Callable[[SwsDB], Any]) -> Any:
        """The SDK is synchronous: run it in a worker thread so the event loop (maybe this very server's) stays free."""
        db = client(ctx)
        try:
            return await anyio.to_thread.run_sync(fn, db)
        except SwsDBError as e:
            raise _fail(e) from None
        finally:
            if per_request:
                db.close()

    # ------------------------------------------------------------------ read

    @mcp.tool()
    async def list_collections(ctx: Context) -> list[dict]:
        """The collections this key can see, with how many documents each holds."""
        def run(db: SwsDB) -> list[dict]:
            out = []
            for c in db.collections():
                info = db.collection(c["name"]).info()
                docs = info.get("documents")
                out.append({"name": c["name"], "documents": docs["count"] if docs else None,
                            "kind": "documents" if docs else "raw vectors/graph", "graph": bool(c["graph"])})
            return out
        return await call(ctx, run)

    @mcp.tool()
    async def search(collection: str, query: str, ctx: Context, k: int = 5, mode: str = "hybrid",
               graph_hops: int | None = None, where: dict[str, Any] | None = None) -> str:
        """Search a documents collection. mode: hybrid (default), dense (meaning) or sparse (keywords).
        graph_hops 1-2 adds passages connected through shared entities. where filters on metadata fields.
        Returns numbered passages with their document id, title and page."""
        return await call(ctx, lambda db: _cite(db.collection(collection).query(
            query, k=k, mode=mode, graph_hops=graph_hops, where=where)))

    @mcp.tool()
    async def ask(collection: str, question: str, ctx: Context, k: int = 5) -> str:
        """Answer a question from a documents collection, citing passages as [n]. The sources follow the answer."""
        def run(db: SwsDB) -> str:
            r = db.collection(collection).answer(question, k=k)
            sources = [p for p in r["passages"] if p["cited"]]
            lines = [r["answer"], "", "Sources:"] + [
                f"[{p['n']}] document {p['document']}" + (f" ({p['title']})" if p.get("title") else "")
                + (f", page {p['page']}" if p.get("page") is not None else "") for p in sources]
            return "\n".join(lines if sources else [r["answer"]])
        return await call(ctx, run)

    @mcp.tool()
    async def get_document(collection: str, id: str, ctx: Context) -> dict:
        """The full text of one document (as it is searched), if this key may read it."""
        def run(db: SwsDB) -> dict:
            return {k: v for k, v in db.collection(collection).text(id).items() if k != "chunks"}
        return await call(ctx, run)

    @mcp.tool()
    async def list_documents(collection: str, ctx: Context, limit: int = 50, status: str | None = None) -> dict:
        """Documents in a collection with their status (needs an admin key)."""
        def run(db: SwsDB) -> dict:
            r = db.collection(collection).documents(limit=min(limit, 500), status=status)
            keep = ("id", "title", "status", "version", "chunks", "updated_at", "readers")
            return {"total": r["total"], "documents": [{k: d.get(k) for k in keep} for d in r["documents"]]}
        return await call(ctx, run)

    @mcp.tool()
    async def sql_query(query: str, ctx: Context, max_rows: int = 200) -> dict:
        """Run one read-only SQL statement in this tenant's own schema (PostgreSQL)."""
        return await call(ctx, lambda db: db.sql(query, read_only=True, max_rows=min(max_rows, 1000)))

    @mcp.tool()
    async def graph_query(collection: str, query: str, ctx: Context) -> dict:
        """Run one read-only Cypher query on a collection's graph (FalkorDB). Documents collections hold
        (:Document)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(:Entity)-[:RELATED]-(:Entity); raw graphs hold your own."""
        return await call(ctx, lambda db: db.collection(collection).cypher(query, read_only=True))

    @mcp.tool()
    async def recall(query: str, ctx: Context, k: int = 5, tags: list[str] | None = None,
               collection: str = MEMORY) -> str:
        """Search what was remembered earlier (most relevant first). tags narrows to memories with any of them."""
        def run(db: SwsDB) -> str:
            try:
                hits = db.collection(collection).query(query, k=k, graph_hops=0,
                                                       where={"tags": tags} if tags else None)
            except SwsDBError as e:
                if e.status == 404:
                    return "Nothing has been remembered yet."
                raise
            if not hits:
                return "Nothing relevant remembered."
            return "\n".join(f"- ({h['document']}) {(h.get('text') or '').strip()}" for h in hits)
        return await call(ctx, run)

    if not read_write:
        return mcp

    # ------------------------------------------------------------------ write

    def ensure(db: SwsDB, collection: str) -> None:
        try:
            db.collection(collection).info()
        except SwsDBError as e:
            if e.status != 404:
                raise
            db.create_collection(collection, documents=True)

    @mcp.tool()
    async def add_document(collection: str, id: str, text: str, ctx: Context, title: str | None = None,
                     metadata: dict[str, Any] | None = None, readers: list[str] | None = None,
                     wait: bool = True) -> dict:
        """Add or replace a document (Markdown or plain text). The collection is created if needed. readers limits
        who may find it (e.g. ["group:hr"]); by default everyone in the tenant. wait: return once searchable."""
        def run(db: SwsDB) -> dict:
            ensure(db, collection)
            col = db.collection(collection)
            r = col.add_text(id, text, title=title, metadata=metadata, readers=readers)
            if wait and not r.get("unchanged"):
                r = col.wait_ready(id, timeout=120)
            return {k: r.get(k) for k in ("id", "status", "version", "chunks")}
        return await call(ctx, run)

    @mcp.tool()
    async def delete_document(collection: str, id: str, ctx: Context) -> dict:
        """Delete a document from every store. Searches stop returning it at once."""
        return await call(ctx, lambda db: db.collection(collection).delete_document(id))

    @mcp.tool()
    async def remember(text: str, ctx: Context, tags: list[str] | None = None, collection: str = MEMORY) -> dict:
        """Store a fact or note for later conversations; `recall` finds it by meaning. Returns its id."""
        mid = f"m-{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
        def run(db: SwsDB) -> dict:
            ensure(db, collection)
            col = db.collection(collection)
            col.add_text(mid, text, metadata={"tags": tags} if tags else None, format="text")
            col.wait_ready(mid, timeout=120)
            return {"id": mid, "remembered": True}
        return await call(ctx, run)

    @mcp.tool()
    async def forget(id: str, ctx: Context, collection: str = MEMORY) -> dict:
        """Delete a memory by the id `remember` or `recall` gave."""
        return await call(ctx, lambda db: db.collection(collection).delete_document(id))

    return mcp


def serve(read_write: bool = False, http_port: int | None = None, host: str = "127.0.0.1",
          url: str | None = None, api_key: str | None = None, tenant: str | None = None,
          verify: bool | str = True) -> None:
    """Run the MCP server for one swsdb connection: over stdio, or over HTTP on http_port."""
    db = SwsDB(url, api_key, tenant, timeout=180, verify=verify)
    settings: dict[str, Any] = {}
    if http_port:
        settings.update(host=host, port=http_port, stateless_http=True, json_response=True)
    mcp = build(lambda _ctx: db, read_write=read_write, **settings)
    mcp.run("streamable-http" if http_port else "stdio")
