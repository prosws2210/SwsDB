"""Consistency under failure: outages, crashes, drift, and a randomised chaos run.

Every test ends with an independent check (CHECKER, run inside the container) that reads Postgres, Qdrant and
FalkorDB directly, without swsdb's code, and lists every disagreement between them. Chaos rounds: CHAOS_ROUNDS
(default 3).
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh
from test_documents import FAKE_LLM

pytestmark = pytest.mark.image

ADMIN = "admin-chaos-key"

CHECKER = r'''
import json, httpx, psycopg, redis
sec = json.load(open("/swsdb/data/swsdb/secrets.json"))
pg = psycopg.connect(host="/run/swsdb/pg", dbname="swsdb", user="swsdb", autocommit=True)
q = httpx.Client(base_url="http://127.0.0.1:6333", headers={"api-key": sec["qdrant_api_key"]}, timeout=30)
r = redis.Redis(port=6379, password=sec["falkordb_password"])
out = {"outbox": pg.execute("SELECT count(*) FROM swsdb.outbox").fetchone()[0], "pending": 0, "problems": [],
       "documents": 0}
for tenant, name, has_graph in pg.execute("SELECT tenant, name, graph FROM swsdb.collections").fetchall():
    key = f"sw_{tenant}__{name}"
    docs = {str(i): (st, lv, n) for i, st, lv, n in pg.execute(
        "SELECT id, status, live_version, chunks FROM swsdb.documents WHERE tenant=%s AND collection=%s",
        [tenant, name]).fetchall()}
    out["documents"] += len(docs)
    out["pending"] += sum(1 for st, _, _ in docs.values() if st in ("queued", "processing", "deleting"))
    counts, offset = {}, None
    while True:
        body = {"limit": 1000, "with_payload": {"include": ["document_id", "version", "live"]}}
        if offset is not None:
            body["offset"] = offset
        res = q.post(f"/collections/{key}/points/scroll", json=body).json()["result"]
        for p in res["points"]:
            pl = p["payload"]
            k = (pl["document_id"], pl["version"], bool(pl.get("live")))
            counts[k] = counts.get(k, 0) + 1
        offset = res.get("next_page_offset")
        if offset is None:
            break
    for (doc, ver, live), n in counts.items():
        if doc not in docs:
            out["problems"].append(f"{name}: {n} vectors of unknown document {doc}")
    for doc, (st, lv, n) in docs.items():
        if st != "ready":
            continue
        if counts.get((doc, lv, True), 0) != n:
            out["problems"].append(f"{name}: {doc} has {counts.get((doc, lv, True), 0)} live vectors, catalog {n}")
        extra = sum(c for (d, v, live), c in counts.items() if d == doc and (v != lv or not live))
        if extra:
            out["problems"].append(f"{name}: {doc} has {extra} vectors that aren't its live version")
    if has_graph and r.exists(key):
        cypher = "MATCH (d:Document) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) RETURN d.id, count(c)"
        rows = r.execute_command("GRAPH.RO_QUERY", key, cypher)[1]
        g = {(row[0].decode() if isinstance(row[0], bytes) else row[0]): row[1] for row in rows}
        for doc in g:
            if doc not in docs:
                out["problems"].append(f"{name}: graph holds unknown document {doc}")
        for doc, (st, lv, n) in docs.items():
            if st == "ready" and g.get(doc) != n:
                out["problems"].append(f"{name}: {doc} has {g.get(doc)} graph chunks, catalog {n}")
print(json.dumps(out))
'''


@pytest.fixture(scope="module")
def env():
    tag = uuid.uuid4().hex[:8]
    net, vol, llm = f"swsdb-net-{tag}", f"swsdb-chaos-{tag}", f"swsdb-llm-{tag}"
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", llm, "--network", net, "--network-alias", "llm", "-p", "127.0.0.1::8000",
       "python:3.12-slim-trixie", "python", "-c", FAKE_LLM)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_LLM_URL": "http://llm:8000/v1", "SWSDB_LLM_MODEL": "stand-in",
                  "SWSDB_RECONCILE_INTERVAL_S": "0", "SWSDB_INGEST_WORKERS": "3"}, extra=["--network", net])
    b.start()
    try:
        b.wait_ready()
        yield b, llm
    finally:
        b.remove()
        sh("docker", "rm", "-f", llm, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def db(b: Box) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, tenant="chaos", timeout=60)


def check(b: Box) -> dict:
    return json.loads(b.exec("/opt/swsdb/venv/bin/python", "-c", CHECKER))


def settle(b: Box, timeout: float = 300) -> dict:
    """Wait until every accepted change is applied, then return the checker's report."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            last = check(b)
            if last["outbox"] == 0 and last["pending"] == 0:
                return last
        except (AssertionError, ValueError):
            pass  # an engine is still restarting
        time.sleep(1)
    raise AssertionError(f"not settled after {timeout}s: {last}\n{b.logs()[-3000:]}")


