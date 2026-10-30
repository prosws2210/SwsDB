"""Webhooks against a real receiver: ready, failed and deleted events arrive signed, retries happen while the receiver
is down, and a tenant's hooks see only its own documents."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request
import uuid

import pytest
from swsdb import SwsDB, SwsDBError
from test_container import Box, free_port, sh

pytestmark = pytest.mark.image

ADMIN = "webhook-admin-key"

RECEIVER = r'''
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
GOT = []
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps(GOT).encode()
        self.send_response(200); self.send_header("content-length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        if os.path.exists("/tmp/down"):
            self.send_response(503); self.end_headers(); return
        GOT.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body.decode()})
        self.send_response(204); self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
'''


@pytest.fixture(scope="module")
def env():
    tag = uuid.uuid4().hex[:8]
    net, vol, hook = f"swsdb-net-{tag}", f"swsdb-wh-{tag}", f"swsdb-hook-{tag}"
    port = free_port()
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", hook, "--network", net, "--network-alias", "hook", "-p",
       f"127.0.0.1:{port}:8000", "python:3.12-slim-trixie", "python", "-c", RECEIVER)
    b = Box(vol, {"SWSDB_API_KEY": ADMIN}, extra=["--network", net]).start()
    try:
        b.wait_ready()
        yield b, hook, port
    finally:
        b.remove()
        sh("docker", "rm", "-f", hook, check=False)
        sh("docker", "network", "rm", net, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)


def received(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        return json.loads(r.read())


def wait_for(port: int, pred, timeout: float = 120) -> list[dict]:
    deadline = time.time() + timeout
    while True:
        got = received(port)
        if pred(got):
            return got
        assert time.time() < deadline, got
        time.sleep(0.5)


def test_events_arrive_signed_and_retry_until_delivered(env):
    b, hook, port = env
    db = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, tenant="acme", timeout=120)
    w = db.create_webhook("http://hook:8000/events")
    assert w["secret"] and w["events"] == ["document.deleted", "document.failed", "document.ready"]
    other = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, tenant="globex")
    other.create_collection("kb", documents=True).add_text("secret", "Globex only.")  # no hook in globex

    kb = db.create_collection("kb", documents=True)
    kb.add_text("refunds", "Refunds take five business days.")
    kb.wait_ready("refunds")
    got = wait_for(port, lambda g: any(json.loads(x["body"])["event"] == "document.ready" for x in g))
    ev = next(x for x in got if json.loads(x["body"])["event"] == "document.ready")
    body = json.loads(ev["body"])
    assert body["tenant"] == "acme" and body["document"] == "refunds" and body["version"] == 1
    expected = "sha256=" + hmac.new(w["secret"].encode(), ev["body"].encode(), hashlib.sha256).hexdigest()
    assert ev["headers"]["swsdb-signature"] == expected and ev["headers"]["swsdb-event"] == "document.ready"

    with pytest.raises(SwsDBError):  # an empty file is refused up front; a broken PDF fails while indexing
        kb.add_text("empty", "")
    db._call("POST", "/v1/collections/kb/documents", files={"file": ("broken.pdf", b"%PDF-1.4 not really")})
    wait_for(port, lambda g: any(json.loads(x["body"])["event"] == "document.failed" for x in g))

    # the receiver goes down: deliveries are retried, then succeed
    sh("docker", "exec", hook, "touch", "/tmp/down")  # noqa: S108 - inside the receiver container
    kb.delete_document("refunds")
    time.sleep(6)
    pending = db.webhook_deliveries(w["id"])
    assert any(d["event"] == "document.deleted" and d["state"] == "pending" and d["attempts"] >= 1
               and d["last_status"] == 503 for d in pending), pending
    sh("docker", "exec", hook, "rm", "/tmp/down")  # noqa: S108 - inside the receiver container
    got = wait_for(port, lambda g: any(json.loads(x["body"])["event"] == "document.deleted" for x in g), 180)
    assert all(json.loads(x["body"])["tenant"] == "acme" for x in got)  # nothing from globex
    listed = db.webhooks()[0]
    assert listed["delivered"] >= 3 and "secret" not in listed

    db.ping_webhook(w["id"])
    wait_for(port, lambda g: any(json.loads(x["body"])["event"] == "ping" for x in g))


def test_hooks_cannot_target_the_server_itself(env):
    b, _, _ = env
    db = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN)
    for url in ("http://127.0.0.1:6333/collections", "http://localhost:6464/v1/sql", "http://169.254.169.254/"):
        with pytest.raises(SwsDBError) as e:
            db.create_webhook(url)
        assert e.value.status == 400
    read = SwsDB(f"http://127.0.0.1:{b.port}", api_key=db.create_key("read", tenant="default")["key"])
    with pytest.raises(SwsDBError) as e:
        read.create_webhook("http://hook:8000/")
    assert e.value.status == 403
