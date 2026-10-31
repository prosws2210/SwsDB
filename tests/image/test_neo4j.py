"""An existing Neo4j (Community) as the graph engine: documents keep their graph there, retrieval quality holds the
same floors, collections and tenants stay apart in the one shared database, pass-through Cypher is refused, and
snapshots carry the graphs."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh
from test_quality import FLOORS

pytestmark = pytest.mark.image

NEO4J_IMAGE = "mirror.gcr.io/library/neo4j:5-community"
DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "kestrel.json"
ADMIN = "neo4j-admin-key"


@pytest.fixture(scope="module")
def stack():
    tag = uuid.uuid4().hex[:8]
    net, neo, vol = f"swsdb-neo-{tag}", f"swsdb-neo4j-{tag}", f"swsdb-neo-vol-{tag}"
    password = secrets.token_urlsafe(18)
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", neo, "--network", net, "--network-alias", "neo4j",
       "-e", f"NEO4J_AUTH=neo4j/{password}", "-e", "NEO4J_server_memory_heap_max__size=512m",
       "-e", "NEO4J_server_memory_pagecache_size=128m", NEO4J_IMAGE, timeout=300)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_NEO4J_URL": "http://neo4j:7474",
                  "SWSDB_NEO4J_PASSWORD": password}, extra=["--network", net])
    try:
        b.start()
        b.wait_ready(timeout=240)
        yield b, neo, password
    finally:
        b.remove()
        sh("docker", "rm", "-f", neo, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def neo4j_query(b: Box, password: str, statement: str) -> list:
    """Straight to Neo4j (from the swsdb container, which has curl and reaches it by name)."""
    body = json.dumps({"statement": statement})
    out = b.exec("curl", "-s", "-u", f"neo4j:{password}", "-H", "content-type: application/json",
                 "-H", "accept: application/json", "-d", body, "http://neo4j:7474/db/neo4j/query/v2")
    return json.loads(out)["data"]["values"]


@pytest.fixture(scope="module")
def kb(stack):
    b, _, _ = stack
    db = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, timeout=600)
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    kb = db.create_collection("kestrel", documents=True)
    for d in data["documents"]:
        kb.add_text(d["id"], d["text"], title=d["title"])
    for d in data["documents"]:
        kb.wait_ready(d["id"], timeout=600)
    cases = [{k: q[k] for k in ("query", "relevant", "answer")} for q in data["questions"]]
    kb.put_eval_set("all", cases)
    kb.put_eval_set("multihop", [c for c, q in zip(cases, data["questions"], strict=True) if q.get("hops") == 2])
    return db, kb


def test_neo4j_is_the_graph_engine(stack):
    b, _, _ = stack
    code, st = b.get("/v1/status", ADMIN)
    assert code == 200 and st["engines"]["graph"]["engine"] == "neo4j"
    assert st["engines"]["graph"]["version"].startswith("5.")
    assert "redis-server" not in b.exec("ps", "-eo", "comm")
    assert "external: graph" in b.logs()


def test_retrieval_quality_holds_with_neo4j(kb):
    db, coll = kb
    assert db.reconcile()["issues"] == 0  # concurrent workers wrote graphs sharing entities: no mention lost
    configs = [{"name": "hybrid", "mode": "hybrid"},
               {"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1, "graph_k": 3}]
    for set_name in ("all", "multihop"):
        got = {r["config"]["name"]: r["metrics"] for r in coll.evaluate(set_name, k=5, configs=configs)["results"]}
        for metric, floor in FLOORS[set_name].get("hybrid+graph", {}).items():
            assert got["hybrid+graph"][metric] >= floor, (set_name, got)
        if set_name == "multihop":
            assert got["hybrid+graph"]["recall"] > got["hybrid"]["recall"], got


def test_collections_and_tenants_share_the_database_but_not_their_graphs(stack, kb):
    b, _, password = stack
    root, _ = kb
    url = f"http://127.0.0.1:{b.port}"
    tenants = {}
    for t, marker in (("acme", "Initech"), ("globex", "Umbrella")):
        db = SwsDB(url, api_key=root.create_key("admin", tenant=t)["key"], timeout=300)
        c = db.create_collection("kb", documents=True)
        c.add_text("doc", f"{marker} Labs ships the Falcon engine.")
        c.add_text("people", f"Maria Lopez runs {marker} Labs.")
        c.wait_ready("doc", timeout=300)
        c.wait_ready("people", timeout=300)
        tenants[t] = c
    for t, c in tenants.items():
        hits = c.query("who runs the lab that ships the Falcon engine", k=1, graph_hops=1, graph_k=3)
        assert {h["document"] for h in hits} == {"doc", "people"}, hits
        other = "Umbrella" if t == "acme" else "Initech"
        assert other not in json.dumps(hits)
    # in Neo4j every node of a collection carries that collection's own label, and no node is unlabelled
    counts = neo4j_query(b, password, "MATCH (n) WHERE NOT n:SwsdbGraph RETURN "
                                        "size([l IN labels(n) WHERE l STARTS WITH 'C_']) AS k, count(*)")
    assert {k for k, _ in counts} == {1}, counts
    graphs = {r[0] for r in neo4j_query(b, password, "MATCH (g:SwsdbGraph) RETURN g.name")}
    assert {"sw_default__kestrel", "sw_acme__kb", "sw_globex__kb"} <= graphs


def test_pass_through_cypher_is_refused_with_a_reason(kb):
    _, coll = kb
    with pytest.raises(SwsDBError) as e:
        coll.cypher("MATCH (n) RETURN count(n)", read_only=True)
    assert e.value.status == 403 and "one Neo4j database" in str(e.value)


def test_snapshot_round_trip_carries_the_neo4j_graph(stack, kb):
    db, coll = kb
    before = coll.query("Which drone does the Falcon engine power?", k=1, graph_hops=1, graph_k=3)
    snap = db.create_snapshot("neo4j")
    assert "sw_default__kestrel" in snap["graphs"]
    db.delete_collection("kestrel")
    out = db.restore_snapshot(snap["id"], backup_first=False)
    assert out["reconcile"]["issues"] == 0, out
    after = db.collection("kestrel").query("Which drone does the Falcon engine power?", k=1, graph_hops=1, graph_k=3)
    assert [(h["document"], h["via"]) for h in after] == [(h["document"], h["via"]) for h in before]
    deadline = time.time() + 30
    while time.time() < deadline:  # and the graph is still the one ingestion maintains
        if db.reconcile()["issues"] == 0:
            break
        time.sleep(1)
    else:
        raise AssertionError("graph mismatch after restore")
