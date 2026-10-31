"""API v1 end to end, through the Python SDK, against a real container."""

from __future__ import annotations

import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN, READ = "admin-key-for-tests", "read-key-for-tests"


@pytest.fixture(scope="module")
def box():
    vol = f"swsdb-api-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_READ_ONLY_API_KEY": READ}).start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def db(box: Box, key: str = ADMIN, tenant: str = "acme") -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{box.port}", api_key=key, tenant=tenant)


def err(fn) -> SwsDBError:
    with pytest.raises(SwsDBError) as e:
        fn()
    return e.value


def test_vectors(box):
    d = db(box)
    docs = d.create_collection("docs", vector_size=3, distance="cosine")
    assert err(lambda: d.create_collection("docs", vector_size=3)).status == 409
    n = docs.upsert([
        {"id": "handbook#1", "vector": [1, 0, 0], "payload": {"team": "support", "year": 2025}},
        {"id": "handbook#2", "vector": [0, 1, 0], "payload": {"team": "sales", "year": 2024}},
        {"id": 7, "vector": [0.9, 0.1, 0], "payload": {"team": "support", "year": 2024}},
    ])
    assert n == 3
    hits = docs.search([1, 0, 0], limit=2)
    assert [h["id"] for h in hits] == ["handbook#1", 7]  # the ids we gave, not engine ids
    assert hits[0]["payload"] == {"team": "support", "year": 2025}
    assert [h["id"] for h in docs.search([1, 0, 0], where={"team": "sales"})] == ["handbook#2"]
    assert {h["id"] for h in docs.search([1, 0, 0], where={"year": [2024]})} == {"handbook#2", 7}
    # paging through every point (the console's vector browser): two pages of 2 and 1, the ids we gave
    first = docs.browse(limit=2)
    second = docs.browse(limit=2, offset=first["next"])
    assert first["next"] is not None and second["next"] is None
    assert {p["id"] for p in first["points"] + second["points"]} == {"handbook#1", "handbook#2", 7}
    assert [p["id"] for p in docs.browse(where={"team": "sales"}, with_vector=True)["points"]] == ["handbook#2"]
    assert docs.browse(where={"team": "sales"}, with_vector=True)["points"][0]["vector"] == pytest.approx([0, 1, 0])
    assert docs.get(["handbook#2"])[0]["vector"] == pytest.approx([0, 1, 0])
    docs.delete(["handbook#2"])
    assert docs.get(["handbook#2"]) == []
    assert err(lambda: docs.search([1, 0])).status == 400  # wrong dimension, clear message
    info = docs.info()
    assert info["vector"]["size"] == 3 and info["vector"]["points"] == 2 and info["graph"] == {"nodes": 0}


def test_graph_with_parameters(box):
    kb = db(box).create_collection("kb")  # graph only (no vectors)
    evil = "x'}) MATCH (n) DETACH DELETE n //"
    kb.cypher("CREATE (:Doc {title: $t}), (:Doc {title: 'keep'})", {"t": evil})
    r = kb.cypher("MATCH (d:Doc) RETURN d.title ORDER BY d.title", read_only=True)
    assert r["columns"] == ["d.title"] and [row[0] for row in r["rows"]] == ["keep", evil]
    node = kb.cypher("MATCH (d:Doc {title: 'keep'}) RETURN d", read_only=True)["rows"][0][0]
    assert node["labels"] == ["Doc"] and node["properties"] == {"title": "keep"}
    e = err(lambda: kb.cypher("CREATE (:Doc {title: 'x'})", read_only=True))
    assert e.status == 400  # writes are refused in read-only mode
    assert err(lambda: kb.cypher("MATCH (n RETURN n")).status == 400
    # lists, maps, numbers and booleans come back with their types, not as text
    typed = kb.cypher("RETURN [1, 2.5, 'z'], {k: [1, 2], b: false}, 1.5, true, null", read_only=True)["rows"][0]
    assert typed == [[1, 2.5, "z"], {"k": [1, 2], "b": False}, 1.5, True, None]
    path = kb.cypher("CREATE (:P {n: 1})-[:L {w: 2}]->(:P {n: 2})")
    assert path["columns"] == []
    p = kb.cypher("MATCH p=(:P)-[:L]->(:P) RETURN p", read_only=True)["rows"][0][0]
    assert [n["properties"]["n"] for n in p["nodes"]] == [1, 2] and p["edges"][0]["type"] == "L"


