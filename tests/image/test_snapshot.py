"""Snapshots and restore across all engines: a round trip in place, a restore on another machine, a snapshot taken
while documents are being written, and refusing snapshots this swsdb can't open."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN = "snapshot-admin-key"


def start(tag: str) -> tuple[Box, str]:
    vol = f"swsdb-snap-{tag}-{uuid.uuid4().hex[:6]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN}).start()
    b.wait_ready()
    return b, vol


@pytest.fixture(scope="module")
def pair():
    """Two containers: `a` holds the data, `b` is a fresh machine to restore onto."""
    made = []
    try:
        made.append(start("a"))
        made.append(start("b"))
        yield made[0][0], made[1][0]
    finally:
        for b, vol in made:
            b.remove()
            sh("docker", "volume", "rm", "-f", vol, check=False)


def client(box: Box, key: str = ADMIN, tenant: str | None = None) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{box.port}", api_key=key, tenant=tenant, timeout=300)


def state(box: Box, key: str = ADMIN) -> dict:
    """Everything a restore must bring back, from every engine."""
    db = client(box)
    kb = db.collection("kb")
    docs = sorted((d["id"], d["status"], d["version"]) for d in kb.documents(limit=1000)["documents"])
    hits = {h["document"] for h in kb.query("refund parcel courier", k=10, graph_hops=0)}
    return {
        "docs": docs,
        "hits": hits,
        "raw": sorted(p["id"] for p in db.collection("raw").search([1, 0, 0], limit=10)),
        "graph": db.collection("notes").cypher("MATCH (n:Note) RETURN n.body ORDER BY n.body", read_only=True)["rows"],
        "kb_graph_chunks": kb.cypher("MATCH (c:Chunk) RETURN count(c)", read_only=True)["rows"][0][0],
        "sql": db.sql("SELECT body FROM memo ORDER BY body")["rows"],
        "tenant_sql": client(box, tenant="acme").sql("SELECT body FROM memo ORDER BY body")["rows"],
        "evalsets": [s["name"] for s in db._call("GET", "/v1/collections/kb/eval/sets")["sets"]],
    }


def seed(box: Box) -> str:
    db = client(box)
    kb = db.create_collection("kb", documents=True)
    kb.add_text("refunds", "Refunds go back to the original card within five business days.")
    kb.add_text("shipping", "Parcels leave the Rotterdam warehouse by courier within a day.")
    kb.add_text("old", "This refund note is outdated and will be deleted after the snapshot.")
    for d in ("refunds", "shipping", "old"):
        kb.wait_ready(d, timeout=300)
    kb.put_eval_set("faq", [{"query": "How long do refunds take?", "relevant": ["refunds"]}])
    db.create_collection("raw", vector_size=3).upsert([{"id": "v1", "vector": [1, 0, 0]},
                                                       {"id": "v2", "vector": [0, 1, 0]}])
    db.create_collection("notes", graph=True).cypher("CREATE (:Note {body: 'kept'})")
    db.sql("CREATE TABLE memo (body text)")
    db.sql("INSERT INTO memo VALUES ('default memo')")
    acme = client(box, tenant="acme")
    acme.sql("CREATE TABLE memo (body text)")
    acme.sql("INSERT INTO memo VALUES ('acme memo')")
    return db.create_key("read", "acme reader", tenant="acme")["key"]


def test_round_trip_in_place(pair):
    a, _ = pair
    acme_key = seed(a)
    db = client(a)
    before = state(a)
    snap = db.create_snapshot("round-trip")
    assert snap["documents"] == 3 and len(snap["vector_collections"]) == 2 and len(snap["graphs"]) == 3
    assert snap["migrations"] >= 5 and snap["swsdb_version"]

    # change everything after the snapshot
    kb = db.collection("kb")
    kb.delete_document("old")
    kb.add_text("new", "A new refund rule written after the snapshot: parcels by courier only.")
    kb.wait_ready("new", timeout=300)
    db.collection("raw").delete(["v2"])
    db.collection("notes").cypher("CREATE (:Note {body: 'added later'})")
    db.sql("DROP TABLE memo")
    client(a, tenant="acme").sql("INSERT INTO memo VALUES ('acme later')")
    db.revoke_key(next(k["id"] for k in db.keys(all_tenants=True) if k["name"] == "acme reader"))
    assert state_safe(a) != before

    r = db.restore_snapshot(snap["id"])
    assert r["restored"] == snap["id"] and r["backup"]
    assert r["reconcile"] == {"issues": 0, "totals": {}} or r["reconcile"]["issues"] == 0
    after = state(a)
    assert after == before
    assert "new" not in after["hits"]
    assert client(a, acme_key).status()["tenant"] == "acme"  # the key revoked after the snapshot is back
    # the server keeps working normally after a restore
    kb.add_text("after", "Written after the restore: refunds by courier.")
    assert kb.wait_ready("after", timeout=300)["status"] == "ready"
    assert {s["id"] for s in db.snapshots()} >= {snap["id"], r["backup"]}
    assert db.reconcile()["issues"] == 0


def state_safe(box: Box) -> dict | str:
    try:
        return state(box)
    except SwsDBError as e:
        return f"error {e.status}"


def test_restore_on_another_machine(pair, tmp_path):
    a, b = pair
    src = client(a)
    snap = next(s for s in src.snapshots() if s.get("name") == "round-trip")
    path = src.download_snapshot(snap["id"], tmp_path / "s.tar")
    assert path.stat().st_size == snap["bytes"]
    dst = client(b)
    up = dst.upload_snapshot(path)
    assert up["id"] == "s" and up["documents"] == 3
    r = dst.restore_snapshot("s", backup_first=False)
    assert r["backup"] is None and r["reconcile"]["issues"] == 0
    restored = state(b)
    assert restored["docs"] == sorted([("old", "ready", 1), ("refunds", "ready", 1), ("shipping", "ready", 1)])
    assert restored["sql"] == [["default memo"]] and restored["tenant_sql"] == [["acme memo"]]
    assert restored["graph"] == [["kept"]] and restored["raw"] == ["v1", "v2"]
    # a different machine: its own engine secrets, the same data and keys
    assert b.secret("qdrant_api_key") != a.secret("qdrant_api_key")
    reader = next(k for k in dst.keys(all_tenants=True) if k["name"] == "acme reader")
    assert reader["tenant"] == "acme"
    assert client(b, tenant="acme").sql("SELECT current_user")["rows"] == [["swsdb_t_acme"]]


def test_snapshot_taken_while_documents_are_written(pair, tmp_path):
    a, b = pair
    db = client(a)
    col = db.create_collection("busy", documents=True)
    stop = threading.Event()
    written: list[str] = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                col.add_text(f"d{i}", f"Busy document number {i} about parcels and refunds.")
                written.append(f"d{i}")
            except SwsDBError:
                pass
            i += 1
            time.sleep(0.05)

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(2)
    snap = db.create_snapshot("busy")
    time.sleep(1)
    stop.set()
    t.join()
    assert written
    dst = client(b)
    sid = f"busy-{uuid.uuid4().hex[:6]}"
    dst.upload_snapshot(db.download_snapshot(snap["id"], tmp_path / "busy.tar"), sid)
    r = dst.restore_snapshot(sid, backup_first=False)
    assert r["reconcile"]["issues"] == 0  # every store was copied at the same point
    busy = dst.collection("busy")
    docs = busy.documents(limit=1000)["documents"]
    assert docs and all(d["status"] in ("ready", "queued", "processing") for d in docs)
    for d in docs:  # what was still waiting at snapshot time is finished after the restore
        assert busy.wait_ready(d["id"], timeout=600)["status"] == "ready"
    assert dst.reconcile()["issues"] == 0


def test_incompatible_snapshots_are_refused_before_anything_changes(pair, tmp_path):
    a, _ = pair
    db = client(a)
    snap = db.snapshots()[0]
    path = db.download_snapshot(snap["id"], tmp_path / "x.tar")
    bad = tmp_path / "newer.tar"
    with tarfile.open(path) as src, tarfile.open(bad, "w") as dst:
        for m in src.getmembers():
            f = src.extractfile(m) if m.isfile() else None
            if m.name == "manifest.json":
                manifest = json.load(f)
                manifest["migrations"] = 999
                data = json.dumps(manifest).encode()
                m.size = len(data)
                f = io.BytesIO(data)
            dst.addfile(m, f)
    before = state(a)
    db.upload_snapshot(bad, "from-the-future")
    with pytest.raises(SwsDBError) as e:
        db.restore_snapshot("from-the-future")
    assert e.value.status == 400 and "newer" in e.value.message
    assert state(a) == before
    with pytest.raises(SwsDBError) as e:
        db.upload_snapshot(tmp_path / "x.tar", "../escape")
    assert e.value.status in (400, 404, 422)
    db.delete_snapshot("from-the-future")


def test_only_the_environment_admin_key_handles_snapshots(pair):
    a, _ = pair
    db = client(a)
    tenant_admin = client(a, db.create_key("admin", "acme admin", tenant="acme")["key"])
    for call in (tenant_admin.snapshots, lambda: tenant_admin.create_snapshot("x"),
                 lambda: tenant_admin.restore_snapshot(db.snapshots()[0]["id"])):
        with pytest.raises(SwsDBError) as e:
            call()
        assert e.value.status == 403


def test_admin_cli_inside_the_container(pair):
    a, _ = pair
    out = a.exec("swsdb-admin", "snapshot", "create", "--name", "from-cron")
    assert "snapshot swsdb-" in out and "from-cron" in out
    assert "from-cron" in a.exec("swsdb-admin", "snapshot", "list")
