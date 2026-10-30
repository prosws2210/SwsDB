from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_URL = "http://localhost:6464"


def _seg(value: str) -> str:
    """One URL path segment: ids may hold '/', '?', '#' or '..'."""
    return quote(str(value), safe="")


class SwsDBError(Exception):
    """An error answered by swsdb. `status` is the HTTP status, `code` swsdb's error code (e.g. "not_found")."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class SwsDB:
    """A connection to one swsdb server, acting for one tenant.

    url, api_key and tenant default to SWSDB_URL, SWSDB_API_KEY and SWSDB_TENANT. Without a tenant, a tenant's key
    acts for its own tenant and other keys for "default".
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, tenant: str | None = None,
                 timeout: float = 60.0, transport: httpx.BaseTransport | None = None, verify: bool | str = True):
        self.url = (url or os.environ.get("SWSDB_URL") or DEFAULT_URL).rstrip("/")
        # None: the key's own tenant for a tenant's key, else "default" (the server decides)
        self.tenant = tenant or os.environ.get("SWSDB_TENANT") or None
        key = api_key if api_key is not None else os.environ.get("SWSDB_API_KEY")
        headers = {"user-agent": "swsdb-python"}
        if self.tenant:
            headers["swsdb-tenant"] = self.tenant
        if key:
            headers["api-key"] = key
        # verify: False skips TLS certificate checks (a self-signed certificate on a test server); a path is a CA file
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout, transport=transport,
                                  verify=verify)

    # -- plumbing

    def _call(self, method: str, path: str, json: Any = None, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, json=json, **kw)
        except httpx.HTTPError as e:
            raise SwsDBError(0, "unreachable", f"can't reach swsdb at {self.url}: {e}") from e
        return self._result(r)

    @staticmethod
    def _result(r: httpx.Response) -> Any:
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
        self._call("PUT", f"/v1/collections/{_seg(name)}", body)
        return Collection(self, name)

    def delete_collection(self, name: str) -> dict:
        return self._call("DELETE", f"/v1/collections/{_seg(name)}")

    # -- consistency

    def reconcile(self, repair: bool = False, all_tenants: bool = False) -> dict:
        """Compare the catalog with the vector and graph stores. With repair=True, queue the fixes."""
        return self._call("POST", "/v1/admin/reconcile", {"dry_run": not repair, "all_tenants": all_tenants})

    # -- snapshots (they cover every tenant: the admin key from the server's environment)

    def create_snapshot(self, name: str | None = None) -> dict:
        """One consistent snapshot of every engine, kept on the server's volume. Returns its manifest."""
        return self._call("POST", "/v1/admin/snapshots", {"name": name}, timeout=None)

    def snapshots(self) -> list[dict]:
        return self._call("GET", "/v1/admin/snapshots")["snapshots"]

    def download_snapshot(self, snapshot_id: str, path: str | Path) -> Path:
        path = Path(path)
        tmp = path.with_name(path.name + ".part")
        try:
            with self._http.stream("GET", f"/v1/admin/snapshots/{_seg(snapshot_id)}/download", timeout=None) as r:
                if r.status_code >= 400:
                    r.read()
                    self._result(r)
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            tmp.replace(path)
        except httpx.HTTPError as e:
            raise SwsDBError(0, "unreachable", f"can't reach swsdb at {self.url}: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)
        return path

    def upload_snapshot(self, path: str | Path, snapshot_id: str | None = None) -> dict:
        """Send a snapshot file to the server (e.g. to restore it on another machine). Returns its manifest."""
        path = Path(path)
        sid = snapshot_id or path.name.removesuffix(".tar")
        with path.open("rb") as f:
            return self._call("PUT", f"/v1/admin/snapshots/{_seg(sid)}", content=f, timeout=None,
                              headers={"content-type": "application/x-tar"})

    def restore_snapshot(self, snapshot_id: str, backup_first: bool = True) -> dict:
        """Replace all data with the snapshot's. With backup_first, the current data is snapshotted before."""
        return self._call("POST", f"/v1/admin/snapshots/{_seg(snapshot_id)}/restore",
                          {"backup_first": backup_first}, timeout=None)

    def delete_snapshot(self, snapshot_id: str) -> dict:
        return self._call("DELETE", f"/v1/admin/snapshots/{_seg(snapshot_id)}")

    # -- webhooks (document.ready, document.failed, document.deleted)

    def create_webhook(self, url: str, events: Sequence[str] | None = None,
                       collections: Sequence[str] | None = None, secret: str | None = None) -> dict:
        """Register a URL to be POSTed signed events. The result's "secret" (for checking signatures) is shown once."""
        body: dict[str, Any] = {"url": url, "collections": list(collections) if collections else None,
                                "secret": secret}
        if events:
            body["events"] = list(events)
        return self._call("POST", "/v1/webhooks", body)

    def webhooks(self) -> list[dict]:
        return self._call("GET", "/v1/webhooks")["webhooks"]

    def webhook_deliveries(self, webhook_id: str, limit: int = 50) -> list[dict]:
        return self._call("GET", f"/v1/webhooks/{_seg(webhook_id)}/deliveries?limit={limit}")["deliveries"]

    def ping_webhook(self, webhook_id: str) -> dict:
        return self._call("POST", f"/v1/webhooks/{_seg(webhook_id)}/ping")

    def delete_webhook(self, webhook_id: str) -> dict:
        return self._call("DELETE", f"/v1/webhooks/{_seg(webhook_id)}")

    def tenants(self) -> list[dict]:
        """Every tenant holding something, with its collections and counts (a key from the environment)."""
        return self._call("GET", "/v1/admin/tenants")["tenants"]

    # -- API keys for one tenant

    def create_key(self, role: str = "read", name: str | None = None, tenant: str | None = None) -> dict:
        """A new key for `tenant` (default: this client's tenant). The result's "key" is shown only this once."""
        return self._call("POST", "/v1/keys", {"role": role, "name": name, "tenant": tenant})

    def keys(self, all_tenants: bool = False) -> list[dict]:
        return self._call("GET", f"/v1/keys?all_tenants={'true' if all_tenants else 'false'}")["keys"]

    def revoke_key(self, key_id: str) -> dict:
        return self._call("DELETE", f"/v1/keys/{key_id}")

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
        return self.db._call("GET", f"/v1/collections/{self.name}/documents/{_seg(id)}")

    def documents(self, limit: int = 100, offset: int = 0, status: str | None = None) -> dict:
        q = f"?limit={limit}&offset={offset}" + (f"&status={status}" if status else "")
        return self.db._call("GET", f"/v1/collections/{self.name}/documents{q}")

    def delete_document(self, id: str) -> dict:
        return self.db._call("DELETE", f"/v1/collections/{self.name}/documents/{_seg(id)}")

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
              principals: Sequence[str] | None = None, graph_hops: int | None = None, graph_k: int = 5,
              rerank: bool | None = None) -> list[dict]:
        """Hybrid retrieval over the documents: the top `k` hits, then up to `graph_k` connected chunks
        (via="graph", with graph_path). `principals` (admin key only) is who is asking, e.g. ["user:42"].
        `rerank` re-orders the hits with a cross-encoder (default: the server's SWSDB_RERANK)."""
        body = {"query": query, "k": k, "mode": mode, "where": dict(where) if where else None,
                "principals": list(principals) if principals is not None else None, "graph_hops": graph_hops,
                "graph_k": graph_k}
        if rerank is not None:
            body["rerank"] = rerank
        return self.db._call("POST", f"/v1/collections/{self.name}/query", body)["hits"]

    def answer(self, question: str, k: int = 5, *, mode: str = "hybrid", where: Mapping[str, Any] | None = None,
               principals: Sequence[str] | None = None, graph_hops: int | None = None,
               generate: bool = True) -> dict:
        """A short answer citing the passages it uses as [n]: {"answer", "cited", "passages", "mode", ...}. With a
        chat model configured on the server it is generated; otherwise it is the best matching sentences."""
        body = {"question": question, "k": k, "mode": mode, "where": dict(where) if where else None,
                "principals": list(principals) if principals is not None else None, "graph_hops": graph_hops,
                "generate": generate}
        return self.db._call("POST", f"/v1/collections/{self.name}/answer", body, timeout=180)

    def text(self, id: str, principals: Sequence[str] | None = None) -> dict:
        """A document's searchable text (its live version), if the caller may read it."""
        q = f"?principals={quote(','.join(principals))}" if principals else ""
        return self.db._call("GET", f"/v1/collections/{self.name}/text/{_seg(id)}{q}")

    def diagnose(self, id: str) -> dict:
        """Why a document is or isn't searchable: catalog, pending changes, vectors and graph, with a verdict."""
        return self.db._call("GET", f"/v1/collections/{self.name}/diagnose/{_seg(id)}")

    def put_eval_set(self, name: str, cases: Sequence[Mapping[str, Any]]) -> dict:
        """A golden set: [{"query": ..., "relevant": [document ids], "answer": optional phrase}]."""
        body = {"cases": [dict(c) for c in cases]}
        return self.db._call("PUT", f"/v1/collections/{self.name}/eval/sets/{_seg(name)}", body)

    def evaluate(self, set_name: str, k: int = 5, configs: Sequence[Mapping[str, Any]] | None = None,
                 principals: Sequence[str] | None = None) -> dict:
        """Ask every question under each configuration (default: hybrid, dense, keyword, hybrid+graph).
        `principals` evaluates as that user would search (admin key)."""
        body: dict[str, Any] = {"set": set_name, "k": k}
        if configs:
            body["configs"] = [dict(c) for c in configs]
        if principals is not None:
            body["principals"] = list(principals)
        return self.db._call("POST", f"/v1/collections/{self.name}/eval/runs", body, timeout=600)
