"""Documents end to end: ingest, hybrid retrieval, graph expansion, permissions, versions, deletion, a 200-page PDF.

Entity extraction uses a small stand-in for an OpenAI-compatible model, run in a sibling container: it reports every
run of Capitalised Words as an entity, so results are deterministic and need no API key.
"""

from __future__ import annotations

import textwrap
import time
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN, READ = "admin-docs-key", "read-docs-key"

FAKE_LLM = r'''
import json, re
from http.server import BaseHTTPRequestHandler, HTTPServer
CALLS = [0]
class H(BaseHTTPRequestHandler):
    def do_GET(self):  # how many extraction calls were made
        data = json.dumps({"calls": CALLS[0]}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if body["messages"][0]["role"] == "system":  # a cited answer: the first passage's first line, cited
            block = body["messages"][-1]["content"].split("[1] ", 1)[-1].split("\n\n[2] ", 1)[0]
            lines = [x for x in block.split("\n")[1:] if x.strip() and not x.startswith("#")]
            data = json.dumps({"choices": [{"message": {"content": lines[0].strip() + " [1] [9]"}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        CALLS[0] += 1
        text = body["messages"][-1]["content"].split("Text:\n", 1)[-1]
        names = sorted(set(re.findall(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)+\b", text)))
        out = {"entities": [{"name": n, "type": "Other"} for n in names],
               "relations": [{"source": a, "target": b, "type": "RELATED_TO"} for a, b in zip(names, names[1:])]}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):
        pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
'''


@pytest.fixture(scope="module")
def box():
    tag = uuid.uuid4().hex[:8]
    net, vol, llm = f"swsdb-net-{tag}", f"swsdb-docs-{tag}", f"swsdb-llm-{tag}"
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", llm, "--network", net, "--network-alias", "llm",
       "python:3.12-slim-trixie", "python", "-c", FAKE_LLM)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_READ_ONLY_API_KEY": READ, "SWSDB_LLM_URL": "http://llm:8000/v1",
                  "SWSDB_LLM_MODEL": "stand-in"}, extra=["--network", net])
    b.start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "rm", "-f", llm, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def client(box: Box, key: str = ADMIN) -> SwsDB:
    return SwsDB(f"http://127.0.0.1:{box.port}", api_key=key, tenant="acme", timeout=300)


