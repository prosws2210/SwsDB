"""Engines in their own containers (deploy/compose): swsdb runs only its server and uses Postgres, Qdrant and FalkorDB
at their URLs, with everything else (tenant SQL roles, snapshots, documents) working the same."""

from __future__ import annotations

import os
import secrets
import time
import uuid
from pathlib import Path

import pytest
import test_container
from swsdb import SwsDB, SwsDBError
from test_container import free_port, sh

pytestmark = pytest.mark.image

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "docker-compose.yml"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    project = f"swsdb-ext-{uuid.uuid4().hex[:6]}"
    port = free_port()
    env = tmp_path_factory.mktemp("compose") / ".env"
    admin = secrets.token_urlsafe(24)
    env.write_text("\n".join([
        f"SWSDB_API_KEY={admin}", f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
        f"QDRANT_API_KEY={secrets.token_urlsafe(24)}", f"FALKORDB_PASSWORD={secrets.token_hex(24)}",
        f"SWSDB_IMAGE={test_container.IMAGE}", f"SWSDB_PORT={port}"]) + "\n", encoding="utf-8")
    base = ["docker", "compose", "-f", str(COMPOSE), "-p", project, "--env-file", str(env)]
    sh(*base, "up", "-d", timeout=600)
    try:
        db = SwsDB(f"http://127.0.0.1:{port}", api_key=admin, timeout=300)
        deadline = time.time() + 240
        while not db.ready():
            assert time.time() < deadline, sh(*base, "logs", "--tail", "80", check=False)
            time.sleep(2)
        yield db, f"{project}-swsdb-1"
    finally:
        sh(*base, "down", "-v", timeout=300, check=False)


def test_the_server_uses_the_engine_containers(stack):
    db, container = stack
    status = db.status()
    assert {c: e["state"] for c, e in status["engines"].items()} == {"sql": "ready", "vector": "ready",
                                                                     "graph": "ready"}
    assert status["engines"]["sql"]["version"].startswith("17")
    procs = sh("docker", "exec", container, "ps", "-eo", "comm=")
    assert not {"postgres", "qdrant", "redis-server"} & set(procs.split())  # nothing local but the server
    assert "external: sql, vector, graph" in sh("docker", "logs", container, check=False)


def test_documents_sql_tenants_and_snapshots_work_the_same(stack):
    db, _ = stack
    kb = db.create_collection("kb", documents=True)
    kb.add_text("refunds", "# Refunds\n\nRefunds are issued within five business days. Priya Nair approves them.")
    kb.add_text("people", "Priya Nair works from the Lisbon office.")
    kb.wait_ready("refunds")
    kb.wait_ready("people")
    assert kb.query("how long do refunds take", k=1)[0]["document"] == "refunds"
    answered = kb.answer("How long do refunds take?", generate=False)
    assert "five business days" in answered["answer"] and answered["cited"] == [1]
    assert kb.cypher("MATCH (e:Entity) RETURN count(e)", read_only=True)["rows"][0][0] >= 1

    db.sql("CREATE TABLE notes (body text)")
    db.sql("INSERT INTO notes VALUES ('default note')")
    acme = SwsDB(db.url, api_key=db.create_key("admin", tenant="acme")["key"])
    assert acme.sql("SELECT current_user")["rows"] == [["swsdb_t_acme"]]  # a password-authenticated sandbox role
    acme.sql("CREATE TABLE notes (body text)")
    with pytest.raises(SwsDBError):
        acme.sql("SELECT * FROM public.notes")

    snap = db.create_snapshot("external")
    kb.delete_document("people")
    db.sql("DROP TABLE notes")
    r = db.restore_snapshot(snap["id"], backup_first=False)
    assert r["reconcile"]["issues"] == 0
    assert {d["id"] for d in kb.documents()["documents"]} == {"refunds", "people"}
    assert db.sql("SELECT body FROM notes")["rows"] == [["default note"]]
    assert acme.sql("SELECT count(*) FROM notes")["rows"] == [[0]]  # the role logs in again after the restore


@pytest.mark.skipif(os.environ.get("CI") is None and not COMPOSE.exists(), reason="no compose file")
def test_compose_file_is_valid():
    out = sh("docker", "compose", "-f", str(COMPOSE), "config", "--quiet", check=False)
    assert "SWSDB_API_KEY" in out  # refuses to start without the required values
