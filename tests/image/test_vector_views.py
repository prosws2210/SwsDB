"""The console's closer look at a vector collection (Qdrant's dashboard ideas, for swsdb's collections): configuration,
payload indexes, memory, segments, index quality, similar points by any named vector, and a 2D map; and what a graph
holds, for the graph browser. Against a real container, on collections made the way an app makes them."""

from __future__ import annotations

import random
import uuid
import warnings

import httpx
import pytest
from qdrant_client import QdrantClient, models
from test_container import Box, sh

pytestmark = pytest.mark.image

ADMIN = "vector-views-admin-key"


@pytest.fixture(scope="module")
def box():
    vol = f"swsdb-vviews-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN}).start()
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def api(box: Box, key: str = ADMIN, tenant: str | None = None) -> httpx.Client:
    headers = {"api-key": key, **({"swsdb-tenant": tenant} if tenant else {})}
    return httpx.Client(base_url=f"http://127.0.0.1:{box.port}/v1/collections", headers=headers, timeout=120)


def ok(r: httpx.Response, status: int = 200):
    assert r.status_code == status, f"{r.status_code}: {r.text[:400]}"
    return r.json()


def unit(n: int) -> list[float]:
    v = [random.gauss(0, 1) for _ in range(n)]
    s = sum(x * x for x in v) ** 0.5
    return [x / s for x in v]


