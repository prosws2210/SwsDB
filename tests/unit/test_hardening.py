"""Hardening rules that don't need engines: Cypher pass-through checks, tenant roles, keys, secrets from files, TLS."""

import pytest
from fastapi.testclient import TestClient
from test_api import FakeMonitor

from swsdb_server.app import create_app
from swsdb_server.auth import KEY, digest, new_key
from swsdb_server.bootstrap import render_pg_ident
from swsdb_server.settings import Settings, secrets_from_files
from swsdb_server.stores import Forbidden, check_pass_through_cypher, tenant_role

READY = {"sql": "ready", "vector": "ready", "graph": "ready"}


@pytest.mark.parametrize("query", [
    "LOAD CSV FROM 'https://example.com/a.csv' AS row RETURN row",
    "load csv with headers from 'file:///etc/passwd' as r return r",
    "MATCH (n) WITH n LOAD\n\tCSV FROM $u AS r RETURN r",
    "LOAD/* hidden */CSV FROM 'http://169.254.169.254/' AS r RETURN r",
    "LOAD // line comment\nCSV FROM 'x' AS r RETURN r",
])
def test_load_csv_is_refused_however_it_is_written(query):
    with pytest.raises(Forbidden):
        check_pass_through_cypher(query)


@pytest.mark.parametrize("query", [
    "RETURN 'LOAD CSV is mentioned in a string'",
    'MATCH (n {note: "load csv"}) RETURN n',
    "MATCH (`LOAD CSV`) RETURN 1",
    "// LOAD CSV in a comment\nRETURN 1",
    "MATCH (n:Download) RETURN n.csv",
    "RETURN 'it\\'s fine' AS a, 'LOAD CSV' AS b",
])
def test_ordinary_queries_mentioning_it_are_allowed(query):
    check_pass_through_cypher(query)


def test_tenant_roles_are_namespaced_and_fit_postgres():
    assert tenant_role("default") == "swsdb_app"
    assert tenant_role("acme") == "swsdb_t_acme"
    long = "a" * 63
    r = tenant_role(long)
    assert len(r) <= 63 and r.startswith("swsdb_t_") and r != tenant_role("a" * 62 + "b")
    assert "/^swsdb_t_[a-z0-9_-]+$" in render_pg_ident()  # the server may log in as any tenant role, nothing else


def test_managed_keys_have_a_findable_id_and_only_a_hash_is_kept():
    kid, key = new_key()
    m = KEY.match(key)
    assert m and m.group(1) == kid
    assert digest(key) != key.encode() and len(digest(key)) == 32
    assert new_key()[1] != key


def test_managed_keys_need_an_environment_key_first():
    s = Settings(allowed_hosts="testserver")
    with TestClient(create_app(s, FakeMonitor(s, READY), stores={})) as c:
        r = c.post("/v1/keys", json={"role": "read"})
        assert r.status_code == 409 and "SWSDB_API_KEY" in r.json()["message"]


def test_a_tenant_key_header_mismatch_and_metrics_are_refused():
    s = Settings(api_key="root")
    app = create_app(s, FakeMonitor(s, READY), stores={})

    class Keys:
        async def resolve(self, key):
            from swsdb_server.auth import Caller
            return Caller("admin", "acme", "k1") if key == "acme-key" else None

        async def any_active(self):
            return True

    with TestClient(app) as c:
        app.state.keys = Keys()
        h = {"api-key": "acme-key"}
        assert c.get("/v1/status", headers=h).json()["tenant"] == "acme"
        r = c.get("/v1/collections", headers={**h, "swsdb-tenant": "globex"})
        assert r.status_code == 403 and "acme" in r.json()["message"]
        assert c.get("/metrics", headers=h).status_code == 403
        assert c.get("/metrics", headers={"api-key": "root"}).status_code == 200
        assert c.post("/v1/admin/reconcile", json={"all_tenants": True}, headers=h).status_code == 403


def test_secrets_can_come_from_files(tmp_path):
    f = tmp_path / "key"
    f.write_text("from-a-file\n", encoding="utf-8")
    env = {"SWSDB_API_KEY_FILE": str(f), "SWSDB_LLM_API_KEY": "explicit", "SWSDB_LLM_API_KEY_FILE": str(f)}
    secrets_from_files(env)
    assert env["SWSDB_API_KEY"] == "from-a-file"
    assert env["SWSDB_LLM_API_KEY"] == "explicit"  # an explicit variable wins
    with pytest.raises(ValueError, match="can't read"):
        secrets_from_files({"SWSDB_API_KEY_FILE": str(tmp_path / "missing")})


