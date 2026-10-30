"""The API's health and auth behaviour, with the engine monitor replaced by a fake."""

from fastapi.testclient import TestClient

from swsdb_server.app import create_app
from swsdb_server.health import EngineStatus, Monitor
from swsdb_server.settings import Settings


class FakeMonitor(Monitor):
    def __init__(self, s: Settings, states: dict[str, str]):
        super().__init__(s)
        for cap, state in states.items():
            self.status[cap] = EngineStatus(capability=cap, engine=self.status[cap].engine, state=state)


def client(states: dict[str, str], **kw) -> TestClient:
    s = Settings(**{"allowed_hosts": "testserver", **kw})  # TestClient's Host header
    return TestClient(create_app(s, FakeMonitor(s, states), stores={}))


def test_readyz_is_200_only_when_every_engine_is_ready():
    with client({"sql": "ready", "vector": "ready", "graph": "ready"}) as c:
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ready"
    with client({"sql": "ready", "vector": "starting", "graph": "ready"}) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["status"] == "starting"
    with client({"sql": "ready", "vector": "down", "graph": "ready"}) as c:
        assert c.get("/readyz").json()["status"] == "degraded"


def test_only_selected_engines_are_reported():
    with client({"vector": "ready"}, engines_raw="vector") as c:
        assert list(c.get("/readyz").json()["engines"]) == ["vector"]
        assert c.get("/").json()["engines"] == ["vector"]


def test_keys():
    states = {"sql": "ready", "vector": "ready", "graph": "ready"}
    with client(states, api_key="adm", read_only_api_key="ro") as c:
        assert c.get("/v1/status").status_code == 401
        assert c.get("/v1/status", headers={"api-key": "nope"}).status_code == 401
        assert c.get("/v1/status", headers={"api-key": "adm"}).status_code == 200
        assert c.get("/v1/status", headers={"Authorization": "Bearer ro"}).status_code == 200
        assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200
    with client(states) as c:  # no keys configured: local use, everything open
        assert c.get("/v1/status").status_code == 200


def test_readyz_waits_for_the_document_service():
    s = Settings(allowed_hosts="testserver")
    app = create_app(s, FakeMonitor(s, {"sql": "ready", "vector": "ready", "graph": "ready"}),
                     stores={"sql": object(), "vector": object()})
    with TestClient(app) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["documents"]["state"] == "starting"
        app.state.docs = object()
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["documents"]["state"] == "ready"
        app.state.docs = None  # a stand-in, not a service to stop
