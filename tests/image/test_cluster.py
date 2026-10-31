"""Two swsdb servers on the same engines (deploy/compose/docker-compose.ha.yml): either one serves any request, they
share the ingestion work and can take it over from each other, key changes reach both at once, and a snapshot taken
on one holds the other's writes until it is done."""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

import pytest
import test_container
from swsdb import SwsDB, SwsDBError
from test_container import free_port, sh

pytestmark = pytest.mark.image

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    project = f"swsdb-ha-{uuid.uuid4().hex[:6]}"
    ports = free_port(), free_port()
    env = tmp_path_factory.mktemp("compose") / ".env"
    admin = secrets.token_urlsafe(24)
    env.write_text("\n".join([
        f"SWSDB_API_KEY={admin}", f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
        f"QDRANT_API_KEY={secrets.token_urlsafe(24)}", f"FALKORDB_PASSWORD={secrets.token_hex(24)}",
        f"SWSDB_IMAGE={test_container.IMAGE}", f"SWSDB_PORT={ports[0]}", f"SWSDB_PORT_2={ports[1]}"]) + "\n",
        encoding="utf-8")
    base = ["docker", "compose", "-f", str(COMPOSE / "docker-compose.yml"), "-f",
            str(COMPOSE / "docker-compose.ha.yml"), "-p", project, "--env-file", str(env)]
    sh(*base, "up", "-d", timeout=600)
    try:
        dbs = [SwsDB(f"http://127.0.0.1:{p}", api_key=admin, timeout=300) for p in ports]
        deadline = time.time() + 240
        while not all(db.ready() for db in dbs):
            assert time.time() < deadline, sh(*base, "logs", "--tail", "80", check=False)
            time.sleep(2)
        yield {"a": dbs[0], "b": dbs[1], "names": (f"{project}-swsdb-1", f"{project}-swsdb-2-1"), "base": base,
               "admin": admin}
    finally:
        sh(*base, "down", "-v", timeout=300, check=False)


def wait_for(fn, timeout: float = 60, every: float = 0.5):
    deadline = time.time() + timeout
    while True:
        try:
            if v := fn():
                return v
        except SwsDBError:
            pass
        assert time.time() < deadline, "timed out"
        time.sleep(every)


def test_both_servers_see_each_other_and_one_leads(pair):
    def two():
        sts = [pair[s].status()["cluster"] for s in ("a", "b")]
        return sts if all(len(c["servers"]) == 2 for c in sts) else None

    a, b = wait_for(two)
    assert {a["server"], b["server"]} == {"swsdb-1", "swsdb-2"}
    assert a["files"] == b["files"] == "postgres" and "warning" not in a
    assert wait_for(lambda: [pair[s].status()["cluster"]["leader"] for s in ("a", "b")].count(True) == 1)


def test_an_upload_to_one_server_is_served_by_the_other(pair):
    a, b = pair["a"], pair["b"]
    kb = a.create_collection("kb", documents=True)
    kb.add_text("refunds", "Refunds are issued within five business days. Priya Nair approves them.")
    b.collection("kb").wait_ready("refunds", timeout=300)
    assert b.collection("kb").query("how long do refunds take", k=1)[0]["document"] == "refunds"
    assert "five business days" in b.collection("kb").answer("How long do refunds take?")["answer"]


def test_the_other_server_takes_over_queued_work_when_one_stops(pair):
    """Server a accepts uploads and stops at once: b processes them from Postgres (it can't read a's volume)."""
    a, b = pair["a"], pair["b"]
    kb = a.collection("kb")
    # more than a can index before it stops (it indexes quickly on fast machines), so some are left for b
    docs = {f"note-{i}": f"Note {i}: the Kestrel {i} drone ships from depot {i * 7}." for i in range(150)}
    for doc, text in docs.items():
        kb.add_text(doc, text)
    sh("docker", "stop", "-t", "2", pair["names"][0], timeout=60)
    try:
        for doc in docs:
            b.collection("kb").wait_ready(doc, timeout=300)
        assert b.collection("kb").query("the Kestrel 5 drone ships from depot 35", k=1)[0]["document"] == "note-5"
        done_by_b = sh("docker", "logs", pair["names"][1], check=False).count("ready default/kb/note-")
        assert done_by_b >= 1, "server b processed none of the uploads a accepted"
        assert wait_for(lambda: b.status()["cluster"]["leader"], timeout=60)  # and b runs the schedule now
        assert b.reconcile()["issues"] == 0
    finally:
        sh("docker", "start", pair["names"][0], timeout=60)
        wait_for(a.ready, timeout=120)


def test_key_changes_reach_both_servers(pair):
    a, b = pair["a"], pair["b"]
    made = a.create_key("read", "shared-key")
    on_b = SwsDB(b.url, api_key=made["key"])
    assert on_b.status()["role"] == "read"
    on_a = SwsDB(a.url, api_key=made["key"])
    assert on_a.status()["role"] == "read"  # a caches it now
    b.revoke_key(made["id"])
    t0 = time.time()

    def refused() -> bool:
        try:
            on_a.status()
        except SwsDBError as e:
            return e.status == 401
        return False

    assert wait_for(refused, timeout=10, every=0.1)
    assert time.time() - t0 < 5  # told through Postgres, not by a cache expiring


def test_a_snapshot_on_one_server_holds_the_other_s_writes(pair):
    a, b = pair["a"], pair["b"]
    raw = b.create_collection("raw", vector_size=3)
    raw.upsert([{"id": 1, "vector": [1, 0, 0]}])
    kb_b = b.collection("kb")
    for i in range(6):
        kb_b.add_text(f"burst-{i}", f"Burst document {i} about the Harrier {i} engine.")
    snap = a.create_snapshot("ha")  # waits for b's changes in flight, keeps new ones out meanwhile
    for i in range(6):
        kb_b.wait_ready(f"burst-{i}", timeout=300)
    raw.upsert([{"id": 2, "vector": [0, 1, 0]}])  # after the snapshot: not in it
    out = a.restore_snapshot(snap["id"], backup_first=False)
    assert out["reconcile"]["issues"] == 0, out  # the stores were copied at the same point as the catalog
    # b forgot what it cached of the replaced database and keeps serving
    assert wait_for(lambda: b.collection("kb").query("how long do refunds take", k=1)[0]["document"] == "refunds",
                    timeout=60)
    assert {p["id"] for p in b.collection("raw").get([1, 2])} == {1}
    assert wait_for(lambda: b.reconcile()["issues"] == 0, timeout=120)
    assert b.collection("kb").cypher("RETURN 1", read_only=True)["rows"] == [[1]]
