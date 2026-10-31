"""The swsdb image as a user runs it: start, readiness, durability, crash recovery, engine selection, shutdown.

Needs Docker and an image (default swsdb/swsdb:dev; set SWSDB_TEST_IMAGE). Run:  pytest -m image tests/image
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.image

IMAGE = os.environ.get("SWSDB_TEST_IMAGE", "swsdb/swsdb:dev")


def sh(*args: str, check: bool = True, timeout: float = 120) -> str:
    p = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({p.returncode}): {p.stdout[-2000:]}{p.stderr[-2000:]}")
    return p.stdout + p.stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shareable(folder) -> str:
    """A host folder the container's swsdb user (uid 10001) can read when it's mounted: on Linux, pytest's temporary
    folders are private to the user running the tests (0700). Returns the folder, for the -v argument."""
    os.chmod(folder, 0o755)  # noqa: S103 - readable test fixtures, nothing secret
    for f in os.listdir(folder):
        os.chmod(os.path.join(folder, f), 0o644)
    return str(folder)


class Box:
    """One swsdb container on a named volume."""

    def __init__(self, volume: str, env: dict[str, str] | None = None, extra: list[str] | None = None):
        self.volume, self.env, self.extra = volume, env or {}, extra or []
        self.name = f"swsdb-test-{uuid.uuid4().hex[:8]}"
        self.port = free_port()

    def start(self) -> Box:
        envs = [a for k, v in self.env.items() for a in ("-e", f"{k}={v}")]
        sh("docker", "run", "-d", "--name", self.name, "-p", f"127.0.0.1:{self.port}:6464",
           "-v", f"{self.volume}:/swsdb/data", *envs, *self.extra, IMAGE)
        return self

    def get(self, path: str, key: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"api-key": key} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - a fixed http://127.0.0.1 URL
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait_ready(self, timeout: float = 90) -> float:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.get("/readyz")[0] == 200:
                    return time.time() - t0
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        raise AssertionError(f"not ready after {timeout}s:\n{self.logs()[-4000:]}")

    def exec(self, *cmd: str, user: str = "swsdb") -> str:
        return sh("docker", "exec", "-u", user, self.name, *cmd)

    def secret(self, key: str) -> str:
        return json.loads(self.exec("cat", "/swsdb/data/swsdb/secrets.json"))[key]

    def psql(self, sql: str) -> str:
        return self.exec("psql", "-h", "/run/swsdb/pg", "-d", "swsdb", "-tAc", sql).strip()

    def qdrant(self, method: str, path: str, body: dict | None = None) -> dict:
        key = self.secret("qdrant_api_key")
        args = ["curl", "-sS", "-X", method, f"http://127.0.0.1:6333{path}", "-H", f"api-key: {key}"]
        if body is not None:
            args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        return json.loads(self.exec(*args))

    def cypher(self, query: str) -> str:
        return self.exec("redis-cli", "--no-auth-warning", "-a", self.secret("falkordb_password"),
                         "GRAPH.QUERY", "t", query)

    def logs(self) -> str:
        return sh("docker", "logs", self.name, check=False)

    def stop(self, timeout: int = 60) -> float:
        t0 = time.time()
        sh("docker", "stop", "-t", str(timeout), self.name, timeout=timeout + 30)
        return time.time() - t0

    def remove(self) -> None:
        sh("docker", "rm", "-f", self.name, check=False)


@pytest.fixture
def volume():
    name = f"swsdb-test-{uuid.uuid4().hex[:8]}"
    yield name
    sh("docker", "volume", "rm", "-f", name, check=False)


@pytest.fixture
def boxes():
    made: list[Box] = []
    yield made
    for b in made:
        b.remove()


def write_one_of_each(b: Box) -> None:
    b.psql("CREATE TABLE notes (id int primary key, body text); INSERT INTO notes VALUES (1, 'kept')")
    assert b.qdrant("PUT", "/collections/c", {"vectors": {"size": 3, "distance": "Cosine"}})["status"] == "ok"
    r = b.qdrant("PUT", "/collections/c/points?wait=true", {"points": [{"id": 1, "vector": [0.1, 0.2, 0.3]}]})
    assert r["status"] == "ok"
    assert "Nodes created: 1" in b.cypher("CREATE (:Note {id: 1, body: 'kept'})")


def assert_one_of_each(b: Box) -> None:
    assert b.psql("SELECT body FROM notes WHERE id = 1") == "kept"
    assert b.qdrant("GET", "/collections/c/points/1")["result"]["id"] == 1
    assert "kept" in b.cypher("MATCH (n:Note {id: 1}) RETURN n.body")


def test_first_start_is_ready_with_all_engines(volume, boxes):
    b = Box(volume).start()
    boxes.append(b)
    took = b.wait_ready()
    code, st = b.get("/v1/status")
    assert code == 200 and st["status"] == "ready"
    assert {c: e["engine"] for c, e in st["engines"].items()} == {"sql": "postgres", "vector": "qdrant",
                                                                  "graph": "falkordb"}
    assert all(e["version"] for e in st["engines"].values())
    assert took < 60, f"first start took {took:.1f}s"
    # Qdrant's own dashboard is served (on the native port, so only inside the container unless exposed)
    assert "<title>UI | Qdrant" in b.exec("curl", "-s", "http://127.0.0.1:6333/dashboard/")
    # beyond localhost inside the container only the API and Postgres listen; Postgres admits only project logins
    # from there (test_projects), and Qdrant and FalkorDB stay inside
    listening = b.exec("cat", "/proc/net/tcp", "/proc/net/tcp6")
    exposed = {int(line.split()[1].split(":")[1], 16) for line in listening.splitlines()[1:]
               if len(line.split()) > 3 and line.split()[3] == "0A"
               and line.split()[1].split(":")[0].strip("0") == ""}
    assert exposed == {6464, 5432}, f"ports bound to all interfaces: {sorted(exposed)}"
    hba = b.exec("sh", "-c", "grep -v '^#' /swsdb/data/postgres/pg_hba.conf", user="root")
    assert {ln.split()[2] for ln in hba.splitlines() if "/0" in ln} == {"+swsdb_projects"}


def test_data_survives_restart_and_a_new_container(volume, boxes):
    b = Box(volume).start()
    boxes.append(b)
    b.wait_ready()
    write_one_of_each(b)
    b.stop()
    logs = b.logs()
    assert "database system is shut down" in logs, "Postgres did not shut down cleanly"
    sh("docker", "start", b.name)
    b.wait_ready()
    assert_one_of_each(b)
    b.remove()
    b2 = Box(volume).start()  # same volume, new container (an upgrade looks like this)
    boxes.append(b2)
    b2.wait_ready()
    assert_one_of_each(b2)


@pytest.mark.parametrize("process", ["postgres", "qdrant", "redis-server"])
def test_a_crashed_engine_is_restarted_and_reported(volume, boxes, process):
    b = Box(volume).start()
    boxes.append(b)
    b.wait_ready()
    write_one_of_each(b)
    b.exec("pkill", "-9", "-o", "-x", process, user="root")  # the oldest match is the engine's main process
    deadline = time.time() + 60
    while time.time() < deadline and b.get("/readyz")[0] == 200:
        time.sleep(0.2)  # may already be back; the restart counter below tells
    b.wait_ready(timeout=90)
    assert_one_of_each(b)
    assert "restarting" in b.logs() or "was killed" in b.logs()


def test_engines_can_be_selected(volume, boxes):
    b = Box(volume, {"SWSDB_ENGINES": "vector"}).start()
    boxes.append(b)
    b.wait_ready()
    _, st = b.get("/v1/status")
    assert list(st["engines"]) == ["vector"]
    procs = b.exec("ps", "-eo", "comm")
    assert "qdrant" in procs and "postgres" not in procs and "redis-server" not in procs


def test_an_unknown_engine_stops_the_container_with_a_clear_error(volume, boxes):
    b = Box(volume, {"SWSDB_ENGINES": "vector,mongo"}).start()
    boxes.append(b)
    deadline = time.time() + 60
    while time.time() < deadline and "running" in sh("docker", "inspect", "-f", "{{.State.Status}}", b.name):
        time.sleep(0.5)
    assert "unknown engine 'mongo'" in b.logs()
    assert "running" not in sh("docker", "inspect", "-f", "{{.State.Status}}", b.name)


def test_api_key_is_enforced(volume, boxes):
    b = Box(volume, {"SWSDB_API_KEY": "admin-secret", "SWSDB_READ_ONLY_API_KEY": "read-secret"}).start()
    boxes.append(b)
    b.wait_ready()
    assert b.get("/v1/status")[0] == 401
    assert b.get("/v1/status", "wrong")[0] == 401
    assert b.get("/v1/status", "admin-secret")[0] == 200
    assert b.get("/v1/status", "read-secret")[0] == 200
    assert b.get("/readyz")[0] == 200  # health stays open for orchestrators


def test_data_from_a_newer_swsdb_is_refused(volume, boxes):
    b = Box(volume).start()
    boxes.append(b)
    b.wait_ready()
    b.exec("sh", "-c", "sed -i 's/\"format\": 1/\"format\": 999/' /swsdb/data/swsdb/layout.json")
    b.stop()
    sh("docker", "start", b.name)
    deadline = time.time() + 60
    while time.time() < deadline and "running" in sh("docker", "inspect", "-f", "{{.State.Status}}", b.name):
        time.sleep(0.5)
    assert "newer than this image" in b.logs()


def wait_exited(b: Box, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and "running" in sh("docker", "inspect", "-f", "{{.State.Status}}", b.name):
        time.sleep(0.5)


def test_a_hard_kill_leaves_nothing_that_blocks_the_next_start(volume, boxes):
    b = Box(volume).start()
    boxes.append(b)
    b.wait_ready()
    write_one_of_each(b)
    sh("docker", "kill", "-s", "KILL", b.name)  # no shutdown at all: like a host crash
    b.remove()
    b2 = Box(volume).start()  # the stale postmaster.pid's PID may belong to another process here
    boxes.append(b2)
    b2.wait_ready()
    assert_one_of_each(b2)


def test_a_second_container_on_the_same_volume_is_refused(volume, boxes):
    b = Box(volume).start()
    boxes.append(b)
    b.wait_ready()
    b2 = Box(volume).start()
    boxes.append(b2)
    wait_exited(b2)
    assert "already used by another running swsdb container" in b2.logs()
    assert b.get("/readyz")[0] == 200  # the first one is untouched


def test_a_mounted_config_file_configures_the_server(volume, boxes, tmp_path):
    (tmp_path / "swsdb.yaml").write_text("engines: [sql, vector]\nrerank: true\nreconcile_interval_s: 0\n",
                                         encoding="utf-8")
    b = Box(volume, extra=["-v", f"{shareable(tmp_path)}:/swsdb/config:ro"]).start()
    boxes.append(b)
    b.wait_ready()
    assert set(b.get("/readyz")[1]["engines"]) == {"sql", "vector"}  # no graph engine started
    from swsdb import SwsDB
    kb = SwsDB(f"http://127.0.0.1:{b.port}").create_collection("kb", documents=True)
    kb.add_text("a", "Refunds take five business days.")
    kb.wait_ready("a")
    r = kb.db._call("POST", "/v1/collections/kb/query", {"query": "refunds"})
    assert r["reranked"] is True and r["hits"][0]["document"] == "a"
