"""Security review, as tests against a real container: tenant isolation on every read path, the SQL and Cypher
pass-through (sandbox and injection), document permissions, API keys, secrets handling and TLS.

Two tenants hold look-alike data with a marker each (ALPHA-7731 for acme, OMEGA-9902 for globex). A tenant's key
must never see the other's marker, whatever it calls.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh
from test_documents import FAKE_LLM

pytestmark = pytest.mark.image

ADMIN, READ = "root-admin-key-from-a-file", "root-read-key-from-a-file"
ACME, GLOBEX = "ALPHA-7731", "OMEGA-9902"


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    tag = uuid.uuid4().hex[:8]
    net, vol, llm = f"swsdb-net-{tag}", f"swsdb-sec-{tag}", f"swsdb-llm-{tag}"
    secrets = tmp_path_factory.mktemp("secrets")
    for name, value in (("admin", ADMIN), ("read", READ)):
        (secrets / name).write_text(value + "\n", encoding="utf-8")
        os.chmod(secrets / name, 0o644)  # readable by the container's swsdb user on Linux hosts
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", llm, "--network", net, "--network-alias", "llm",
       "python:3.12-slim-trixie", "python", "-c", FAKE_LLM)
    b = Box(vol, {"SWSDB_API_KEY_FILE": "/run/secrets/admin", "SWSDB_READ_ONLY_API_KEY_FILE": "/run/secrets/read",
                  "SWSDB_LLM_URL": "http://llm:8000/v1", "SWSDB_LLM_MODEL": "stand-in"},
            extra=["--network", net, "-v", f"{secrets}:/run/secrets:ro"])
    b.start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "rm", "-f", llm, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def client(box: Box, key: str | None = ADMIN, tenant: str | None = None) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{box.port}", api_key=key or "", tenant=tenant, timeout=120)


def status_of(fn) -> int:
    try:
        fn()
    except SwsDBError as e:
        return e.status
    return 200


@pytest.fixture(scope="module")
def world(box):
    """Two tenants with the same collection, entity and document names, and a key per tenant and role."""
    root = client(box)
    keys = {(t, r): root.create_key(r, f"{t}-{r}", tenant=t)["key"]
            for t in ("acme", "globex") for r in ("admin", "read")}
    for t, marker in (("acme", ACME), ("globex", GLOBEX)):
        db = client(box, keys[(t, "admin")])
        kb = db.create_collection("kb", documents=True)
        kb.add_text("codes", f"The launch code {marker} is held by Maria Lopez at Initech Labs.")
        kb.add_text("private", f"Maria Lopez keeps the backup code {marker}-B in the vault.", readers=["group:ops"])
        kb.add_text("public", "Initech Labs publishes its office hours every Monday.")
        for d in ("codes", "private", "public"):
            kb.wait_ready(d, timeout=300)
        db.create_collection("raw", vector_size=3).upsert([{"id": "p1", "vector": [1, 0, 0], "payload": {"m": marker}}])
        db.collection("raw").cypher(f"CREATE (:Note {{code: '{marker}'}})")
        db.sql("CREATE TABLE IF NOT EXISTS notes (body text)")
        db.sql("INSERT INTO notes VALUES (%s)", [marker])
        kb.put_eval_set("codes", [{"query": "Who holds the launch code?", "relevant": ["codes"]}])
    return keys


# ------------------------------------------------------------------ keys


def test_keys_come_from_files_and_never_show_in_inspect_logs_or_responses(box, world):
    inspect = sh("docker", "inspect", box.name)
    assert ADMIN not in inspect and READ not in inspect
    assert client(box).status()["role"] == "admin" and client(box, READ).status()["role"] == "read"
    status = json.dumps(client(box).status())
    logs = box.logs()
    for secret in (ADMIN, READ, *world.values(), box.secret("postgres_password"), box.secret("qdrant_api_key"),
                   box.secret("falkordb_password")):
        assert secret not in logs and secret not in status
    r = status_of(lambda: client(box, "swsdb_0123456789_" + "x" * 40).status())
    assert r == 401
    stored = box.psql("SELECT encode(hash, 'hex') FROM swsdb.api_keys LIMIT 1")
    assert len(stored) == 64 and all(k not in stored for k in world.values())  # only hashes are kept
    perms = box.exec("stat", "-c", "%a %U", "/swsdb/data/swsdb/secrets.json", "/swsdb/data/swsdb").split("\n")
    assert perms[0].strip() == "600 swsdb" and perms[1].strip() == "700 swsdb"


def test_a_tenant_key_reaches_only_its_tenant(box, world):
    acme = client(box, world[("acme", "admin")])
    assert acme.status()["tenant"] == "acme"
    assert {c["name"] for c in acme.collections()} == {"kb", "raw"}
    sneaky = client(box, world[("acme", "admin")], tenant="globex")  # acme's key, asking for globex
    for call in (sneaky.collections, lambda: sneaky.collection("kb").query("code"), lambda: sneaky.sql("SELECT 1"),
                 lambda: sneaky.collection("raw").cypher("RETURN 1", read_only=True), lambda: sneaky.create_key("read"),
                 lambda: sneaky.collection("kb").document("codes"), sneaky.reconcile):
        assert status_of(call) == 403
    assert status_of(lambda: acme.create_key("read", tenant="globex")) == 403
    assert status_of(lambda: acme.keys(all_tenants=True)) == 403
    assert status_of(lambda: acme.reconcile(all_tenants=True)) == 403
    req = urllib.request.Request(f"http://127.0.0.1:{box.port}/metrics", headers={"api-key": world[("acme", "admin")]})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 - fixed local URL
    assert e.value.code == 403
    assert {k["tenant"] for k in acme.keys()} == {"acme"}  # it lists its own tenant's keys only


def test_no_read_path_leaks_the_other_tenant(box, world):
    """Every read path with acme's admin key: GLOBEX's marker must never come back."""
    acme = client(box, world[("acme", "admin")])
    kb, raw = acme.collection("kb"), acme.collection("raw")
    seen: list[str] = []

    def collect(x):
        seen.append(json.dumps(x))
        return x

    for mode in ("hybrid", "dense", "sparse"):
        for hops in (0, 1, 2):
            collect(kb.query(f"launch code {GLOBEX} Maria Lopez Initech", k=20, mode=mode, graph_hops=hops,
                             principals=["group:ops"]))
    collect(kb.info())
    collect(kb.documents(limit=1000))
    for d in ("codes", "private", "public"):
        collect(kb.document(d))
        collect(kb.diagnose(d))
    collect(acme._call("GET", "/v1/collections/kb/eval/sets"))
    collect(kb.evaluate("codes"))
    collect(acme._call("GET", "/v1/collections/kb/eval/runs"))
    collect(raw.search([1, 0, 0], limit=100))
    collect(raw.get(["p1"]))
    collect(raw.cypher("MATCH (n) RETURN n", read_only=True))
    collect(kb.cypher("MATCH (n) RETURN n LIMIT 500", read_only=True))
    collect(acme.sql("SELECT * FROM notes"))
    collect(acme.sql("SELECT table_schema, table_name FROM information_schema.tables"))
    collect(acme.reconcile())
    collect(acme.collections())
    text = "\n".join(seen)
    assert ACME in text  # the probes did reach acme's own data
    assert GLOBEX not in text
    assert status_of(lambda: acme.collection("globex-only").info()) == 404


def test_answers_and_mcp_tools_do_not_leak_the_other_tenant(box, world):
    from test_agents import mcp_http

    acme = client(box, world[("acme", "admin")])
    kb = acme.collection("kb")
    seen = [json.dumps(kb.answer(f"Who holds launch code {GLOBEX}?", k=10, graph_hops=hops, generate=gen,
                                 principals=["group:ops"]))
            for hops in (0, 2) for gen in (True, False)]
    seen.append(json.dumps(kb.text("codes")))

    async def steps(s):
        out = []
        calls = [("list_collections", {}), ("search", {"collection": "kb", "query": f"launch code {GLOBEX}", "k": 20,
                                                       "graph_hops": 2}),
                 ("ask", {"collection": "kb", "question": f"Who holds {GLOBEX}?"}),
                 ("get_document", {"collection": "kb", "id": "codes"}), ("list_documents", {"collection": "kb"}),
                 ("sql_query", {"query": "SELECT * FROM notes"}),
                 ("graph_query", {"collection": "raw", "query": "MATCH (n) RETURN n"}),
                 ("graph_query", {"collection": "kb", "query": "MATCH (n) RETURN n LIMIT 200"}),
                 ("recall", {"query": GLOBEX})]
        for name, args in calls:
            r = await s.call_tool(name, args)
            out.append("\n".join(c.text for c in r.content if getattr(c, "text", None)))
        return out

    seen += mcp_http(box, "/mcp", world[("acme", "admin")], steps)
    seen += mcp_http(box, "/mcp/rw", world[("acme", "admin")], steps)
    text = "\n".join(seen)
    assert ACME in text and GLOBEX not in text
    # an MCP session asking for the other tenant gets the API's refusal for every tool
    refused = mcp_http_tenant(box, world[("acme", "admin")], "globex")
    assert refused.isError and "403" in refused.content[0].text


def mcp_http_tenant(box, key, tenant):
    import asyncio

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with httpx.AsyncClient(headers={"api-key": key, "swsdb-tenant": tenant}) as http, \
                streamable_http_client(f"http://127.0.0.1:{box.port}/mcp", http_client=http) as (r, w, _), \
                ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool("list_collections", {})
    return asyncio.run(go())


def test_a_revoked_key_stops_at_once(box, world):
    root = client(box)
    made = root.create_key("read", "short-lived", tenant="acme")
    c = client(box, made["key"])
    assert c.status()["tenant"] == "acme"
    root.revoke_key(made["id"])
    assert status_of(c.status) == 401
    assert status_of(lambda: root.revoke_key(made["id"])) == 404


def test_read_only_keys_cannot_skip_document_readers(box, world):
    ro = client(box, world[("acme", "read")])
    kb = ro.collection("kb")
    hits = kb.query("backup code vault Maria Lopez", k=20, graph_hops=2)
    assert "private" not in {h["document"] for h in hits} and f"{ACME}-B" not in json.dumps(hits)
    assert status_of(lambda: kb.query("x", principals=["group:ops"])) == 403
    for call in (lambda: ro._call("POST", "/v1/collections/kb/search", {"vector": [0.1] * 384}),
                 lambda: ro._call("POST", "/v1/collections/kb/points/get", {"ids": ["x"]}),
                 lambda: kb.cypher("MATCH (c:Chunk) RETURN c.id", read_only=True)):
        assert status_of(call) == 403
    # raw collections hold no document permissions: reading them with a read-only key is fine, writing is not
    assert ro.collection("raw").search([1, 0, 0])[0]["payload"]["m"] == ACME
    assert status_of(lambda: ro.collection("raw").upsert([{"id": 2, "vector": [0, 1, 0]}])) == 403
    assert status_of(lambda: ro.sql("CREATE TABLE x (a int)")) == 403


# ------------------------------------------------------------------ SQL pass-through


def test_sql_runs_in_a_sandbox_per_tenant(box, world):
    acme, globex = client(box, world[("acme", "admin")]), client(box, world[("globex", "admin")])
    default = client(box)
    assert acme.sql("SELECT current_user, current_schema()")["rows"] == [["swsdb_t_acme", "swsdb_t_acme"]]
    assert default.sql("SELECT current_user")["rows"] == [["swsdb_app"]]
    default.sql("CREATE TABLE IF NOT EXISTS default_notes (body text)")
    denied = [
        "SELECT * FROM swsdb_t_globex.notes",  # another tenant's schema
        "SELECT * FROM public.default_notes",  # the default tenant's schema
        "SELECT * FROM swsdb.documents",  # swsdb's catalog
        "SELECT * FROM swsdb.api_keys",
        "SELECT * FROM pg_authid",  # password hashes
        "SET ROLE swsdb_t_globex",
        "SET ROLE swsdb_app",
        "SET SESSION AUTHORIZATION swsdb",
        "CREATE ROLE intruder",
        "CREATE EXTENSION dblink",
        "COPY notes TO PROGRAM 'id'",
        "SELECT pg_read_file('/swsdb/data/swsdb/secrets.json')",
        "SELECT pg_ls_dir('/swsdb/data')",
        "CREATE SCHEMA elsewhere",
        "SELECT 1; SELECT 2",  # one statement per call
        "COMMIT; SELECT 1",
    ]
    for stmt in denied:
        assert status_of(lambda s=stmt: acme.sql(s)) == 400, stmt
    assert status_of(lambda: default.sql("SELECT * FROM swsdb_t_acme.notes")) == 400
    assert status_of(lambda: acme.sql("INSERT INTO notes VALUES ('x')", read_only=True)) == 400
    assert status_of(lambda: acme.sql("SELECT pg_sleep(5)", timeout_s=0.5)) == 400
    # nothing a statement does to its session survives into the next one
    acme.sql("SET search_path = pg_catalog")
    assert acme.sql("SELECT current_schema()")["rows"] == [["swsdb_t_acme"]]
    acme.sql("CREATE TEMP TABLE scratch (a int)")
    assert status_of(lambda: acme.sql("SELECT * FROM scratch")) == 400
    # other tenants' table names stay out of the catalog views a tenant can list
    names = json.dumps(acme.sql("SELECT table_schema, table_name FROM information_schema.tables "
                                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")["rows"])
    assert "swsdb_t_globex" not in names and "default_notes" not in names and "api_keys" not in names
    assert globex.sql("SELECT body FROM notes")["rows"] == [[GLOBEX]]


def test_sql_parameters_are_data_not_code(box, world):
    acme = client(box, world[("acme", "admin")])
    evil = "x'); DROP TABLE notes; --"
    assert acme.sql("SELECT %s AS v", [evil])["rows"] == [[evil]]
    assert acme.sql("SELECT %(v)s AS v", {"v": evil})["rows"] == [[evil]]
    assert acme.sql("SELECT count(*) FROM notes")["rows"][0][0] >= 1  # still there


# ------------------------------------------------------------------ Cypher pass-through


def test_cypher_parameters_are_data_and_load_csv_is_off(box, world):
    acme = client(box, world[("acme", "admin")]).collection("raw")
    evil = "x'}) MATCH (n) DETACH DELETE n //"
    assert acme.cypher("RETURN $v", {"v": evil}, read_only=True)["rows"] == [[evil]]
    assert acme.cypher("RETURN $v", {"v": 'quote " and \\ backslash'}, read_only=True)["rows"] == \
        [['quote " and \\ backslash']]
    assert acme.cypher("MATCH (n:Note) RETURN count(n)", read_only=True)["rows"] == [[1]]
    for bad in ({"v) RETURN 1 //": 1}, {"1v": 1}, {"v": {"a b": 1}}):
        assert status_of(lambda p=bad: acme.cypher("RETURN 1", p, read_only=True)) == 400
    for q in ("LOAD CSV FROM 'https://example.com/' AS r RETURN r",
              "LOAD/**/CSV FROM 'http://169.254.169.254/latest/meta-data/' AS r RETURN r",
              "load csv from 'file:///swsdb/data/swsdb/secrets.json' as r return r"):
        assert status_of(lambda q=q: acme.cypher(q)) == 403
    assert status_of(lambda: acme.cypher("CREATE (:X)", read_only=True)) == 400  # read-only means read-only


# ------------------------------------------------------------------ names and ids


def test_names_and_ids_cannot_escape(box, world):
    root = client(box)
    for tenant in ("acme__x", "../etc", "ACME", "a b", "x'--"):
        assert status_of(lambda t=tenant: client(box, tenant=t).collections()) == 422
    for name in ("a__b", "..", "A", "a%20b"):
        assert status_of(lambda n=name: root._call("GET", f"/v1/collections/{n}")) in (404, 422)
    kb = client(box, tenant="acme").collection("kb")
    kb.add_text("../../../../etc/cron.d/x", "A document whose id looks like a path.")
    kb.wait_ready("../../../../etc/cron.d/x", timeout=300)
    assert box.exec("sh", "-c", "test ! -e /etc/cron.d/x && echo clean").strip() == "clean"
    files = box.exec("find", "/swsdb/data/files", "-type", "f")
    assert ".." not in files and "cron" not in files


# ------------------------------------------------------------------ TLS


def test_tls_serves_https_and_the_healthcheck_follows(volume, boxes, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(tmp_path / "key.pem"), "-out", str(tmp_path / "cert.pem")],
                   check=True, capture_output=True)
    for f in ("key.pem", "cert.pem"):
        os.chmod(tmp_path / f, 0o644)
    b = Box(volume, {"SWSDB_TLS_CERT_FILE": "/tls/cert.pem", "SWSDB_TLS_KEY_FILE": "/tls/key.pem",
                     "SWSDB_API_KEY": ADMIN}, extra=["-v", f"{tmp_path}:/tls:ro"])
    boxes.append(b.start())
    ctx = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
    deadline = time.time() + 120
    while True:
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:{b.port}/readyz", context=ctx, timeout=5) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        assert time.time() < deadline, b.logs()[-3000:]
        time.sleep(1)
    with pytest.raises((urllib.error.URLError, OSError, ConnectionError)):  # plain HTTP is not served
        urllib.request.urlopen(f"http://127.0.0.1:{b.port}/readyz", timeout=5)
    assert "swsdb: ready" in b.exec("swsdb-admin", "status")
    deadline = time.time() + 60
    while (health := sh("docker", "inspect", "-f", "{{.State.Health.Status}}", b.name).strip()) != "healthy":
        assert time.time() < deadline, health
        time.sleep(2)


def test_a_half_tls_configuration_refuses_to_start(volume, boxes):
    b = Box(volume, {"SWSDB_TLS_CERT_FILE": "/nope/cert.pem", "SWSDB_TLS_KEY_FILE": "/nope/key.pem"})
    boxes.append(b.start())
    deadline = time.time() + 60
    while "TLS file" not in b.logs():
        assert time.time() < deadline, b.logs()[-3000:]
        time.sleep(1)
