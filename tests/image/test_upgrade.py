"""Upgrades: start version N, write data, upgrade to N+1 on the same volume, verify; and refuse to go backwards.

Version N is SWSDB_PREVIOUS_IMAGE (default swsdb/swsdb:prev). Build it from the previous release, e.g.
    git archive v0.1.0 | docker build -f docker/Dockerfile -t swsdb/swsdb:prev -
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
import test_container
from swsdb import SwsDB
from test_container import Box

pytestmark = pytest.mark.image

PREVIOUS = os.environ.get("SWSDB_PREVIOUS_IMAGE", "swsdb/swsdb:prev")
ADMIN = "upgrade-admin-key"


def have(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode == 0


def run(image: str, volume: str) -> Box:
    saved = test_container.IMAGE
    test_container.IMAGE = image
    try:
        b = Box(volume, {"SWSDB_API_KEY": ADMIN}).start()
    finally:
        test_container.IMAGE = saved
    b.wait_ready(timeout=180)
    return b


def client(b: Box) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, tenant="default", timeout=300)


@pytest.mark.skipif(not have(PREVIOUS), reason=f"no previous image {PREVIOUS}")
def test_upgrade_keeps_every_engines_data(volume, boxes):
    old = run(PREVIOUS, volume)
    boxes.append(old)
    db = client(old)
    kb = db.create_collection("kb", documents=True)
    kb.add_text("refunds", "Refunds go back to the original card within five business days.")
    kb.add_text("pending", "Written just before the upgrade: parcels ship by courier.")
    kb.wait_ready("refunds", timeout=300)
    kb.put_eval_set("faq", [{"query": "How long do refunds take?", "relevant": ["refunds"]}])
    db.create_collection("raw", vector_size=3).upsert([{"id": "v1", "vector": [1, 0, 0]}])
    db.collection("raw").cypher("CREATE (:Note {body: 'kept across versions'})")
    db.sql("CREATE TABLE memo (body text)")
    db.sql("INSERT INTO memo VALUES ('kept across versions')")
    old.stop()
    old.remove()

    new = run(test_container.IMAGE, volume)
    boxes.append(new)
    db = client(new)
    from swsdb_server.stores import MIGRATIONS
    assert new.psql("SELECT max(version) FROM swsdb.migrations") == str(len(MIGRATIONS))
    kb = db.collection("kb")
    assert kb.wait_ready("pending", timeout=300)["status"] == "ready"  # queued before, finished after
    assert {h["document"] for h in kb.query("how long do refunds take", k=3)} >= {"refunds"}
    assert [p["id"] for p in db.collection("raw").search([1, 0, 0])] == ["v1"]
    assert db.collection("raw").cypher("MATCH (n:Note) RETURN n.body", read_only=True)["rows"] == \
        [["kept across versions"]]
    assert db.sql("SELECT body FROM memo")["rows"] == [["kept across versions"]]
    assert kb.evaluate("faq")["results"]
    assert db.reconcile()["issues"] == 0
    # the new version's features work on the old data
    key = db.create_key("read", "after upgrade", tenant="acme")
    assert SwsDB(f"http://127.0.0.1:{new.port}", api_key=key["key"]).status()["tenant"] == "acme"
    # and the database as it was before the schema changed was kept for a rollback
    backups = new.exec("ls", "/swsdb/data/backups").split()
    assert any(b.startswith("before-schema-") and b.endswith(".dump") for b in backups), backups


def test_a_database_from_a_newer_swsdb_is_refused(volume, boxes):
    b = Box(volume, {"SWSDB_API_KEY": ADMIN}).start()
    boxes.append(b)
    b.wait_ready()
    b.psql("INSERT INTO swsdb.migrations (version) VALUES (999)")
    b.stop()
    b.remove()
    again = Box(volume, {"SWSDB_API_KEY": ADMIN}).start()
    boxes.append(again)
    deadline = time.time() + 90
    while True:
        try:
            code, body = again.get("/readyz")
        except OSError:  # still starting
            code, body = 0, {}
        error = (body.get("documents") or {}).get("error", "")
        if "newer swsdb" in error:
            break
        assert time.time() < deadline, (code, body)
        time.sleep(1)
    assert code == 503  # it never reports ready on data it must not write to


def test_newer_on_disk_layout_is_refused(volume, boxes):
    b = Box(volume, {"SWSDB_API_KEY": ADMIN}).start()
    boxes.append(b)
    b.wait_ready()
    b.exec("sh", "-c", "sed -i 's/\"format\": [0-9]*/\"format\": 99/' /swsdb/data/swsdb/layout.json", user="root")
    b.stop()
    b.remove()
    again = Box(volume, {"SWSDB_API_KEY": ADMIN}).start()
    boxes.append(again)
    deadline = time.time() + 60
    while "newer than this image" not in again.logs():
        assert time.time() < deadline, again.logs()[-3000:]
        time.sleep(1)
