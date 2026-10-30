"""Phase 5 end to end: diagnosis, evaluation, metrics, the console and its graph queries."""

from __future__ import annotations

import json
import re
import time
import urllib.request
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh
from test_documents import FAKE_LLM

pytestmark = pytest.mark.image

ADMIN, READ = "admin-obs-key", "read-obs-key"


@pytest.fixture(scope="module")
def box():
    tag = uuid.uuid4().hex[:8]
    net, vol, llm = f"swsdb-net-{tag}", f"swsdb-obs-{tag}", f"swsdb-llm-{tag}"
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", llm, "--network", net, "--network-alias", "llm",
       "python:3.12-slim-trixie", "python", "-c", FAKE_LLM)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_READ_ONLY_API_KEY": READ, "SWSDB_LLM_URL": "http://llm:8000/v1",
                  "SWSDB_LLM_MODEL": "stand-in"}, extra=["--network", net])
    b.start()
    try:
        b.wait_ready()
        kb = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, tenant="obs").create_collection("kb", documents=True)
        kb.add_text("refunds", "# Refunds\n\nRefunds from Acme Retail are issued within five business days.")
        kb.add_text("shipping", "# Shipping\n\nOrders from Acme Retail ship from Pune within two days.")
        kb.add_text("careers", "# Careers\n\nWe hire engineers in Lisbon and Bangalore.")
        for d in ("refunds", "shipping", "careers"):
            kb.wait_ready(d)
        yield b
    finally:
        b.remove()
        sh("docker", "rm", "-f", llm, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def kb(b: Box, key: str = ADMIN):
    return SwsDB(f"http://127.0.0.1:{b.port}", api_key=key, tenant="obs").collection("kb")


def raw(b: Box, path: str, key: str | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{b.port}{path}", headers={"api-key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - local test server
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_diagnosis_explains_healthy_failed_and_drifted_documents(box):
    c = kb(box)
    ok = c.diagnose("refunds")
    assert ok["verdict"] == "searchable" and ok["vectors"]["live"] == ok["document"]["chunks"]
    assert ok["graph"]["chunks"] == ok["document"]["chunks"] and ok["graph"]["entities"] >= 1

    status, _, _ = raw(box, "/readyz")
    assert status == 200
    files = {"file": ("scan.pdf", b"%PDF-1.4 not really", "application/pdf")}
    r = c.db._http.post("/v1/collections/kb/documents", files=files, data={"id": "scan"})
    assert r.status_code == 202
    for _ in range(60):
        d = c.diagnose("scan")
        if d["document"]["status"] == "failed":
            break
        time.sleep(0.5)
    assert d["verdict"] == "not searchable" and "upload it again" in " ".join(d["reasons"])

    sec = json.loads(box.exec("cat", "/swsdb/data/swsdb/secrets.json"))
    box.exec("curl", "-sS", "-X", "POST", "http://127.0.0.1:6333/collections/sw_obs__kb/points/delete?wait=true",
             "-H", f"api-key: {sec['qdrant_api_key']}", "-H", "Content-Type: application/json",
             "-d", json.dumps({"filter": {"must": [{"key": "doc", "match": {"value": "shipping"}}]}}))
    d = c.diagnose("shipping")
    assert d["verdict"] == "not fully searchable" and any("doctor --repair" in x for x in d["reasons"])
    c.db.reconcile(repair=True)
    for _ in range(60):
        if c.diagnose("shipping")["verdict"] == "searchable":
            break
        time.sleep(0.5)
    assert c.diagnose("shipping")["verdict"] == "searchable"


def test_evaluation_compares_configurations_and_keeps_history(box):
    c = kb(box)
    c.put_eval_set("faq", [
        {"query": "how long until I get my money back", "relevant": ["refunds"], "answer": "five business days"},
        {"query": "where do orders ship from", "relevant": ["shipping"]},
        {"query": "open jobs", "relevant": ["careers"]},
    ])
    run = c.evaluate("faq", k=1)
    names = [r["config"]["name"] for r in run["results"]]
    assert names == ["hybrid", "dense", "keyword", "hybrid+graph"]
    by = {r["config"]["name"]: r["metrics"] for r in run["results"]}
    assert by["hybrid"]["recall"] == 1.0 and by["hybrid"]["answer_found"] == 1.0
    assert by["keyword"]["recall"] < by["hybrid"]["recall"]  # paraphrases defeat pure keyword search
    assert by["hybrid+graph"]["evidence"] >= by["hybrid"]["evidence"]
    assert run["best"] in ("hybrid", "dense", "hybrid+graph")
    custom = c.evaluate("faq", k=2, configs=[{"name": "dense-k2", "mode": "dense"}])
    assert [r["config"]["name"] for r in custom["results"]] == ["dense-k2"]
    history = c.db._call("GET", "/v1/collections/kb/eval/runs")["runs"]
    assert len(history) >= 2 and history[0]["id"] > history[1]["id"]
    with pytest.raises(SwsDBError) as e:
        kb(box, READ).evaluate("faq")
    assert e.value.status == 403


def test_metrics_need_a_key_and_report_what_matters(box):
    kb(box).query("refund timeline", k=2)
    assert raw(box, "/metrics")[0] == 401
    status, headers, body = raw(box, "/metrics", READ)
    text = body.decode()
    assert status == 200 and headers["content-type"].startswith("text/plain")
    for name in ("swsdb_engine_up", "swsdb_documents", "swsdb_outbox_pending", "swsdb_query_seconds_bucket",
                 "swsdb_ingest_documents_total", "swsdb_http_requests_total"):
        assert name in text, name
    assert re.search(r'swsdb_engine_up\{engine="qdrant"\} 1\.0', text)
    assert re.search(r'swsdb_ingest_documents_total\{result="failed"\} [1-9]', text)
    # route templates, not raw paths: document ids never become metric labels
    leaks = [ln for ln in text.splitlines() if "refunds" in ln]
    assert not leaks, leaks
    assert "/v1/collections/{name}/query" in text


def test_console_is_served_locked_down(box):
    status, headers, body = raw(box, "/console/")
    assert status == 200 and b"swsdb console" in body
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert headers["cache-control"] == "no-cache"  # an upgraded server never serves a stale console
    for asset in ("app.js", "style.css"):
        assert raw(box, f"/console/{asset}")[0] == 200
    js = raw(box, "/console/app.js")[2].decode()
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|\.outerHTML\s*=", js)  # API values rendered as text


def test_the_console_graph_queries_run_on_real_entities(box):
    c = kb(box)
    top = c.cypher("MATCH (e:Entity) WHERE toLower(e.name) CONTAINS toLower($q) "
                   "OPTIONAL MATCH (e)<-[:MENTIONS]-(c:Chunk) "
                   "RETURN e.key, e.name, e.type, count(c) AS mentions ORDER BY mentions DESC LIMIT 50",
                   {"q": "acme"}, read_only=True)
    assert top["rows"] and top["rows"][0][1] == "Acme Retail" and top["rows"][0][3] == 2
    key = top["rows"][0][0]
    docs = c.cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document) "
                    "RETURN d.external_id, count(c) AS n ORDER BY n DESC LIMIT 20", {"k": key}, read_only=True)
    assert {r[0] for r in docs["rows"]} == {"refunds", "shipping"}
    co = c.cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(:Chunk)-[:MENTIONS]->(o:Entity) WHERE o.key <> $k "
                  "RETURN o.key, o.name, count(*) AS n ORDER BY n DESC LIMIT 24", {"k": key}, read_only=True)
    assert "columns" in co