def wait_ready(db: SwsDB, collection: str, doc: str, version: int | None = None, timeout: float = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = db._call("GET", f"/v1/collections/{collection}/documents/{doc}")
        if d["status"] == "failed":
            raise AssertionError(f"{doc} failed: {d['error']}")
        if d["status"] == "ready" and (version is None or d["version"] == version):
            return d
        time.sleep(0.5)
    raise AssertionError(f"{doc} not ready after {timeout}s: {d}")


def add(db: SwsDB, collection: str, doc: str, text: str, **kw) -> dict:
    return db._call("POST", f"/v1/collections/{collection}/documents/text", {"id": doc, "text": text, **kw})


def query(db: SwsDB, collection: str, q: str, **kw) -> dict:
    return db._call("POST", f"/v1/collections/{collection}/query", {"query": q, **kw})


def test_ingest_and_hybrid_query(box):
    db = client(box)
    db._call("PUT", "/v1/collections/handbook", {"documents": True})
    add(db, "handbook", "policies", textwrap.dedent("""\
        # Returns
        ## Refunds
        Refunds are issued within five business days after the returned item reaches our warehouse.
        ## Shipping
        Orders ship from our Pune warehouse within two days.
        # Errors
        If checkout fails with error ERR-4471 the payment provider rejected the card.
        """), title="Customer policies", metadata={"team": "support"})
    d = wait_ready(db, "handbook", "policies")
    assert d["chunks"] >= 3 and d["entities"] >= 0
    hits = query(db, "handbook", "how long until I get my money back", k=3)["hits"]
    assert hits[0]["heading"] == "Returns > Refunds" and hits[0]["document"] == "policies"
    assert hits[0]["title"] == "Customer policies" and hits[0]["metadata"] == {"team": "support"}
    # an exact code only keyword search knows
    assert "ERR-4471" in query(db, "handbook", "ERR-4471", k=1, mode="sparse")["hits"][0]["text"]
    assert "ERR-4471" in query(db, "handbook", "ERR-4471", k=1)["hits"][0]["text"]  # hybrid keeps it
    assert query(db, "handbook", "refund", k=3, where={"team": "sales"})["hits"] == []
    info = db.collection("handbook").info()
    assert info["documents"]["count"] == 1 and info["vector"]["size"] == 384


def test_graph_expansion_finds_what_search_misses(box):
    db = client(box)
    db._call("PUT", "/v1/collections/projects", {"documents": True})
    add(db, "projects", "falcon", "Project Falcon is led by Maria Lopez. Its budget for next year is still open.")
    add(db, "projects", "people", "Before joining us, Maria Lopez spent eight years at Acme Robotics building "
                                  "warehouse automation.")
    add(db, "projects", "noise", "The cafeteria menu changes every Monday. Parking passes renew in January.")
    for doc in ("falcon", "people", "noise"):
        wait_ready(db, "projects", doc)
    q = "Who leads Project Falcon?"
    plain = query(db, "projects", q, k=1, graph_hops=0)["hits"]
    assert [h["document"] for h in plain] == ["falcon"]
    with_graph = query(db, "projects", q, k=1, graph_hops=1)
    extra = [h for h in with_graph["hits"] if h["via"] == "graph"]
    assert with_graph["graph"]["used"] and any(h["document"] == "people" for h in extra), with_graph
    hop = next(h for h in extra if h["document"] == "people")
    assert "Maria Lopez" in hop["graph_path"][0] and "Acme Robotics" in hop["text"]


def test_permissions_hold_in_search_and_in_the_graph(box):
    db = client(box)
    db._call("PUT", "/v1/collections/hr", {"documents": True})
    add(db, "hr", "salaries", "Maria Lopez earns a salary band of Grade Nine.", readers=["group:hr"])
    add(db, "hr", "org", "Maria Lopez leads the robotics group.", readers=["*"])
    wait_ready(db, "hr", "salaries")
    wait_ready(db, "hr", "org")
    q = "Maria Lopez salary"
    as_user = query(db, "hr", q, k=5, principals=["user:7"], graph_hops=1)["hits"]
    assert {h["document"] for h in as_user} == {"org"}  # not in search, not through the shared entity either
    as_hr = query(db, "hr", q, k=5, principals=["user:9", "group:hr"])["hits"]
    assert "salaries" in {h["document"] for h in as_hr}
    ro = client(box, READ)
    assert {h["document"] for h in query(ro, "hr", q, k=5)["hits"]} == {"org"}
    with pytest.raises(SwsDBError) as e:
        query(ro, "hr", q, principals=["group:hr"])
    assert e.value.status == 403


def test_reingest_replaces_the_old_version(box):
    faq = client(box).create_collection("faq", documents=True)  # through the SDK this time
    faq.add_text("hours", "The support desk opens at nine in the morning.")
    assert faq.wait_ready("hours")["version"] == 1
    again = faq.add_text("hours", "The support desk opens at nine in the morning.")
    assert again["unchanged"] is True and again["version"] == 1
    faq.add_text("hours", "The support desk now opens at seven in the morning.")
    d = wait_ready(faq.db, "faq", "hours", 2)
    assert d["version"] == 2
    texts = [h["text"] for h in faq.query("when does the support desk open", k=5)]
    assert texts == ["The support desk now opens at seven in the morning."]
    assert faq.documents()["total"] == 1


def test_delete_removes_it_everywhere(box):
    db = client(box)
    db._call("PUT", "/v1/collections/tmp", {"documents": True})
    add(db, "tmp", "gone", "Zebra Crossing Protocol is retired.")
    wait_ready(db, "tmp", "gone")
    db._call("DELETE", "/v1/collections/tmp/documents/gone")
    # hidden from queries at once (checked against the catalog) ...
    assert query(db, "tmp", "Zebra Crossing Protocol", k=5)["hits"] == []
    # ... and removed from the stores asynchronously, through the outbox
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            db._call("GET", "/v1/collections/tmp/documents/gone")
        except SwsDBError as e:
            assert e.status == 404
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the delete was not applied within 60s")
    graph = db.collection("tmp").cypher("MATCH (n) RETURN count(n)", read_only=True)
    assert graph["rows"] == [[0]]  # document, chunks and its now-unused entities


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one text page per entry (Helvetica, wrapped)."""
    def esc(s: str) -> bytes:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
    objs: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
                              3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, text in enumerate(pages):
        cid, pid = 4 + 2 * i, 5 + 2 * i
        lines = textwrap.wrap(text, 90)
        stream = b"BT /F1 10 Tf 40 760 Td 13 TL " + b" ".join(b"(" + esc(ln) + b") '" for ln in lines) + b" ET"
        objs[cid] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[pid] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                     b"/Resources << /Font << /F1 3 0 R >> >> >>" % cid)
        kids.append(pid)
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out, offsets = b"%PDF-1.4\n", {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref, size = len(out), max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size + b"".join(b"%010d 00000 n \n" % offsets[n]
                                                                  for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return out


def test_a_200_page_pdf_is_ingested_and_cited_by_page(box, tmp_path):
    db = client(box)
    manual = db.create_collection("manual", documents=True, graph=False)
    filler = ("This section of the operations manual describes routine maintenance of the storage systems, "
              "inspection schedules, safety rules for technicians and the records each team keeps. ") * 3
    pages = [f"Chapter {n}. {filler} The access code for storage vault {n} is VAULT{n}X{n * 7}. {filler}"
             for n in range(1, 201)]
    pdf = make_pdf(pages)
    path = tmp_path / "manual.pdf"
    path.write_bytes(pdf)
    t0 = time.time()
    assert manual.upload(path, title="Operations manual")["status"] == "queued"
    d = manual.wait_ready("manual.pdf", timeout=900)
    took = time.time() - t0
    assert d["chunks"] >= 200
    hit = manual.query("access code for storage vault 137", k=3)[0]
    assert hit["page"] == 137 and "VAULT137X959" in hit["text"], hit
    print(f"\n200-page PDF ({len(pdf) // 1024} KB, {d['chunks']} chunks) ingested in {took:.1f}s")


def test_listing_facets_and_activity_for_the_console(box):
    """Paging with search and metadata filters, counts by metadata value (narrowed by filters), recent activity."""
    db = client(box)
    kb = db.create_collection("catalog", documents=True)
    for i in range(12):
        kb.add_text(f"t-{i:02d}", f"Ticket {i}: the export of report {i} is missing rows.", title=f"Export ticket {i}",
                    metadata={"priority": "urgent" if i % 4 == 0 else "low", "team": "data" if i % 2 else "billing"})
    for i in range(12):
        kb.wait_ready(f"t-{i:02d}", timeout=300)
    page = db._call("GET", "/v1/collections/catalog/documents?limit=5&offset=5&sort=id")
    assert page["total"] == 12 and [d["id"] for d in page["documents"]] == [f"t-{i:02d}" for i in range(5, 10)]
    found = db._call("GET", "/v1/collections/catalog/documents?q=TICKET%201")  # title, case-insensitive
    assert {d["id"] for d in found["documents"]} == {"t-01", "t-10", "t-11"}
    urgent = db._call("GET", '/v1/collections/catalog/documents?meta={"priority":"urgent"}')
    assert urgent["total"] == 3 and all(d["metadata"]["priority"] == "urgent" for d in urgent["documents"])
    f = db._call("GET", "/v1/collections/catalog/facets")
    assert f["totals"]["documents"] == 12 and f["status"] == {"ready": 12}
    assert {v["value"]: v["count"] for v in f["fields"]["priority"]["values"]} == {"urgent": 3, "low": 9}
    narrowed = db._call("GET", '/v1/collections/catalog/facets?meta={"priority":"urgent"}')
    assert {v["value"]: v["count"] for v in narrowed["fields"]["team"]["values"]} == {"billing": 3}
    kb.query("missing rows", k=3)
    act = db._call("GET", "/v1/activity?minutes=10")
    assert len(act["minutes"]) == 10 and act["searches"] >= 1 and act["indexed"] >= 12
    assert act["latency_ms"]["p50"] is not None