def consistent(b: Box) -> dict:
    report = settle(b)
    assert report["problems"] == [], report["problems"]
    return report


def svc(b: Box, action: str, service: str) -> None:
    """Take an engine down for real (killed and kept down) or bring it back."""
    if action == "up":
        b.exec("/command/s6-svc", "-u", f"/run/service/{service}", user="root")
        return
    # A graceful stop isn't an outage: Qdrant refuses new connections but keeps serving existing keep-alive
    # connections (swsdb's pool) for ~20 s while it shuts down. So: want it down, kill it now, wait until it's gone.
    b.exec("/command/s6-svc", "-d", f"/run/service/{service}", user="root")
    b.exec("/command/s6-svc", "-k", f"/run/service/{service}", user="root")
    port = {"svc-qdrant": 6333, "svc-falkordb": 6379}[service]
    probe = (f"import socket; s = socket.socket(); s.settimeout(1); "
             f"print('open' if s.connect_ex(('127.0.0.1', {port})) == 0 else 'closed')")
    deadline = time.time() + 30
    while time.time() < deadline:
        if "closed" in b.exec("/opt/swsdb/venv/bin/python", "-c", probe):
            return
        time.sleep(0.2)
    raise AssertionError(f"{service} still accepting connections 30s after stopping it")


def llm_calls(llm: str) -> int:
    port = sh("docker", "port", llm, "8000").split(":")[-1].strip()
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        return json.loads(r.read())["calls"]


def ensure(d: SwsDB, name: str) -> None:
    try:
        d.create_collection(name, documents=True)
    except SwsDBError as e:
        if e.status != 409:
            raise


def test_uploads_work_while_the_vector_engine_is_down(env):
    b, _ = env
    d = db(b)
    ensure(d, "outage")
    kb = d.collection("outage")
    svc(b, "down", "svc-qdrant")
    try:
        for i in range(3):
            assert kb.add_text(f"doc{i}", f"Outage document {i} about the Pune warehouse.")["status"] == "queued"
        time.sleep(4)
        st = kb.document("doc0")
        assert st["status"] in ("queued", "processing") and (st["error"] or "").startswith("retrying"), st
    finally:
        svc(b, "up", "svc-qdrant")
    for i in range(3):
        assert kb.wait_ready(f"doc{i}", timeout=120)["status"] == "ready"
    consistent(b)


def test_a_graph_outage_delays_documents_but_loses_nothing(env):
    b, _ = env
    d = db(b)
    ensure(d, "graphout")
    kb = d.collection("graphout")
    svc(b, "down", "svc-falkordb")
    try:
        kb.add_text("g1", "Project Heron is run by Ana Silva in Lisbon.")
        time.sleep(4)
        assert kb.document("g1")["status"] != "ready"
    finally:
        svc(b, "up", "svc-falkordb")
    kb.wait_ready("g1", timeout=120)
    consistent(b)


