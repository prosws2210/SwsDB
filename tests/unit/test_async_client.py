"""The async SDK speaks the same API as the sync one: paths, bodies, headers and errors."""

import json

import httpx
import pytest
from swsdb import AsyncSwsDB, SwsDBError


def server(seen: list):
    def handle(req: httpx.Request) -> httpx.Response:
        is_json = req.headers.get("content-type") == "application/json"
        body = json.loads(req.content) if req.content and is_json else None
        seen.append((req.method, req.url.raw_path.decode(), body, dict(req.headers)))
        path = req.url.path
        if path.endswith("/query"):
            return httpx.Response(200, json={"hits": [{"document": "a", "score": 1.0}]})
        if path.endswith("/documents/a%2Fb") or path.endswith("/documents/a/b"):
            return httpx.Response(200, json={"id": "a/b", "status": "ready"})
        if path == "/v1/collections/kb":
            return httpx.Response(201, json={"name": "kb"})
        if path == "/v1/collections":
            return httpx.Response(200, json={"collections": [{"name": "kb"}]})
        return httpx.Response(404, json={"error": "not_found", "message": "nope"})
    return httpx.MockTransport(handle)


async def test_async_client_round_trip():
    seen: list = []
    async with AsyncSwsDB("http://x", api_key="k", tenant="acme", transport=server(seen)) as db:
        kb = await db.create_collection("kb", documents=True)
        assert [c["name"] for c in await db.collections()] == ["kb"]
        hits = await kb.query("refunds", k=3, rerank=True)
        assert hits[0]["document"] == "a"
        assert (await kb.wait_ready("a/b"))["status"] == "ready"
        with pytest.raises(SwsDBError) as e:
            await kb.diagnose("missing")
        assert e.value.status == 404
    method, path, body, headers = seen[2]
    assert method == "POST" and path == "/v1/collections/kb/query"
    assert body["k"] == 3 and body["rerank"] is True
    assert headers["api-key"] == "k" and headers["swsdb-tenant"] == "acme"
    assert seen[3][1] == "/v1/collections/kb/documents/a%2Fb"  # ids are one encoded path segment