@pytest.fixture(scope="module")
def keys(box):
    a = httpx.Client(base_url=f"http://127.0.0.1:{box.port}", headers={"api-key": ADMIN}, timeout=60)
    out = {name: a.post("/v1/keys", json={"role": role, "tenant": tenant, "name": name}).json()["key"]
           for name, role, tenant in (("shop", "admin", "shop"), ("shop_read", "read", "shop"),
                                      ("rival", "admin", "rival"))}
    random.seed(3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        q = QdrantClient(url=f"http://127.0.0.1:{box.port}", prefix="qdrant", api_key=out["shop"], timeout=60)
    q.create_collection("products", vectors_config={
        "image": models.VectorParams(size=8, distance=models.Distance.COSINE),
        "text": models.VectorParams(size=12, distance=models.Distance.DOT)},
        sparse_vectors_config={"keywords": models.SparseVectorParams()})
    q.create_payload_index("products", "category", field_schema=models.PayloadSchemaType.KEYWORD)
    cats = ["kitchen", "office", "garden"]
    q.upsert("products", wait=True, points=[models.PointStruct(id=i, vector={
        "image": unit(8), "text": unit(12),
        "keywords": models.SparseVector(indices=sorted(random.sample(range(100), 4)), values=[0.5, 0.4, 0.3, 0.2])},
        payload={"category": cats[i % 3], "price": i * 1.5, "specs": {"color": "red" if i % 2 else "black"}})
        for i in range(1, 121)])
    q.create_collection("faces", vectors_config=models.VectorParams(
        size=4, distance=models.Distance.COSINE,
        multivector_config=models.MultiVectorConfig(comparator=models.MultiVectorComparator.MAX_SIM)))
    q.upsert("faces", wait=True, points=[models.PointStruct(id=i, vector=[unit(4), unit(4)], payload={"p": i % 5})
                                         for i in range(1, 31)])
    ok(api(box, out["shop"]).put("/docs", json={"documents": True}), 201)
    return out


def test_inspect_describes_named_sparse_and_multi_vectors(box, keys):
    p = ok(api(box, keys["shop_read"]).get("/products/inspect"))
    assert p["engine"] == "qdrant" and p["points"] == 120 and p["status"] in ("green", "yellow")
    assert {(v["name"], v["kind"], v["size"], v["distance"]) for v in p["vectors"]} == {
        ("image", "dense", 8, "Cosine"), ("text", "dense", 12, "Dot")}
    assert p["sparse"] == [{"name": "keywords"}]
    assert p["payload_indexes"] == [{"field": "category", "type": "keyword", "points": 120}]
    assert p["hnsw"]["m"] > 0 and p["segments"] >= 1
    f = ok(api(box, keys["shop"]).get("/faces/inspect"))
    assert f["vectors"] == [{"name": "", "kind": "multivector", "size": 4, "distance": "Cosine", "on_disk": False}]


def test_memory_and_segments(box, keys):
    m = ok(api(box, keys["shop_read"]).get("/products/memory"))
    assert m["engine"] == "qdrant" and m["total"]["disk_bytes"] > 0
    assert {v["name"] for v in m["vectors"]} == {"image", "text"} and m["sparse_vectors"][0]["name"] == "keywords"
    assert [x["name"] for x in m["payload_index"]] == ["category"]
    assert "sw_" not in str(m)  # the engine's names for the tenant's collections never show
    s = ok(api(box, keys["shop_read"]).get("/products/segments"))
    assert sum(x["points_count"] for x in s["idle_segments"]) == 120 and "summary" in s


def test_similar_by_any_kind_of_vector(box, keys):
    c = api(box, keys["shop_read"])
    for using in ("image", "text", "keywords"):
        hits = ok(c.post("/products/similar", json={"like": 7, "using": using, "limit": 5}))["hits"]
        assert len(hits) == 5 and 7 not in {h["id"] for h in hits}, using  # its neighbours, not the point itself
        assert hits == sorted(hits, key=lambda h: -h["score"])
    within = ok(c.post("/products/similar", json={"like": 7, "using": "image", "limit": 20,
                                                  "where": {"category": "office"}}))["hits"]
    assert within and {h["payload"]["category"] for h in within} == {"office"}
    assert len(ok(c.post("/faces/similar", json={"like": 3, "limit": 3}))["hits"]) == 3
    by_vector = ok(c.post("/products/similar", json={"vector": unit(8), "using": "image", "limit": 3}))["hits"]
    assert len(by_vector) == 3
    assert c.post("/products/similar", json={"limit": 3}).status_code == 422  # neither a point nor a vector


def test_index_quality(box, keys):
    r = ok(api(box, keys["shop_read"]).post("/products/recall", json={"samples": 20, "k": 5, "using": "image"}))
    assert r["samples"] == 20 and r["k"] == 5 and 0.9 <= r["recall"] <= 1.0
    assert r["approx_ms"] >= 0 and r["exact_ms"] >= 0
    bad = api(box, keys["shop"]).post("/products/recall", json={"using": "keywords"})
    assert bad.status_code == 400 and "dense" in bad.json()["message"]


def test_map_projects_points_with_their_colour(box, keys):
    r = ok(api(box, keys["shop_read"]).post("/products/map", json={"limit": 100, "using": "text",
                                                                    "color_by": "category"}))
    assert len(r["points"]) == 100 and len(r["explained"]) == 2
    assert all(-1 <= p["x"] <= 1 and -1 <= p["y"] <= 1 for p in r["points"])
    assert {p["label"] for p in r["points"]} == {"kitchen", "office", "garden"}
    nested = ok(api(box, keys["shop"]).post("/products/map", json={"limit": 10, "using": "image",
                                                                    "color_by": "specs.color"}))
    assert {p["label"] for p in nested["points"]} <= {"red", "black"}
    assert ok(api(box, keys["shop"]).post("/faces/map", json={"limit": 30}))["points"]  # multivectors: their mean
    sparse = api(box, keys["shop"]).post("/products/map", json={"using": "keywords"})
    assert sparse.status_code == 400 and "sparse" in sparse.json()["message"]


def test_payload_indexes_need_the_admin_key_and_a_raw_collection(box, keys):
    admin, read = api(box, keys["shop"]), api(box, keys["shop_read"])
    assert read.put("/products/index", json={"field": "price", "type": "float"}).status_code == 403
    ok(admin.put("/products/index", json={"field": "price", "type": "float"}))
    ok(admin.put("/products/index", json={"field": "specs.color", "type": "keyword"}))
    fields = {x["field"]: x["type"] for x in ok(read.get("/products/inspect"))["payload_indexes"]}
    assert fields == {"category": "keyword", "price": "float", "specs.color": "keyword"}
    assert read.delete("/products/index/price").status_code == 403
    ok(admin.delete("/products/index/price"))
    assert "price" not in {x["field"] for x in ok(read.get("/products/inspect"))["payload_indexes"]}
    assert admin.put("/products/index", json={"field": "x", "type": "nope"}).status_code == 400
    assert admin.put("/docs/index", json={"field": "x", "type": "keyword"}).status_code == 409  # swsdb's indexes


def test_a_documents_collection_needs_the_admin_key_and_other_projects_see_nothing(box, keys):
    read = api(box, keys["shop_read"])
    assert ok(read.get("/docs/inspect"))["vectors"][0]["name"] == "dense"  # its setup is fine to read
    for call in (lambda: read.post("/docs/map", json={"limit": 10}),
                 lambda: read.post("/docs/recall", json={"using": "dense"}),
                 lambda: read.post("/docs/similar", json={"vector": [0.0] * 384, "using": "dense"})):
        assert call().status_code == 403  # raw access would skip document readers
    rival = api(box, keys["rival"])
    for path in ("/products/inspect", "/products/memory", "/products/segments"):
        assert rival.get(path).status_code == 404
    assert rival.post("/products/map", json={"limit": 5}).status_code == 404
    assert api(box, keys["rival"], tenant="shop").get("/products/inspect").status_code == 403


def test_graph_info_for_the_browser(box, keys):
    """Labels and relationship types with counts, property keys and the label-to-label schema of a raw graph."""
    c = api(box, keys["shop"])
    ok(c.put("/people", json={"graph": True}), 201)
    ok(c.post("/people/cypher", json={"read_only": False, "query":
        "CREATE (a:Person {name:'Ana', age:30})-[:KNOWS]->(b:Person {name:'Ravi'}), "
        "(a)-[:WORKS_AT {since:2020}]->(o:Office {city:'Lisbon'}), (b)-[:WORKS_AT]->(o)"}))
    g = ok(api(box, keys["shop_read"]).get("/people/graph"))
    assert g["engine"] == "falkordb" and g["nodes"] == 3 and g["edges"] == 3
    assert {(x["name"], x["count"]) for x in g["labels"]} == {("Person", 2), ("Office", 1)}
    assert {(x["name"], x["count"]) for x in g["relationships"]} == {("KNOWS", 1), ("WORKS_AT", 2)}
    assert set(g["property_keys"]) >= {"name", "age", "city", "since"}
    assert {(s["from"], s["type"], s["to"], s["count"]) for s in g["schema"]} == {
        ("Person", "KNOWS", "Person", 1), ("Person", "WORKS_AT", "Office", 2)}
    assert api(box, keys["rival"]).get("/people/graph").status_code == 404
    assert c.get("/products/graph").status_code == 404  # a vector collection has no graph
