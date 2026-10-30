"""Answers with citations and the MCP server (HTTP at /mcp and /mcp/rw, and `swsdb mcp` over stdio)."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh
from test_documents import FAKE_LLM

pytestmark = pytest.mark.image

ADMIN, READ = "agents-admin-key", "agents-read-key"


@pytest.fixture(scope="module")
def box():
    tag = uuid.uuid4().hex[:8]
    net, vol, llm = f"swsdb-net-{tag}", f"swsdb-agents-{tag}", f"swsdb-llm-{tag}"
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", llm, "--network", net, "--network-alias", "llm",
       "python:3.12-slim-trixie", "python", "-c", FAKE_LLM)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_READ_ONLY_API_KEY": READ, "SWSDB_LLM_URL": "http://llm:8000/v1",
                  "SWSDB_LLM_MODEL": "stand-in"}, extra=["--network", net])
    b.start()
    try:
        b.wait_ready()
        db = client(b)
        kb = db.create_collection("kb", documents=True)
        kb.add_text("refunds", "# Refunds\n\nRefunds are issued within five business days. Store credit is instant.",
                    title="Refund policy")
        kb.add_text("shipping", "Orders ship from the Rotterdam warehouse within 24 hours.", title="Shipping")
        kb.add_text("salaries", "The salary band for engineers is Grade Nine.", readers=["group:hr"])
        for d in ("refunds", "shipping", "salaries"):
            kb.wait_ready(d, timeout=300)
        yield b
    finally:
        b.remove()
        sh("docker", "rm", "-f", llm, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def client(b: Box, key: str = ADMIN) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{b.port}", api_key=key, timeout=180)


# ------------------------------------------------------------------ answers


def test_a_generated_answer_cites_only_real_passages(box):
    r = client(box).collection("kb").answer("How long do refunds take?", k=3)
    assert r["mode"] == "generated" and r["model"] == "stand-in", r
    assert "five business days" in r["answer"] and "[1]" in r["answer"] and "[9]" not in r["answer"]
    assert r["cited"] == [1] and r["passages"][0]["cited"] and r["passages"][0]["document"] == "refunds"


def test_an_extractive_answer_needs_no_model(box):
    r = client(box).collection("kb").answer("Where do orders ship from?", generate=False)
    assert r["mode"] == "extractive" and r["model"] is None
    assert r["answer"] == "Orders ship from the Rotterdam warehouse within 24 hours. [1]"
    assert r["passages"][0]["document"] == "shipping"
    nothing = client(box).collection("kb").answer("What is the capital of Peru?", generate=False)
    assert nothing["cited"] == [] and "don't answer" in nothing["answer"]


def test_answers_only_use_what_the_caller_may_read(box):
    ro = client(box, READ).collection("kb")
    r = ro.answer("What is the salary band for engineers?", generate=False)
    assert all(p["document"] != "salaries" for p in r["passages"]) and "Grade Nine" not in r["answer"]
    hr = client(box).collection("kb").answer("What is the salary band for engineers?", principals=["group:hr"],
                                             generate=False)
    assert "Grade Nine" in hr["answer"]
    with pytest.raises(SwsDBError) as e:
        ro.answer("x", principals=["group:hr"])
    assert e.value.status == 403
    assert ro.text("refunds")["text"].startswith("# Refunds")
    with pytest.raises(SwsDBError) as e:
        ro.text("salaries")  # exists, but this key may not read it: indistinguishable from missing
    assert e.value.status == 404


# ------------------------------------------------------------------ MCP


def mcp_http(b: Box, path: str, key: str, steps):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with httpx.AsyncClient(headers={"api-key": key}, timeout=180) as http, \
                streamable_http_client(f"http://127.0.0.1:{b.port}{path}", http_client=http) as (r, w, _), \
                ClientSession(r, w) as s:
            await s.initialize()
            return await steps(s)
    return asyncio.run(go())


def text_of(result) -> str:
    assert not result.isError, result.content
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


def test_mcp_over_http_read_only(box):
    async def steps(s):
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"search", "ask", "get_document", "list_collections", "recall", "sql_query", "graph_query"} <= names
        assert not names & {"remember", "add_document", "delete_document", "forget"}
        found = text_of(await s.call_tool("search", {"collection": "kb", "query": "refund five days"}))
        answer = text_of(await s.call_tool("ask", {"collection": "kb", "question": "How long do refunds take?"}))
        doc = text_of(await s.call_tool("get_document", {"collection": "kb", "id": "shipping"}))
        hidden = await s.call_tool("get_document", {"collection": "kb", "id": "salaries"})
        colls = text_of(await s.call_tool("list_collections", {}))
        return found, answer, doc, hidden, colls

    found, answer, doc, hidden, colls = mcp_http(box, "/mcp", READ, steps)
    assert "[1] document refunds" in found and "five business days" in found
    assert "five business days" in answer and "Sources:" in answer and "refunds" in answer
    assert "Rotterdam" in doc
    assert hidden.isError and "404" in text_of_error(hidden)
    assert '"kb"' in colls or "kb" in colls


def text_of_error(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


def test_mcp_read_write_gives_agents_memory(box):
    async def steps(s):
        r = text_of(await s.call_tool("remember", {"text": "The user prefers invoices in euros, sent on the 1st.",
                                                    "tags": ["billing"]}))
        recalled = text_of(await s.call_tool("recall", {"query": "which currency for invoices?"}))
        tagged = text_of(await s.call_tool("recall", {"query": "invoices", "tags": ["other"]}))
        added = text_of(await s.call_tool("add_document", {"collection": "notes", "id": "n1",
                                                            "text": "Standup moves to 10:30 from Monday."}))
        searched = text_of(await s.call_tool("search", {"collection": "notes", "query": "when is standup"}))
        return r, recalled, tagged, added, searched

    r, recalled, tagged, added, searched = mcp_http(box, "/mcp/rw", ADMIN, steps)
    assert '"remembered": true' in r.lower() or "remembered" in r
    assert "euros" in recalled
    assert "euros" not in tagged
    assert "ready" in added and "10:30" in searched


def test_mcp_endpoints_refuse_missing_and_weak_keys(box):
    import httpx
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    h = {"accept": "application/json, text/event-stream"}
    url = f"http://127.0.0.1:{box.port}"
    assert httpx.post(f"{url}/mcp", json=init, headers=h).status_code == 401
    assert httpx.post(f"{url}/mcp/rw", json=init, headers={**h, "api-key": READ}).status_code == 403
    assert httpx.post(f"{url}/mcp", json=init, headers={**h, "api-key": READ}).status_code == 200


def test_swsdb_mcp_over_stdio(box):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "swsdb.cli", "mcp"],
                                   env={**os.environ, "SWSDB_URL": f"http://127.0.0.1:{box.port}",
                                        "SWSDB_API_KEY": READ})

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            out = text_of(await s.call_tool("ask", {"collection": "kb", "question": "Where do orders ship from?"}))
            return names, out

    names, out = asyncio.run(go())
    assert "remember" not in names and "Rotterdam" in out


# ------------------------------------------------------------------ the quickstart's CLI


def cli(b: Box, *args: str) -> str:
    import subprocess
    env = {**os.environ, "SWSDB_URL": f"http://127.0.0.1:{b.port}", "SWSDB_API_KEY": ADMIN, "PYTHONUTF8": "1"}
    p = subprocess.run([sys.executable, "-m", "swsdb.cli", *args], capture_output=True, text=True, env=env,
                       timeout=900, check=False)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_the_quickstart_commands(box):
    from pathlib import Path
    demo = cli(box, "demo")
    assert demo.count("Sources:") >= 3 and "project-orion" in demo and "[1]" in demo
    # without a model the multi-hop answer is assembled from both documents, the second found through the graph
    hop = client(box).collection("kestrel").answer("Which office does the lead of Project Orion work from?",
                                                   k=4, generate=False)
    assert "Lisbon" in hop["answer"], hop
    assert {p["document"] for p in hop["passages"] if p["cited"]} >= {"person-priya-nair"}
    handbook = Path(__file__).resolve().parents[2] / "examples" / "handbook"
    out = cli(box, "ingest", "hb", str(handbook))
    assert "30 of 30 documents searchable in hb" in out
    again = cli(box, "ingest", "hb", str(handbook))
    assert again.count("unchanged") == 30
    asked = cli(box, "ask", "hb", "How many vacation days do employees get?")
    assert "26" in asked and "leave-policy.md" in asked
    found = cli(box, "search", "hb", "laptop replacement", "-k", "3")
    assert found.startswith("[1] hardware.md")
