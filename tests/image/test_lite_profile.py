"""SWSDB_PROFILE=lite: vectors (pgvector) and graphs (Apache AGE) inside PostgreSQL, so Postgres is the only engine.

What only this profile has to prove: no other engine runs, graphs and vectors come back from a snapshot (they are in
the Postgres dump), restarts keep them, a tenant can't reach another tenant's graph even through SQL, and a snapshot
never restores into the other profile. The API itself runs through the same suites as the default profile
(test_security.py and test_quality.py run on both).
"""

from __future__ import annotations

import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN = "lite-admin-key"
DOCS = {  # linked by the names they share (Harrier Works, Osprey Drone)
    "falcon": "Kestrel Labs builds the Falcon engine for Harrier Works.",
    "osprey": "Harrier Works assembles the Osprey Drone in Pune.",
    "harrier": "Harrier Works was founded in 2011 by Meera Iyer.",
    "menu": "The cafeteria serves lentil soup on Fridays.",
}


def status_of(fn) -> int:
    try:
        fn()
    except SwsDBError as e:
        return e.status
    return 200


@pytest.fixture(scope="module")
def lite():
    vol = f"swsdb-lite-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_PROFILE": "lite"}).start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def client(b: Box, key: str = ADMIN, tenant: str | None = None) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{b.port}", api_key=key, tenant=tenant, timeout=300)


@pytest.fixture(scope="module")
def data(lite):
    db = client(lite)
    kb = db.create_collection("kb", documents=True)
    for doc, text in DOCS.items():
        kb.add_text(doc, text, title=doc)
    for doc in DOCS:
        kb.wait_ready(doc, timeout=300)
    raw = db.create_collection("raw", vector_size=3)
    raw.upsert([{"id": "p1", "vector": [1, 0, 0], "payload": {"team": "a"}},
                {"id": 2, "vector": [0, 1, 0], "payload": {"team": "b"}}])
    raw.cypher("CREATE (:Person {name: 'Al'})-[:KNOWS {since: 2020}]->(:Person {name: 'Bo'})")
    db.sql("CREATE TABLE notes (id int PRIMARY KEY, body text)")
    db.sql("INSERT INTO notes VALUES (1, 'kept')")
    return db


def test_postgres_is_the_only_engine(lite):
    code, st = lite.get("/v1/status", ADMIN)
    assert code == 200 and st["status"] == "ready"
    assert {c: e["engine"] for c, e in st["engines"].items()} == {"sql": "postgres", "vector": "pgvector",
                                                                  "graph": "age"}
    procs = lite.exec("ps", "-eo", "comm")
    assert "qdrant" not in procs and "redis-server" not in procs
    assert "in PostgreSQL: vector (pgvector), graph (age)" in lite.logs()


def test_documents_vectors_graph_and_sql(lite, data):
    kb = data.collection("kb")
    hits = kb.query("Who builds the Falcon engine?", k=1, graph_hops=1, graph_k=3)
    assert hits[0]["document"] == "falcon"
    linked = {h["document"] for h in hits if h["via"] == "graph"}
    assert linked and linked <= {"osprey", "harrier"}, hits  # documents naming Harrier Works, through the graph
    assert all("mentions Harrier Works" in h["graph_path"][0] for h in hits if h["via"] == "graph")
    for mode in ("dense", "sparse"):
        assert kb.query("Harrier Works founded", k=1, mode=mode)[0]["document"] == "harrier"
    assert "2011" in kb.answer("When was Harrier Works founded?")["answer"]
    hits = data.collection("raw").search([1, 0, 0], limit=2)
    assert [h["id"] for h in hits] == ["p1", 2] and hits[0]["score"] == pytest.approx(1.0)
    assert [h["id"] for h in data.collection("raw").search([1, 0, 0], where={"team": "b"})] == [2]
    page = data.collection("raw").browse(limit=1)
    rest = data.collection("raw").browse(limit=1, offset=page["next"])
    assert {p["id"] for p in page["points"] + rest["points"]} == {"p1", 2} and rest["next"] is None
    chunks = data.collection("kb").browse(limit=100)["points"]
    assert len(chunks) == len(DOCS) and all(p["payload"]["live"] for p in chunks)
    rows = data.collection("raw").cypher("MATCH p=(a)-[k:KNOWS]->(b) RETURN a.name AS who, k.since, p",
                                         read_only=True)
    assert rows["columns"] == ["who", "k.since", "p"]
    who, since, path = rows["rows"][0]
    assert (who, since) == ("Al", 2020) and [n["labels"] for n in path["nodes"]] == [["Person"], ["Person"]]
    assert path["edges"][0]["type"] == "KNOWS"
    assert data.sql("SELECT body FROM notes")["rows"] == [["kept"]]
    assert data.reconcile()["issues"] == 0