def test_sql_is_sandboxed(box):
    d = db(box)
    d.sql("CREATE TABLE IF NOT EXISTS notes (id int PRIMARY KEY, body text)")
    d.sql("INSERT INTO notes VALUES (%s, %s)", [1, "hello"])
    r = d.sql("SELECT id, body, now() AS at FROM notes WHERE id = %(id)s", {"id": 1})
    assert r["columns"] == ["id", "body", "at"] and r["rows"][0][:2] == [1, "hello"]
    assert d.sql("SELECT '100%' AS pct")["rows"] == [["100%"]]  # no placeholder confusion without params
    # one statement only: no smuggling a second one, no escaping read-only mode
    assert err(lambda: d.sql("SELECT 1; DROP TABLE notes")).status == 400
    assert err(lambda: d.sql("INSERT INTO notes VALUES (2, 'x')", read_only=True)).status == 400
    # not a superuser and can't become one; swsdb's own schema is off limits
    for stmt in ("SET ROLE swsdb", "SELECT * FROM swsdb.migrations", "CREATE DATABASE nope",
                 "ALTER ROLE swsdb_app SUPERUSER", "COPY notes TO PROGRAM 'id'"):
        assert err(lambda s=stmt: d.sql(s)).status == 400, stmt
    assert d.sql("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")["rows"] == [
        ["swsdb_t_acme", False]]  # the tenant's own sandbox role
    assert err(lambda: d.sql("SELECT pg_sleep(5)", timeout_s=0.5)).status == 400  # statement timeout
    big = d.sql("SELECT g FROM generate_series(1, 50) g", max_rows=10)
    assert big["row_count"] == 10 and big["truncated"] is True


def test_tenants_are_isolated(box):
    db(box, tenant="tenant-a").create_collection("private", vector_size=2)
    assert "private" in [c["name"] for c in db(box, tenant="tenant-a").collections()]
    other = db(box, tenant="tenant-b")
    assert "private" not in [c["name"] for c in other.collections()]
    assert err(lambda: other.collection("private").info()).status == 404
    assert err(lambda: db(box, tenant="Bad Tenant").collections()).status == 422


def test_read_only_key(box):
    db(box).create_collection("pub", vector_size=2).upsert([{"id": 1, "vector": [1, 0]}])
    ro = db(box, key=READ)
    assert ro.collection("pub").search([1, 0])[0]["id"] == 1
    assert ro.sql("SELECT 1", read_only=True)["rows"] == [[1]]
    for call in (lambda: ro.collection("pub").upsert([{"id": 2, "vector": [0, 1]}]),
                 lambda: ro.create_collection("x", vector_size=2),
                 lambda: ro.sql("SELECT 1"), lambda: ro.delete_collection("pub")):
        assert err(call).status == 403
    assert err(lambda: db(box, key="wrong").collections()).status == 401


def test_delete_collection(box):
    d = db(box)
    d.create_collection("temp", vector_size=2, graph=True)
    assert d.delete_collection("temp") == {"deleted": "temp", "vector": True, "graph": True, "documents": False}
    assert err(lambda: d.collection("temp").info()).status == 404


def test_cli_inside_the_container(box):
    out = box.exec("swsdb", "--api-key", ADMIN, "status", user="root")
    assert "ready" in out and "postgres" in out


