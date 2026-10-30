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