def test_tls_needs_both_files(tmp_path):
    assert not Settings().tls and Settings().local_url == "http://127.0.0.1:6464"
    s = Settings(tls_cert_file=tmp_path / "c.pem", tls_key_file=tmp_path / "k.pem")
    assert s.tls and s.local_url.startswith("https://")
    with pytest.raises(ValueError, match="both"):
        _ = Settings(tls_cert_file=tmp_path / "c.pem").tls


def test_an_open_server_refuses_dns_rebinding_and_cross_site_requests():
    s = Settings()  # no keys: open, for local use
    with TestClient(create_app(s, FakeMonitor(s, READY), stores={}), base_url="http://localhost:6464") as c:
        assert c.get("/v1/status").status_code == 200
        assert c.get("/v1/status", headers={"host": "127.0.0.1:6464"}).status_code == 200
        assert c.get("/v1/status", headers={"host": "[::1]:6464"}).status_code == 200
        r = c.get("/v1/status", headers={"host": "attacker.example:6464"})  # a rebinding page's own domain
        assert r.status_code == 403 and "SWSDB_API_KEY" in r.json()["message"]
        r = c.post("/v1/sql", json={"query": "SELECT 1"}, headers={"origin": "https://attacker.example"})
        assert r.status_code == 403 and "cross-origin" in r.json()["message"]
        assert c.get("/v1/status", headers={"origin": "null"}).status_code == 403
        assert c.get("/v1/status", headers={"origin": "http://localhost:6464"}).status_code == 200  # the console


def test_with_a_key_any_host_name_works_but_cross_site_still_does_not():
    s = Settings(api_key="k", allowed_hosts="app.example")
    h = {"api-key": "k"}
    with TestClient(create_app(s, FakeMonitor(s, READY), stores={}), base_url="http://db.internal:6464") as c:
        assert c.get("/v1/status", headers=h).status_code == 200
        assert c.get("/v1/status", headers={**h, "origin": "https://evil.example"}).status_code == 403
        assert c.get("/v1/status", headers={**h, "origin": "https://app.example"}).status_code == 200


def test_mcp_endpoints_check_the_key_before_any_session():
    s = Settings(api_key="adm", read_only_api_key="ro", allowed_hosts="testserver")
    with TestClient(create_app(s, FakeMonitor(s, READY), stores={})) as c:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        accept = {"accept": "application/json, text/event-stream"}
        assert c.post("/mcp", json=init, headers=accept).status_code == 401
        assert c.post("/mcp/rw", json=init, headers={**accept, "api-key": "ro"}).status_code == 403
        r = c.post("/mcp", json=init, headers={**accept, "api-key": "ro"})
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "swsdb"
        tools = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                       headers={**accept, "api-key": "ro"}).json()["result"]["tools"]
        names = {x["name"] for x in tools}
        assert {"search", "ask", "get_document", "recall", "sql_query"} <= names and "remember" not in names
        rw = c.post("/mcp/rw", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers={**accept, "api-key": "adm"}).json()["result"]["tools"]
        assert {"remember", "add_document", "delete_document", "forget"} <= {x["name"] for x in rw}


def test_sql_store_connects_locally_or_to_an_external_postgres():
    from swsdb_server.stores import SqlStore
    local = SqlStore("/run/swsdb/pg")
    assert "host=/run/swsdb/pg" in local._conninfo(None) and "user=swsdb" in local._conninfo(None)
    assert local.tool_args() == (["-h", "/run/swsdb/pg", "-U", "swsdb", "-d", "swsdb"], {})
    ext = SqlStore(None, dsn="postgresql://owner:pw@db.example.com:6543/app?sslmode=require")
    assert ext.dbname == "app"
    sandbox = ext._conninfo("swsdb_t_acme", "secret")
    assert "user=swsdb_t_acme" in sandbox and "password=secret" in sandbox and "host=db.example.com" in sandbox
    args, env = ext.tool_args()
    assert args == ["-h", "db.example.com", "-p", "6543", "-U", "owner", "-d", "app"]
    assert env == {"PGPASSWORD": "pw", "PGSSLMODE": "require"}  # never on the command line
    assert "dbname=postgres" in ext.admin_conninfo("postgres")


def test_external_engines_are_not_run_locally():
    s = Settings(qdrant_url="http://qdrant:6333", falkordb_url="redis://:pw@falkordb:6379")
    assert s.local_engines == ("sql",) and s.external("vector") and not s.external("sql")