def test_disabled_engine_gives_a_clear_error():
    vol = f"swsdb-api-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_ENGINES": "vector"}).start()
    try:
        b.wait_ready()
        d = SwsDB(f"http://127.0.0.1:{b.port}")
        e = err(lambda: d.sql("SELECT 1"))
        assert e.status == 409 and "not enabled" in e.message
        d.create_collection("only-vectors", vector_size=2)  # works without SQL or graph
        assert d.collections() == [{"name": "only-vectors", "vector": True, "graph": False}]
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def test_saved_queries_logs_and_advisors(box):
    """What the console's SQL editor, Logs and Advisors pages read."""
    d = db(box)
    d.sql("CREATE TABLE IF NOT EXISTS adv_parent (id int PRIMARY KEY)")
    d.sql("CREATE TABLE IF NOT EXISTS adv_child (pid int REFERENCES adv_parent (id), note text)")  # no key, no index
    d._call("PUT", "/v1/sql/saved/top%20parents", {"query": "SELECT * FROM adv_parent LIMIT 10"})
    d._call("PUT", "/v1/sql/saved/top%20parents", {"query": "SELECT * FROM adv_parent LIMIT 5"})  # replaces
    saved = d._call("GET", "/v1/sql/saved")["queries"]
    assert [(q["name"], q["query"]) for q in saved if q["name"] == "top parents"] == \
        [("top parents", "SELECT * FROM adv_parent LIMIT 5")]
    assert err(lambda: d._call("PUT", "/v1/sql/saved/%20x", {"query": "SELECT 1"})).status == 422
    d._call("DELETE", "/v1/sql/saved/top%20parents")
    assert err(lambda: d._call("DELETE", "/v1/sql/saved/top%20parents")).status == 404

    err(lambda: d._call("GET", "/v1/collections/nope-missing"))  # a 404 for the log
    logs = d._call("GET", "/v1/logs?limit=50")
    assert logs["entries"] and all(set(e) >= {"at", "method", "route", "status", "ms"} for e in logs["entries"])
    assert all("{" in e["route"] or "nope" not in e["route"] for e in logs["entries"])  # templates, not raw paths
    misses = d._call("GET", "/v1/logs?status=4xx")["entries"]
    assert misses and all(400 <= e["status"] < 500 for e in misses)
    assert any(r["route"] == "/v1/sql/saved/{qname}" for r in logs["routes"])

    adv = d._call("GET", "/v1/advisors")
    checks = {f["check"]: f for f in adv["findings"]}
    assert "adv_child" in checks["no_primary_key"]["items"]
    assert any("adv_child" in x for x in checks["unindexed_foreign_keys"]["items"])
    assert adv["counts"]["error"] + adv["counts"]["warning"] + adv["counts"]["info"] == len(adv["findings"])
    assert all(f["fix"] and f["detail"] for f in adv["findings"])


def test_a_tenant_key_sees_only_its_saved_queries_logs_and_findings(box):
    root = db(box)
    root._call("PUT", "/v1/sql/saved/acme-only", {"query": "SELECT 'acme'"})  # tenant acme (the client's default)
    other = SwsDB(f"http://127.0.0.1:{box.port}", api_key=root.create_key("admin", "beta-admin", tenant="beta")["key"])
    assert all(q["name"] != "acme-only" for q in other._call("GET", "/v1/sql/saved")["queries"])
    other.sql("SELECT 1")
    entries = other._call("GET", "/v1/logs?limit=500")["entries"]
    assert entries and {e["tenant"] for e in entries} == {"beta"}
    adv = other._call("GET", "/v1/advisors")
    assert adv["scope"] == "tenant" and adv["tenant"] == "beta"
    assert not {"open_api", "native_ports", "no_backups"} & {f["check"] for f in adv["findings"]}  # server-wide checks
    assert err(lambda: SwsDB(f"http://127.0.0.1:{box.port}", api_key=READ)._call("GET", "/v1/logs")).status == 403


def test_saved_queries_can_be_pinned_for_reports(box):
    d = db(box)
    d._call("PUT", "/v1/sql/saved/daily", {"query": "SELECT 1 AS n"})
    assert [q["pinned"] for q in d._call("GET", "/v1/sql/saved")["queries"] if q["name"] == "daily"] == [False]
    d._call("PUT", "/v1/sql/saved/daily", {"query": "SELECT 1 AS n", "pinned": True})
    d._call("PUT", "/v1/sql/saved/daily", {"query": "SELECT 2 AS n"})  # editing the query keeps the pin
    q = next(q for q in d._call("GET", "/v1/sql/saved")["queries"] if q["name"] == "daily")
    assert q["pinned"] is True and q["query"] == "SELECT 2 AS n"
