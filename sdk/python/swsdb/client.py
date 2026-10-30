from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:6464"


class SwsDBError(Exception):
    """An error answered by swsdb. `status` is the HTTP status, `code` swsdb's error code (e.g. "not_found")."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class SwsDB:
    """A connection to one swsdb server, acting for one tenant.

    url, api_key and tenant default to SWSDB_URL, SWSDB_API_KEY and SWSDB_TENANT.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, tenant: str | None = None,
                 timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self.url = (url or os.environ.get("SWSDB_URL") or DEFAULT_URL).rstrip("/")
        self.tenant = tenant or os.environ.get("SWSDB_TENANT") or "default"
        key = api_key if api_key is not None else os.environ.get("SWSDB_API_KEY")
        headers = {"swsdb-tenant": self.tenant, "user-agent": "swsdb-python"}
        if key:
            headers["api-key"] = key
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout, transport=transport)

    # -- plumbing

    def _call(self, method: str, path: str, json: Any = None) -> Any:
        try:
            r = self._http.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise SwsDBError(0, "unreachable", f"can't reach swsdb at {self.url}: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
                raise SwsDBError(r.status_code, body.get("error", "error"), body.get("message", r.text))
            except ValueError:
                raise SwsDBError(r.status_code, "error", r.text[:500]) from None
        return r.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SwsDB:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- server

    def ready(self) -> bool:
        try:
            return self._http.get("/readyz").status_code == 200
        except httpx.HTTPError:
            return False

    def status(self) -> dict:
        return self._call("GET", "/v1/status")

    # -- collections

    def collections(self) -> list[dict]:
        return self._call("GET", "/v1/collections")["collections"]

    def collection(self, name: str) -> Collection:
        """A handle to an existing collection (no request is made)."""
        return Collection(self, name)

    def create_collection(self, name: str, vector_size: int | None = None, distance: str = "cosine",
                          graph: bool | None = None, documents: bool = False) -> Collection:
        """`documents=True`: swsdb chunks, embeds (dense + BM25) and graphs what you add. `vector_size`: raw vectors
        you embed yourself. `graph`: also a graph (default: yes when the server has the graph engine)."""
        body: dict[str, Any] = {"graph": graph, "documents": documents}
        if vector_size is not None:
            body["vector"] = {"size": vector_size, "distance": distance}
        self._call("PUT", f"/v1/collections/{name}", body)
        return Collection(self, name)

    def delete_collection(self, name: str) -> dict:
        return self._call("DELETE", f"/v1/collections/{name}")

    # -- consistency

    def reconcile(self, repair: bool = False, all_tenants: bool = False) -> dict:
        """Compare the catalog with the vector and graph stores. With repair=True, queue the fixes."""
        return self._call("POST", "/v1/admin/reconcile", {"dry_run": not repair, "all_tenants": all_tenants})

    # -- SQL

    def sql(self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None, *, read_only: bool = False,
            timeout_s: float = 30, max_rows: int = 1000) -> dict:
        """One SQL statement. Returns {"columns", "rows", "row_count", "truncated"}. Use %s / %(name)s placeholders."""
        return self._call("POST", "/v1/sql", {"query": query, "params": params, "read_only": read_only,
                                              "timeout_s": timeout_s, "max_rows": max_rows})


class Collection:
    def __init__(self, db: SwsDB, name: str):
        self.db, self.name = db, name

    def __repr__(self) -> str:
        return f"Collection({self.name!r}, tenant={self.db.tenant!r})"

    def info(self) -> dict:
        return self.db._call("GET", f"/v1/collections/{self.name}")

    def drop(self) -> dict:
        return self.db.delete_collection(self.name)

    # -- vectors

    def upsert(self, points: Iterable[Mapping[str, Any]], batch_size: int = 500) -> int:
        """Insert or replace points: {"id": str|int, "vector": [...], "payload": {...}}. Batched for you."""
        total, batch = 0, []
        for p in points:
            batch.append(dict(p))
            if len(batch) >= batch_size:
                total += self.db._call("PUT", f"/v1/collections/{self.name}/points", {"points": batch})["upserted"]
                batch = []
        if batch:
            total += self.db._call("PUT", f"/v1/collections/{self.name}/points", {"points": batch})["upserted"]
        return total

    def search(self, vector: Sequence[float], limit: int = 10, where: Mapping[str, Any] | None = None,
               with_payload: bool = True) -> list[dict]:
        """Nearest points: [{"id", "score", "payload"}]. `where` matches payload fields exactly (lists: any of)."""
        return self.db._call("POST", f"/v1/collections/{self.name}/search",
                             {"vector": list(vector), "limit": limit, "where": dict(where) if where else None,
                              "with_payload": with_payload})["hits"]

    def get(self, ids: Sequence[str | int]) -> list[dict]:
        return self.db._call("POST", f"/v1/collections/{self.name}/points/get", {"ids": list(ids)})["points"]

    def delete(self, ids: Sequence[str | int]) -> int:
        return self.db._call("POST", f"/v1/collections/{self.name}/points/delete", {"ids": list(ids)})["deleted"]

    # -- graph

    def cypher(self, query: str, params: Mapping[str, Any] | None = None, *, read_only: bool = False,
               timeout_s: float = 30) -> dict:
        """A Cypher query on this collection's graph. Pass values as params ($name), never by formatting strings."""
        return self.db._call("POST", f"/v1/collections/{self.name}/cypher",
                             {"query": query, "params": dict(params) if params else None, "read_only": read_only,
                              "timeout_s": timeout_s})

    # -- documents (collections created with documents=True)

    def add_text(self, id: str, text: str, *, title: str | None = None, metadata: Mapping[str, Any] | None = None,
                 readers: Sequence[str] | None = None, format: str = "markdown") -> dict:
        """Queue a document given as text ("markdown", "text" or "html"). Returns its status (usually "queued")."""
        body = {"id": id, "text": text, "title": title, "format": format,
                "metadata": dict(metadata) if metadata else None, "readers": list(readers) if readers else None}
        return self.db._call("POST", f"/v1/collections/{self.name}/documents/text", body)

    def upload(self, path: str | Path, *, id: str | None = None, title: str | None = None,
               metadata: Mapping[str, Any] | None = None, readers: Sequence[str] | None = None) -> dict:
        """Queue a file (PDF, Markdown, HTML, text). The document id defaults to the file name."""
        p = Path(path)
        data: dict[str, str] = {"id": id or p.name}
        if title:
            data["title"] = title
        if metadata:
            data["metadata"] = json.dumps(dict(metadata))
        if readers:
            data["readers"] = json.dumps(list(readers))
        with p.open("rb") as f:
            r = self.db._http.post(f"/v1/collections/{self.name}/documents", files={"file": (p.name, f)}, data=data)
        if r.status_code >= 400:
            body = r.json()
            raise SwsDBError(r.status_code, body.get("error", "error"), body.get("message", r.text))
        return r.json()

    def document(self, id: str) -> dict:
        return self.db._call("GET", f"/v1/collections/{self.name}/documents/{id}")

    def documents(self, limit: int = 100, offset: int = 0, status: str | None = None) -> dict:
        q = f"?limit={limit}&offset={offset}" + (f"&status={status}" if status else "")
        return self.db._call("GET", f"/v1/collections/{self.name}/documents{q}")

    def delete_document(self, id: str) -> dict:
        return self.db._call("DELETE", f"/v1/collections/{self.name}/documents/{id}")

    def wait_ready(self, id: str, timeout: float = 600, poll: float = 0.5) -> dict:
        """Block until a document is searchable. Raises SwsDBError if its processing failed."""
        deadline = time.monotonic() + timeout
        while True:
            d = self.document(id)
            if d["status"] == "ready":
                return d
            if d["status"] == "failed":
                raise SwsDBError(422, "document_failed", f"{id}: {d['error']}")
            if time.monotonic() > deadline:
                raise SwsDBError(408, "timeout", f"{id} still {d['status']} after {timeout}s")
            time.sleep(poll)

    def query(self, query: str, k: int = 10, *, mode: str = "hybrid", where: Mapping[str, Any] | None = None,
              principals: Sequence[str] | None = None, graph_hops: int | None = None, graph_k: int = 5) -> list[dict]:
        """Hybrid retrieval over the documents: the top `k` hits, then up to `graph_k` connected chunks
        (via="graph", with graph_path). `principals` (admin key only) is who is asking, e.g. ["user:42"]."""
        body = {"query": query, "k": k, "mode": mode, "where": dict(where) if where else None,
                "principals": list(principals) if principals is not None else None, "graph_hops": graph_hops,
                "graph_k": graph_k}
        return self.db._call("POST", f"/v1/collections/{self.name}/query", body)["hits"]

    def diagnose(self, id: str) -> dict:
        """Why a document is or isn't searchable: catalog, pending changes, vectors and graph, with a verdict."""
        return self.db._call("GET", f"/v1/collections/{self.name}/diagnose/{id}")

    def put_eval_set(self, name: str, cases: Sequence[Mapping[str, Any]]) -> dict:
        """A golden set: [{"query": ..., "relevant": [document ids], "answer": optional phrase}]."""
        body = {"cases": [dict(c) for c in cases]}
        return self.db._call("PUT", f"/v1/collections/{self.name}/eval/sets/{name}", body)

    def evaluate(self, set_name: str, k: int = 5, configs: Sequence[Mapping[str, Any]] | None = None) -> dict:
        """Ask every question under each configuration (default: hybrid, dense, keyword, hybrid+graph)."""
        body: dict[str, Any] = {"set": set_name, "k": k}
        if configs:
            body["configs"] = [dict(c) for c in configs]
        return self.db._call("POST", f"/v1/collections/{self.name}/eval/runs", body)