def test_deleted_content_never_appears_even_before_the_stores_catch_up(env):
    b, _ = env
    d = db(b)
    ensure(d, "tomb")
    kb = d.collection("tomb")
    kb.add_text("secret", "The Kestrel launch code word is AMBERFALL.")
    kb.wait_ready("secret")
    svc(b, "down", "svc-falkordb")  # the next change for this document can't finish...
    try:
        kb.add_text("secret", "The Kestrel launch code word is AMBERFALL. Updated.")
        kb.delete_document("secret")  # ...so the delete waits behind it, and Qdrant still holds the vectors
        assert kb.document("secret")["status"] == "deleting"
        hits = kb.query("Kestrel launch code word", k=5)
        assert all(h["document"] != "secret" for h in hits), hits
    finally:
        svc(b, "up", "svc-falkordb")
    consistent(b)
    with pytest.raises(SwsDBError) as e:
        kb.document("secret")
    assert e.value.status == 404


def test_drift_is_found_and_repaired_without_calling_the_llm_again(env):
    b, llm = env
    d = db(b)
    ensure(d, "drift")
    kb = d.collection("drift")
    for i in range(3):
        kb.add_text(f"d{i}", f"Section {i}. Ravi Kumar audits the Chennai plant. " + "Routine detail. " * 120)
    for i in range(3):
        kb.wait_ready(f"d{i}")
    consistent(b)
    calls = llm_calls(llm)
    sec = json.loads(b.exec("cat", "/swsdb/data/swsdb/secrets.json"))
    key = "sw_chaos__drift"
    # damage every store behind swsdb's back
    b.exec("curl", "-sS", "-X", "POST", f"http://127.0.0.1:6333/collections/{key}/points/delete?wait=true",
           "-H", f"api-key: {sec['qdrant_api_key']}", "-H", "Content-Type: application/json",
           "-d", json.dumps({"filter": {"must": [{"key": "doc", "match": {"value": "d0"}}]}}))
    b.exec("curl", "-sS", "-X", "PUT", f"http://127.0.0.1:6333/collections/{key}/points?wait=true",
           "-H", f"api-key: {sec['qdrant_api_key']}", "-H", "Content-Type: application/json",
           "-d", json.dumps({"points": [{"id": str(uuid.uuid4()), "vector": {"dense": [0.01] * 384},
                                         "payload": {"document_id": str(uuid.uuid4()), "version": 1, "live": True}}]}))
    b.exec("redis-cli", "--no-auth-warning", "-a", sec["falkordb_password"], "GRAPH.QUERY", key,
           "MATCH (dd:Document {external_id: 'd1'})-[:HAS_CHUNK]->(c:Chunk) WITH c LIMIT 1 DETACH DELETE c")
    b.exec("redis-cli", "--no-auth-warning", "-a", sec["falkordb_password"], "GRAPH.QUERY", key,
           "CREATE (:Document {id: 'not-in-the-catalog'})")
    assert len(check(b)["problems"]) >= 4

    report = d._call("POST", "/v1/admin/reconcile", {"dry_run": True})
    f = report["totals"]
    assert f["missing_vectors"] >= 1 and f["orphan_vectors"] == 1 and f["graph_mismatch"] >= 1
    assert f["orphan_graph"] == 1
    assert len(check(b)["problems"]) >= 4  # dry run changed nothing

    d._call("POST", "/v1/admin/reconcile", {"dry_run": False})
    consistent(b)
    assert d._call("POST", "/v1/admin/reconcile", {"dry_run": True})["issues"] == 0
    assert llm_calls(llm) == calls  # the graph was rebuilt from the catalog, not re-extracted
    assert kb.query("Ravi Kumar Chennai plant audit", k=3)


def test_a_lost_vector_collection_is_rebuilt(env):
    b, _ = env
    d = db(b)
    ensure(d, "lost")
    kb = d.collection("lost")
    kb.add_text("only", "The Osprey turbine needs inspection every 400 hours.")
    kb.wait_ready("only")
    sec = json.loads(b.exec("cat", "/swsdb/data/swsdb/secrets.json"))
    b.exec("curl", "-sS", "-X", "DELETE", "http://127.0.0.1:6333/collections/sw_chaos__lost",
           "-H", f"api-key: {sec['qdrant_api_key']}")
    assert d._call("POST", "/v1/admin/reconcile", {"dry_run": False})["totals"]["missing_vector_collection"] == 1
    consistent(b)
    assert kb.query("Osprey turbine inspection", k=1)[0]["document"] == "only"


