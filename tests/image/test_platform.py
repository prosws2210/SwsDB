"""End users (Auth), the Data API with row-level security, database functions over HTTP, Storage and query statistics,
against a real container. The point of every test: a signed-in user reaches exactly what policies, exposure and
ownership allow, and never another user's or tenant's rows, files or documents."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN = "platform-admin-key"


@pytest.fixture(scope="module")
def box():
    vol = f"swsdb-platform-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN}).start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def http(box: Box, token: str | None = ADMIN, tenant: str | None = None) -> httpx.Client:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if tenant:
        headers["swsdb-tenant"] = tenant
    return httpx.Client(base_url=f"http://127.0.0.1:{box.port}", headers=headers, timeout=120)


def ok(r: httpx.Response, status: int = 200):
    assert r.status_code == status, f"{r.status_code}: {r.text[:400]}"
    return r.json()


@pytest.fixture(scope="module")
def users(box):
    """Two end users of tenant acme, and their tokens."""
    admin = http(box, tenant="acme")
    out = {}
    for name in ("ana", "ravi"):
        u = ok(admin.post("/v1/auth/users", json={"email": f"{name}@example.com", "password": f"{name}-secret-pw",
                                                    "groups": ["support"] if name == "ana" else []}), 201)
        tok = ok(http(box, None).post("/v1/auth/token", json={"email": f"{name}@example.com",
                                                             "password": f"{name}-secret-pw", "tenant": "acme"}))
        out[name] = {"id": u["id"], "token": tok["access_token"]}
    return out


def test_sign_up_sign_in_and_tokens(box, users):
    anon = http(box, None)
    assert anon.post("/v1/auth/signup", json={"email": "x@example.com", "password": "12345678", "tenant": "acme"}
                     ).status_code == 403  # closed until an admin opens it
    ok(http(box, tenant="acme").put("/v1/auth/settings", json={"signup": True, "token_ttl_s": 600}))
    signed = ok(anon.post("/v1/auth/signup", json={"email": "new@example.com", "password": "new-user-pw",
                                                   "tenant": "acme"}), 201)
    assert signed["expires_in"] == 600 and signed["user"]["email"] == "new@example.com"
    assert anon.post("/v1/auth/signup", json={"email": "new@example.com", "password": "other-pw-1",
                                              "tenant": "acme"}).status_code == 409
    me = ok(http(box, users["ana"]["token"]).get("/v1/auth/user"))
    assert me["tenant"] == "acme" and me["principals"] == [f"user:{users['ana']['id']}", "group:support"]
    assert anon.post("/v1/auth/token", json={"email": "ana@example.com", "password": "wrong-password",
                                             "tenant": "acme"}).status_code == 403
    assert http(box, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.bad").get("/v1/auth/user").status_code == 401
    # a token is for its tenant only
    assert http(box, users["ana"]["token"], tenant="globex").get("/v1/rest/anything").status_code == 403
    # a user is never a key: no SQL, no keys, no admin pages
    u = http(box, users["ana"]["token"])
    for call in (lambda: u.post("/v1/sql", json={"query": "SELECT 1", "read_only": True}),
                 lambda: u.get("/v1/keys"), lambda: u.get("/v1/collections"), lambda: u.get("/v1/logs")):
        assert call().status_code == 403
    # disabling a user and rotating the secret stop tokens at once
    ok(http(box, tenant="acme").patch(f"/v1/auth/users/{signed['user']['id']}", json={"disabled": True}))
    assert http(box, signed["access_token"]).get("/v1/auth/user").status_code == 401
    for _ in range(10):
        anon.post("/v1/auth/token", json={"email": "ravi@example.com", "password": "nope-nope", "tenant": "acme"})
    assert anon.post("/v1/auth/token", json={"email": "ravi@example.com", "password": "ravi-secret-pw",
                                             "tenant": "acme"}).status_code == 429


def test_data_api_with_keys(box):
    admin = http(box, tenant="acme")
    ok(admin.post("/v1/sql", json={"query": "CREATE TABLE items (id serial PRIMARY KEY, name text, qty int, "
                                            "tags jsonb)"}))
    rows = ok(admin.post("/v1/rest/items", json=[{"name": "bolt", "qty": 10, "tags": {"kind": "metal"}},
                                                  {"name": "nut", "qty": 3}, {"name": "gear"}]), 201)["data"]
    assert [r["name"] for r in rows] == ["bolt", "nut", "gear"] and rows[0]["tags"] == {"kind": "metal"}
    got = ok(admin.get("/v1/rest/items", params={"qty": "gte.3", "order": "qty.desc", "select": "name,qty",
                                                 "count": "exact"}))
    assert got["data"] == [{"name": "bolt", "qty": 10}, {"name": "nut", "qty": 3}] and got["count"] == 2
    assert ok(admin.get("/v1/rest/items", params={"qty": "is.null"}))["data"][0]["name"] == "gear"
    picked = ok(admin.get("/v1/rest/items", params={"name": "in.(nut,gear)", "order": "id"}))["data"]
    assert [r["name"] for r in picked] == ["nut", "gear"]
    assert ok(admin.patch("/v1/rest/items", params={"name": "eq.gear"}, json={"qty": 7}))["data"][0]["qty"] == 7
    assert admin.patch("/v1/rest/items", json={"qty": 0}).status_code == 400  # no filter: refused
    assert admin.delete("/v1/rest/items").status_code == 400
    assert ok(admin.delete("/v1/rest/items", params={"name": "eq.nut"}))["data"][0]["name"] == "nut"
    assert admin.get("/v1/rest/items", params={"nope": "eq.1"}).status_code == 400  # unknown column
    assert admin.get("/v1/rest/items", params={"qty": "eq.1; DROP TABLE items"}).status_code == 400  # a value, not SQL
    assert admin.get("/v1/rest/missing").status_code == 404


def test_row_level_security_for_end_users(box, users):
    admin = http(box, tenant="acme")
    ok(admin.post("/v1/sql", json={"query": "CREATE TABLE notes (id serial PRIMARY KEY, owner text NOT NULL "
                                            "DEFAULT swsdb_auth.uid(), body text)"}))
    ok(admin.post("/v1/rest/notes", json={"owner": "nobody", "body": "an admin note"}), 201)
    ana, ravi = http(box, users["ana"]["token"]), http(box, users["ravi"]["token"])
    assert ana.get("/v1/rest/notes").status_code == 403  # not exposed to users yet
    ok(admin.put("/v1/policies/tables/notes", json={"exposed": True}))
    assert ok(ana.get("/v1/rest/notes"))["data"] == []  # exposed, row-level security on, no policy: no rows
    ok(admin.post("/v1/policies/tables/notes", json={"name": "own notes", "command": "ALL",
                                                      "using": "owner = swsdb_auth.uid()",
                                                      "check": "owner = swsdb_auth.uid()"}), 201)
    mine = ok(ana.post("/v1/rest/notes", json={"body": "ana's note"}), 201)["data"][0]
    assert mine["owner"] == users["ana"]["id"]  # the default filled in who she is
    ok(ravi.post("/v1/rest/notes", json={"body": "ravi's note"}), 201)
    assert [n["body"] for n in ok(ana.get("/v1/rest/notes"))["data"]] == ["ana's note"]
    assert [n["body"] for n in ok(ravi.get("/v1/rest/notes"))["data"]] == ["ravi's note"]
    # she can't write a row as someone else, nor change or delete his
    assert ana.post("/v1/rest/notes", json={"owner": users["ravi"]["id"], "body": "forged"}).status_code == 403
    assert ok(ana.patch("/v1/rest/notes", params={"body": "eq.ravi's note"}, json={"body": "x"}))["data"] == []
    assert ok(ana.delete("/v1/rest/notes", params={"body": "eq.ravi's note"}))["data"] == []
    # filters can't widen what a user sees: they only narrow the rows policies allow
    assert ok(ana.get("/v1/rest/notes", params={"owner": f"eq.{users['ravi']['id']}"}))["data"] == []
    assert len(ok(admin.get("/v1/rest/notes"))["data"]) == 3  # a key isn't bound by policies
    pol = ok(admin.get("/v1/policies"))
    t = next(x for x in pol["tables"] if x["table"] == "notes")
    assert t["rls"] and t["exposed"] and t["policies"][0]["name"] == "own notes"
    ok(admin.put("/v1/policies/tables/notes", json={"exposed": False}))
    assert ana.get("/v1/rest/notes").status_code == 403
    ok(admin.delete("/v1/policies/tables/notes/own%20notes"))


def test_database_functions_over_http(box, users):
    admin = http(box, tenant="acme")
    ok(admin.post("/v1/sql", json={"query": "CREATE FUNCTION add_up(a int, b int) RETURNS int LANGUAGE sql "
                                            "IMMUTABLE AS 'SELECT a + b'"}))
    ok(admin.post("/v1/sql", json={"query": "CREATE FUNCTION whoami() RETURNS text LANGUAGE sql STABLE "
                                            "AS 'SELECT swsdb_auth.uid()'"}))
    assert ok(admin.post("/v1/rpc/add_up", json={"a": 2, "b": 3}))["data"] == [{"add_up": 5}]
    ana = http(box, users["ana"]["token"])
    assert ana.post("/v1/rpc/whoami", json={}).status_code == 403  # not exposed
    ok(admin.put("/v1/policies/functions/whoami", json={"exposed": True}))
    assert ok(ana.post("/v1/rpc/whoami", json={}))["data"] == [{"whoami": users["ana"]["id"]}]
    assert admin.post("/v1/rpc/add_up", json={"c": 1}).status_code == 400
    assert admin.post("/v1/rpc/missing", json={}).status_code == 404


def test_storage_buckets_files_and_ownership(box, users):
    admin = http(box, tenant="acme")
    ok(admin.post("/v1/storage/buckets", json={"name": "avatars"}), 201)
    ok(admin.post("/v1/storage/buckets", json={"name": "docs", "public": True}), 201)
    ana, ravi = http(box, users["ana"]["token"]), http(box, users["ravi"]["token"])
    a_id, r_id = users["ana"]["id"], users["ravi"]["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    ok(ana.put(f"/v1/storage/avatars/{a_id}/me.png", content=png, headers={"content-type": "image/png"}), 201)
    ok(ravi.put(f"/v1/storage/avatars/{r_id}/page.html", content=b"<script>alert(1)</script>",
                headers={"content-type": "text/html"}), 201)
    assert ana.put(f"/v1/storage/avatars/{r_id}/x.png", content=b"x").status_code == 403  # not her folder
    assert ana.put("/v1/storage/avatars/../etc/passwd", content=b"x").status_code in (400, 404)
    assert [o["path"] for o in ok(ana.get("/v1/storage/avatars"))["objects"]] == [f"{a_id}/me.png"]
    assert ana.get(f"/v1/storage/avatars/{r_id}/page.html").status_code == 404  # his file doesn't exist for her
    img = ana.get(f"/v1/storage/avatars/{a_id}/me.png")
    assert img.content == png and img.headers["content-type"] == "image/png"
    assert img.headers["content-disposition"].startswith("inline")
    page = admin.get(f"/v1/storage/avatars/{r_id}/page.html")  # the admin sees every file
    assert page.headers["content-type"] == "application/octet-stream"  # never served as a page
    assert page.headers["content-disposition"].startswith("attachment")
    assert page.headers["content-security-policy"] == "default-src 'none'; sandbox"
    ok(admin.put("/v1/storage/docs/handbook.txt", content=b"public handbook", headers={"content-type": "text/plain"}),
       201)
    assert ana.get("/v1/storage/docs/handbook.txt").content == b"public handbook"  # a public bucket
    assert ana.delete("/v1/storage/docs/handbook.txt").status_code == 403
    assert admin.delete("/v1/storage/buckets/avatars").status_code == 409  # not empty
    assert ok(admin.delete("/v1/storage/buckets/avatars", params={"force": "true"}))["files"] == 2
    assert http(box, tenant="globex").get("/v1/storage/docs").status_code == 404  # another tenant's bucket


def test_document_search_as_an_end_user(box, users):
    admin = http(box, tenant="acme")
    ok(admin.put("/v1/collections/kb", json={"documents": True}), 201)
    for doc, readers, text in (("mine", [f"user:{users['ana']['id']}"], "Ana's salary review is in March."),
                               ("team", ["group:support"], "The support rota changes in March."),
                               ("all", ["*"], "The office closes in March for a holiday."),
                               ("his", [f"user:{users['ravi']['id']}"], "Ravi's salary review is in March.")):
        ok(admin.post("/v1/collections/kb/documents/text", json={"id": doc, "text": text, "readers": readers}), 202)
    for doc in ("mine", "team", "all", "his"):
        for _ in range(240):
            if ok(admin.get(f"/v1/collections/kb/documents/{doc}"))["status"] == "ready":
                break
            time.sleep(0.5)
    hits = ok(http(box, users["ana"]["token"]).post("/v1/collections/kb/query", json={"query": "March", "k": 10}))
    assert {h["document"] for h in hits["hits"]} == {"mine", "team", "all"}  # never Ravi's
    assert http(box, users["ana"]["token"]).post("/v1/collections/kb/query", json={
        "query": "March", "principals": [f"user:{users['ravi']['id']}"]}).status_code == 403


def test_query_statistics(box):
    admin = http(box, tenant="acme")
    for _ in range(3):
        ok(admin.post("/v1/sql", json={"query": "SELECT count(*) FROM items WHERE qty > %s", "params": [1],
                                       "read_only": True}))
    stats = ok(http(box).get("/v1/query-stats", params={"limit": 200}))
    assert stats["available"], stats
    hit = [s for s in stats["statements"] if "FROM items WHERE qty >" in s["query"]]
    assert hit and hit[0]["calls"] >= 3 and "$1" in hit[0]["query"]  # normalized: no values
    acme_key = ok(http(box).post("/v1/keys", json={"role": "admin", "tenant": "acme"}), 201)["key"]
    mine = ok(http(box, acme_key).get("/v1/query-stats"))["statements"]
    assert mine and {s["role"] for s in mine} <= {"swsdb_t_acme", "swsdb_t_acme__u"}  # its own roles only
    assert http(box, acme_key).post("/v1/query-stats/reset").status_code == 403
    assert admin.post("/v1/query-stats/reset").status_code == 200  # the environment key is global here