def test_the_consoles_vector_views(lite, data):
    """Configuration, memory (table and index sizes) and the map work on pgvector; Qdrant's own views say so."""
    import httpx

    c = httpx.Client(base_url=f"http://127.0.0.1:{lite.port}/v1/collections", headers={"api-key": ADMIN}, timeout=60)
    ins = c.get("/raw/inspect").json()
    assert ins["engine"] == "pgvector" and ins["vectors"][0]["size"] == 3 and ins["points"] == 2
    mem = c.get("/kb/memory").json()
    assert mem["engine"] == "pgvector" and mem["total"]["disk_bytes"] > 0
    m = c.post("/kb/map", json={"limit": 50, "color_by": "doc"}).json()
    assert len(m["points"]) >= 3 and {p["label"] for p in m["points"]} >= {"falcon", "harrier"}
    assert [h["id"] for h in c.post("/raw/similar", json={"like": "p1", "limit": 2}).json()["hits"]] == [2]
    g = c.get("/raw/graph").json()  # the graph browser's summary, from Apache AGE
    assert g["engine"] == "age" and g["nodes"] == 2 and g["edges"] == 1
    assert g["labels"] == [{"name": "Person", "count": 2}] and g["relationships"] == [{"name": "KNOWS", "count": 1}]
    assert g["schema"] == [{"from": "Person", "type": "KNOWS", "to": "Person", "count": 1}]
    assert {"name", "since"} <= set(g["property_keys"]) or "name" in g["property_keys"]
    for r in (c.get("/raw/segments"), c.post("/raw/recall", json={}),
              c.put("/raw/index", json={"field": "team", "type": "keyword"})):
        assert r.status_code == 409 and "lite profile" in r.json()["message"]


def test_a_tenant_reaches_neither_another_graph_nor_the_vector_tables(lite, data):
    root = client(lite)
    acme = client(lite, root.create_key("admin", "lite-acme", tenant="acme")["key"])
    acme.create_collection("kb", graph=True).cypher("CREATE (:Secret {code: 'ALPHA-7731'})")
    globex = client(lite, root.create_key("admin", "lite-globex", tenant="globex")["key"])
    globex.create_collection("kb", graph=True)
    assert globex.collection("kb").cypher("MATCH (n) RETURN count(n)", read_only=True)["rows"] == [[0]]
    # the graphs are Postgres schemas: SQL as globex can't open acme's, the default tenant's, or swsdb's own
    for q in ('SELECT * FROM sw_acme__kb."Secret"', 'SELECT * FROM sw_default__kb."Chunk"',
              "SELECT * FROM swsdb_lite.collections", "SELECT * FROM ag_catalog.ag_label",
              "SELECT ag_catalog.drop_graph('sw_acme__kb', true)",
              "SELECT * FROM ag_catalog.cypher('sw_acme__kb', $$ MATCH (n) RETURN n $$) AS (n ag_catalog.agtype)"):
        assert status_of(lambda q=q: globex.sql(q)) == 400, q
    assert "ALPHA-7731" in str(acme.collection("kb").cypher("MATCH (n:Secret) RETURN n.code", read_only=True))
    # Cypher can't call Postgres functions it has no right to, or close the quoting it runs in
    for q in ("RETURN pg_catalog.pg_read_file('/etc/passwd')", "RETURN 1 $x$) AS (a agtype); SELECT 1; --"):
        assert status_of(lambda q=q: globex.collection("kb").cypher(q, read_only=True)) == 400


def test_snapshot_round_trip_and_restart(lite, data):
    snap = data.create_snapshot("lite")
    assert snap["profile"] == "lite" and "postgres.dump" not in str(snap.get("graphs"))
    assert "sw_default__kb" in snap["graphs"] and "sw_default__kb" in snap["vector_collections"]
    data.delete_collection("kb")
    data.delete_collection("raw")
    data.sql("DROP TABLE notes")
    out = data.restore_snapshot(snap["id"], backup_first=False)
    assert out["reconcile"]["issues"] == 0, out
    kb = data.collection("kb")
    assert kb.query("Harrier Works founded", k=1)[0]["document"] == "harrier"
    assert data.collection("raw").cypher("MATCH (p:Person) RETURN count(p)", read_only=True)["rows"] == [[2]]
    assert data.sql("SELECT body FROM notes")["rows"] == [["kept"]]
    # the restored graph still belongs to its tenant's role: writes, new labels and new documents work
    data.collection("raw").cypher("CREATE (:City {name: 'Pune'})")
    kb.add_text("pune", "Pune hosts the Osprey drone line of Harrier Works.")
    kb.wait_ready("pune", timeout=300)
    assert data.reconcile()["issues"] == 0
    sh("docker", "restart", lite.name, timeout=180)
    lite.wait_ready()
    assert client(lite).collection("raw").cypher("MATCH (c:City) RETURN c.name", read_only=True)["rows"] == \
        [["Pune"]]
    assert client(lite).collection("kb").query("Pune drone line", k=1)[0]["document"] == "pune"


def test_a_lite_snapshot_does_not_restore_into_the_default_profile(lite, data, tmp_path):
    snap = data.create_snapshot("move")
    path = data.download_snapshot(snap["id"], tmp_path / "lite.tar")
    vol = f"swsdb-lite-other-{uuid.uuid4().hex[:6]}"
    other = Box(vol, {"SWSDB_API_KEY": ADMIN}).start()
    try:
        other.wait_ready()
        db = client(other)
        up = db.upload_snapshot(path)
        with pytest.raises(SwsDBError) as e:
            db.restore_snapshot(up["id"])
        assert "SWSDB_PROFILE=lite" in str(e.value) and e.value.status == 400
    finally:
        other.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)