def test_a_server_crash_mid_ingest_resumes(env):
    b, _ = env
    d = db(b)
    ensure(d, "crash")
    kb = d.collection("crash")
    for i in range(5):
        kb.add_text(f"c{i}", f"Crash test document {i}. " + "Filler sentence about logistics. " * 200)
    b.exec("pkill", "-9", "-f", "swsdb_server$", user="root")  # the API server and its workers, mid-work
    b.wait_ready(timeout=120)
    for i in range(5):
        kb.wait_ready(f"c{i}", timeout=180)
    consistent(b)


DISRUPTIONS = ["kill:postgres", "kill:qdrant", "kill:redis-server", "kill:server",
               "pause:svc-qdrant", "pause:svc-falkordb"]


def disrupt(b: Box, what: str) -> None:
    kind, target = what.split(":")
    if kind == "kill":
        pattern = "swsdb_server$" if target == "server" else None
        if pattern:
            b.exec("pkill", "-9", "-f", pattern, user="root")
        else:
            b.exec("pkill", "-9", "-o", "-x", target, user="root")
    else:
        svc(b, "down", target)
        time.sleep(random.uniform(2, 6))
        svc(b, "up", target)


def test_chaos(env):
    """Random uploads, updates and deletes while engines and the server are killed or paused; after every round all
    three stores must agree with the catalog, and deleted documents must never be returned."""
    b, _ = env
    rounds = int(os.environ.get("CHAOS_ROUNDS", "3"))
    rng = random.Random(int(os.environ.get("CHAOS_SEED", "7")))
    d = db(b)
    ensure(d, "chaos")
    kb = d.collection("chaos")
    alive: set[str] = set()
    deleted: set[str] = set()
    log = []
    for rnd in range(rounds):
        actions = []
        for _ in range(rng.randint(4, 8)):
            if alive and rng.random() < 0.3:
                doc = rng.choice(sorted(alive))
                actions.append(("delete", doc))
            else:
                doc = f"r{rng.randint(0, 15)}"
                actions.append(("put", doc))
        what = rng.choice(DISRUPTIONS)
        at = rng.randint(0, len(actions) - 1)
        for i, (op, doc) in enumerate(actions):
            if i == at:
                try:
                    disrupt(b, what)
                except AssertionError:
                    pass  # the target may already be restarting
            try:
                if op == "put":
                    words = " ".join(rng.choice(["Pune", "Lisbon", "Nora Diaz", "Atlas Freight", "turbine",
                                                 "invoice", "audit", "warehouse"]) for _ in range(rng.randint(40, 400)))
                    kb.add_text(doc, f"# {doc}\n\nRound {rnd}. {words}")
                    alive.add(doc)
                    deleted.discard(doc)
                else:
                    kb.delete_document(doc)
                    alive.discard(doc)
                    deleted.add(doc)
            except SwsDBError:
                pass  # refused or unknown during the disruption: the catalog decides what happened
        b.wait_ready(timeout=120)
        report = consistent(b)
        for doc in deleted:  # acknowledged deletes stay deleted
            with pytest.raises(SwsDBError):
                kb.document(doc)
        for doc in alive:
            try:
                assert kb.document(doc)["status"] in ("ready", "failed")
            except SwsDBError:
                pytest.fail(f"{doc} was acknowledged but is missing")
        hits = kb.query("warehouse audit invoice", k=20)
        assert not {h["document"] for h in hits} & deleted
        log.append(f"round {rnd}: {what}, {len(actions)} changes, {report['documents']} documents consistent")
        print(log[-1], flush=True)
    print("\n" + "\n".join(log))
