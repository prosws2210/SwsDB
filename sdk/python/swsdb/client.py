from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
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
                          graph: bool | None = None) -> Collection:
        body: dict[str, Any] = {"graph": graph}
        if vector_size is not None:
            body["vector"] = {"size": vector_size, "distance": distance}
        self._call("PUT", f"/v1/collections/{name}", body)
        return Collection(self, name)

    def delete_collection(self, name: str) -> dict:
        return self._call("DELETE", f"/v1/collections/{name}")

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
