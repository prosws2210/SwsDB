"""Async client for swsdb, with the same methods as `SwsDB` (awaitable).

    from swsdb import AsyncSwsDB

    async with AsyncSwsDB("http://localhost:6464", api_key="...") as db:
        kb = await db.create_collection("kb", documents=True)
        await kb.add_text("refunds", "Refunds take five business days.")
        await kb.wait_ready("refunds")
        hits = await kb.query("how long do refunds take?")
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .client import DEFAULT_URL, SwsDB, SwsDBError, _seg


class AsyncSwsDB:
    """A connection to one swsdb server, acting for one tenant. Arguments as for `SwsDB`."""

    def __init__(self, url: str | None = None, api_key: str | None = None, tenant: str | None = None,
                 timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None, verify: bool | str = True):
        self.url = (url or os.environ.get("SWSDB_URL") or DEFAULT_URL).rstrip("/")
        self.tenant = tenant or os.environ.get("SWSDB_TENANT") or None
        key = api_key if api_key is not None else os.environ.get("SWSDB_API_KEY")
        headers = {"user-agent": "swsdb-python"}
        if self.tenant:
            headers["swsdb-tenant"] = self.tenant
        if key:
            headers["api-key"] = key
        self._http = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=timeout, transport=transport,
                                       verify=verify)

    async def _call(self, method: str, path: str, json: Any = None, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, json=json, **kw)
        except httpx.HTTPError as e:
            raise SwsDBError(0, "unreachable", f"can't reach swsdb at {self.url}: {e}") from e
        return SwsDB._result(r)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncSwsDB:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # -- server

    async def ready(self) -> bool:
        try:
            return (await self._http.get("/readyz")).status_code == 200
        except httpx.HTTPError:
            return False

    async def status(self) -> dict:
        return await self._call("GET", "/v1/status")

    async def collections(self) -> list[dict]:
        return (await self._call("GET", "/v1/collections"))["collections"]

    def collection(self, name: str) -> AsyncCollection:
        return AsyncCollection(self, name)

    async def create_collection(self, name: str, vector_size: int | None = None, distance: str = "cosine",
                                graph: bool | None = None, documents: bool = False) -> AsyncCollection:
        body: dict[str, Any] = {"graph": graph, "documents": documents}
        if vector_size is not None:
            body["vector"] = {"size": vector_size, "distance": distance}
        await self._call("PUT", f"/v1/collections/{_seg(name)}", body)
        return AsyncCollection(self, name)

    async def delete_collection(self, name: str) -> dict:
        return await self._call("DELETE", f"/v1/collections/{_seg(name)}")

    async def reconcile(self, repair: bool = False, all_tenants: bool = False) -> dict:
        return await self._call("POST", "/v1/admin/reconcile", {"dry_run": not repair, "all_tenants": all_tenants})

    async def sql(self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None, *,
                  read_only: bool = False, timeout_s: float = 30, max_rows: int = 1000) -> dict:
        return await self._call("POST", "/v1/sql", {"query": query, "params": params, "read_only": read_only,
                                                    "timeout_s": timeout_s, "max_rows": max_rows})

    async def create_snapshot(self, name: str | None = None) -> dict:
        return await self._call("POST", "/v1/admin/snapshots", {"name": name}, timeout=None)

    async def snapshots(self) -> list[dict]:
        return (await self._call("GET", "/v1/admin/snapshots"))["snapshots"]

    async def restore_snapshot(self, snapshot_id: str, backup_first: bool = True) -> dict:
        return await self._call("POST", f"/v1/admin/snapshots/{_seg(snapshot_id)}/restore",
                                {"backup_first": backup_first}, timeout=None)

    async def tenants(self) -> list[dict]:
        return (await self._call("GET", "/v1/admin/tenants"))["tenants"]

    async def create_key(self, role: str = "read", name: str | None = None, tenant: str | None = None) -> dict:
        return await self._call("POST", "/v1/keys", {"role": role, "name": name, "tenant": tenant})

    async def keys(self, all_tenants: bool = False) -> list[dict]:
        return (await self._call("GET", f"/v1/keys?all_tenants={'true' if all_tenants else 'false'}"))["keys"]

    async def revoke_key(self, key_id: str) -> dict:
        return await self._call("DELETE", f"/v1/keys/{_seg(key_id)}")


class AsyncCollection:
    def __init__(self, db: AsyncSwsDB, name: str):
        self.db, self.name = db, name
        self._p = f"/v1/collections/{_seg(name)}"

    def __repr__(self) -> str:
        return f"AsyncCollection({self.name!r}, tenant={self.db.tenant!r})"

    async def info(self) -> dict:
        return await self.db._call("GET", self._p)

    async def drop(self) -> dict:
        return await self.db.delete_collection(self.name)

    # -- vectors and graph

    async def upsert(self, points: Iterable[Mapping[str, Any]], batch_size: int = 500) -> int:
        total, batch = 0, []
        for p in points:
            batch.append(dict(p))
            if len(batch) >= batch_size:
                total += (await self.db._call("PUT", f"{self._p}/points", {"points": batch}))["upserted"]
                batch = []
        if batch:
            total += (await self.db._call("PUT", f"{self._p}/points", {"points": batch}))["upserted"]
        return total

    async def search(self, vector: Sequence[float], limit: int = 10, where: Mapping[str, Any] | None = None,
                     with_payload: bool = True) -> list[dict]:
        return (await self.db._call("POST", f"{self._p}/search", {
            "vector": list(vector), "limit": limit, "where": dict(where) if where else None,
            "with_payload": with_payload}))["hits"]

    async def get(self, ids: Sequence[str | int]) -> list[dict]:
        return (await self.db._call("POST", f"{self._p}/points/get", {"ids": list(ids)}))["points"]

    async def delete(self, ids: Sequence[str | int]) -> int:
        return (await self.db._call("POST", f"{self._p}/points/delete", {"ids": list(ids)}))["deleted"]

    async def cypher(self, query: str, params: Mapping[str, Any] | None = None, *, read_only: bool = False,
                     timeout_s: float = 30) -> dict:
        return await self.db._call("POST", f"{self._p}/cypher", {
            "query": query, "params": dict(params) if params else None, "read_only": read_only,
            "timeout_s": timeout_s})

    # -- documents

    async def add_text(self, id: str, text: str, *, title: str | None = None,
                       metadata: Mapping[str, Any] | None = None, readers: Sequence[str] | None = None,
                       format: str = "markdown") -> dict:
        return await self.db._call("POST", f"{self._p}/documents/text", {
            "id": id, "text": text, "title": title, "format": format,
            "metadata": dict(metadata) if metadata else None, "readers": list(readers) if readers else None})

    async def upload(self, path: str | Path, *, id: str | None = None, title: str | None = None,
                     metadata: Mapping[str, Any] | None = None, readers: Sequence[str] | None = None) -> dict:
        p = Path(path)
        data: dict[str, str] = {"id": id or p.name}
        if title:
            data["title"] = title
        if metadata:
            data["metadata"] = json.dumps(dict(metadata))
        if readers:
            data["readers"] = json.dumps(list(readers))
        content = await asyncio.to_thread(p.read_bytes)
        return await self.db._call("POST", f"{self._p}/documents", files={"file": (p.name, content)}, data=data)

    async def document(self, id: str) -> dict:
        return await self.db._call("GET", f"{self._p}/documents/{_seg(id)}")

    async def documents(self, limit: int = 100, offset: int = 0, status: str | None = None) -> dict:
        q = f"?limit={limit}&offset={offset}" + (f"&status={status}" if status else "")
        return await self.db._call("GET", f"{self._p}/documents{q}")

    async def delete_document(self, id: str) -> dict:
        return await self.db._call("DELETE", f"{self._p}/documents/{_seg(id)}")

    async def wait_ready(self, id: str, timeout: float = 600, poll: float = 0.5) -> dict:  # noqa: ASYNC109 - mirrors SwsDB
        deadline = time.monotonic() + timeout
        while True:
            d = await self.document(id)
            if d["status"] == "ready":
                return d
            if d["status"] == "failed":
                raise SwsDBError(422, "document_failed", f"{id}: {d['error']}")
            if time.monotonic() > deadline:
                raise SwsDBError(408, "timeout", f"{id} still {d['status']} after {timeout}s")
            await asyncio.sleep(poll)

    async def query(self, query: str, k: int = 10, *, mode: str = "hybrid", where: Mapping[str, Any] | None = None,
                    principals: Sequence[str] | None = None, graph_hops: int | None = None, graph_k: int = 5,
                    rerank: bool | None = None) -> list[dict]:
        body = {"query": query, "k": k, "mode": mode, "where": dict(where) if where else None,
                "principals": list(principals) if principals is not None else None, "graph_hops": graph_hops,
                "graph_k": graph_k}
        if rerank is not None:
            body["rerank"] = rerank
        return (await self.db._call("POST", f"{self._p}/query", body))["hits"]

    async def answer(self, question: str, k: int = 5, *, mode: str = "hybrid",
                     where: Mapping[str, Any] | None = None, principals: Sequence[str] | None = None,
                     graph_hops: int | None = None, generate: bool = True) -> dict:
        return await self.db._call("POST", f"{self._p}/answer", {
            "question": question, "k": k, "mode": mode, "where": dict(where) if where else None,
            "principals": list(principals) if principals is not None else None, "graph_hops": graph_hops,
            "generate": generate}, timeout=180)

    async def text(self, id: str, principals: Sequence[str] | None = None) -> dict:
        q = f"?principals={quote(','.join(principals))}" if principals else ""
        return await self.db._call("GET", f"{self._p}/text/{_seg(id)}{q}")

    async def diagnose(self, id: str) -> dict:
        return await self.db._call("GET", f"{self._p}/diagnose/{_seg(id)}")

    async def put_eval_set(self, name: str, cases: Sequence[Mapping[str, Any]]) -> dict:
        return await self.db._call("PUT", f"{self._p}/eval/sets/{_seg(name)}", {"cases": [dict(c) for c in cases]})

    async def evaluate(self, set_name: str, k: int = 5, configs: Sequence[Mapping[str, Any]] | None = None,
                       principals: Sequence[str] | None = None) -> dict:
        body: dict[str, Any] = {"set": set_name, "k": k}
        if configs:
            body["configs"] = [dict(c) for c in configs]
        if principals is not None:
            body["principals"] = list(principals)
        return await self.db._call("POST", f"{self._p}/eval/runs", body, timeout=600)
