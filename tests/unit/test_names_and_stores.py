"""Name rules, id mapping, graph parameter encoding and result decoding."""

import uuid

import pytest

from swsdb_server.names import NameError_, check, engine_name, parse_engine_name, point_id
from swsdb_server.stores import StoreError, _qdrant_filter, _StaleSchema, _typed, cypher_literal


@pytest.mark.parametrize("ok", ["docs", "a", "support-docs", "v2_kb", "x" * 63, "0day"])
def test_valid_names(ok):
    assert check("collection", ok) == ok


@pytest.mark.parametrize("bad", ["", "Docs", "-x", "_x", "a__b", "x" * 64, "a b", "a/b", "../x", "a;drop", "é"])
def test_invalid_names(bad):
    with pytest.raises(NameError_):
        check("collection", bad)


def test_tenants_cannot_reach_each_other():
    # "__" is forbidden in names, so no tenant/collection pair can produce another pair's engine name
    assert engine_name("acme", "docs") == "sw_acme__docs"
    with pytest.raises(NameError_):
        engine_name("acme__evil", "docs")
    assert parse_engine_name("sw_acme__docs", "acme") == "docs"
    assert parse_engine_name("sw_acme__docs", "acm") is None
    assert parse_engine_name("sw_acme__docs", "other") is None


def test_point_ids():
    assert point_id(7) == 7
    u = str(uuid.uuid4())
    assert point_id(u) == u
    assert point_id("doc-1#chunk-3") == point_id("doc-1#chunk-3")  # stable
    assert point_id("doc-1#chunk-3") != point_id("doc-1#chunk-4")
    for bad in (-1, True, "", "x" * 513):
        with pytest.raises(ValueError):
            point_id(bad)


@pytest.mark.parametrize(("value", "literal"), [
    (None, "null"), (True, "true"), (3, "3"), (2.5, "2.5"), ("plain", '"plain"'),
    ('quote " and \\ back', '"quote \\" and \\\\ back"'),
    ("x'}) MATCH (n) DETACH DELETE n //", '"x\'}) MATCH (n) DETACH DELETE n //"'),  # stays one string
    ([1, "a"], '[1, "a"]'), ({"k": [True]}, "{k: [true]}"),
])
def test_cypher_literals(value, literal):
    assert cypher_literal(value) == literal


def test_cypher_literals_reject_what_cypher_cant_hold():
    for bad in (float("nan"), {"bad key": 1}, object()):
        with pytest.raises(StoreError):
            cypher_literal(bad)


def test_filters():
    assert _qdrant_filter(None) is None
    assert _qdrant_filter({"team": "a", "year": [1, 2]}) == {"must": [
        {"key": "team", "match": {"value": "a"}}, {"key": "year", "match": {"any": [1, 2]}}]}
    with pytest.raises(StoreError):
        _qdrant_filter({"_id": 1})  # reserved
    with pytest.raises(StoreError):
        _qdrant_filter({"x": {"$gt": 1}})


def test_graph_values_are_decoded_with_their_types():
    # a compact reply from FalkorDB 4.22: MATCH p=(a)-[e]->(c) RETURN a, e, c, p, [1,2.5,"z"], {k:[1,2], b:false}, ...
    schema = (["A", "B", "C"], ["R"], ["n", "s", "f", "t", "l", "m", "w"])
    node_a = [8, [0, [0, 1], [[0, 3, 1], [1, 2, b"x"], [2, 5, b"1.5"], [3, 4, b"true"], [4, 6, [[3, 1], [2, b"a"]]]]]]
    edge = [7, [0, 0, 0, 1, [[6, 3, 2]]]]
    node_c = [8, [1, [2], [[5, 3, 3]]]]
    a = {"id": 0, "labels": ["A", "B"], "properties": {"n": 1, "s": "x", "f": 1.5, "t": True, "l": [1, "a"]}}
    e = {"id": 0, "type": "R", "src": 0, "dst": 1, "properties": {"w": 2}}
    assert _typed(node_a, schema) == a
    assert _typed(edge, schema) == e
    assert _typed([9, [[6, [node_a, node_c]], [6, [edge]]]], schema)["edges"] == [e]
    assert _typed([6, [[3, 1], [5, b"2.5"], [2, b"z"]]], schema) == [1, 2.5, "z"]
    assert _typed([10, [b"k", [6, [[3, 1], [3, 2]]], b"b", [4, b"false"]]], schema) == {"k": [1, 2], "b": False}
    assert _typed([1, None], schema) is None
    assert _typed([11, [b"1.5", b"2.5"]], schema) == {"latitude": 1.5, "longitude": 2.5}
    assert _typed([12, [b"1", b"2"]], schema) == [1.0, 2.0]
    with pytest.raises(_StaleSchema):  # a label created after the schema was read: re-read it
        _typed([8, [5, [7], []]], schema)


def test_worker_processes_are_not_separate_servers_for_the_uploads_warning():
    """SWSDB_WORKERS names its processes <server>/<pid>: they share one volume, so uploads on disk are fine; a second
    server under another name is not."""
    from swsdb_server.cluster import Cluster, volume

    assert volume("box/123") == "box" and volume("box") == "box"
    c = Cluster(None, name="box/1")
    c.peers = [{"id": f"box/{i}", "version": "x", "files": "disk", "seen_s": 0.0} for i in (1, 2, 3)]
    assert "warning" not in c.status()
    c.peers.append({"id": "other", "version": "x", "files": "disk", "seen_s": 0.0})
    assert "SWSDB_FILES=postgres" in c.status()["warning"]


def test_dense_searches_get_the_search_breadth_and_sparse_ones_do_not():
    from swsdb_server.stores import VectorStore

    v = VectorStore("http://qdrant", "k", ef=128)
    assert v._with_ef({"query": [0.1], "limit": 10})["params"] == {"hnsw_ef": 128}
    assert v._with_ef({"query": [0.1], "limit": 500})["params"] == {"hnsw_ef": 500}  # never below the limit
    assert v._with_ef({"query": [0.1], "params": {"exact": True}})["params"] == {"exact": True}  # the caller's own
    assert "params" not in v._with_ef({"query": {"indices": [1], "values": [1.0]}, "using": "sparse"})
    hybrid = v._with_ef({"prefetch": [{"query": [0.1], "using": "dense", "limit": 40},
                                      {"query": {"indices": [1], "values": [1.0]}, "using": "sparse", "limit": 40}],
                         "query": {"fusion": "rrf"}, "limit": 10})
    assert "params" not in hybrid and hybrid["prefetch"][0]["params"] == {"hnsw_ef": 128}
    assert "params" not in hybrid["prefetch"][1]
    assert "params" not in VectorStore("http://qdrant", "k", ef=0)._with_ef({"query": [0.1]})
    assert "params" not in v._with_ef({"filter": {}, "limit": 5})  # no search at all


def test_a_documents_filter_asks_for_indexes_typed_by_its_values():
    from swsdb_server.documents import Documents

    assert Documents.filter_indexes({"team": "support", "year": [2024, 2025], "public": True, "document": "a.pdf"}) == [
        ("meta.team", "keyword"), ("meta.year", "integer"), ("meta.public", "bool"), ("doc", "keyword")]
    assert Documents.filter_indexes({"x": [], "y": 1.5}) == [] and Documents.filter_indexes(None) == []


def test_result_cache_is_bounded_expires_and_clears(monkeypatch):
    from swsdb_server import cache

    now = [100.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    c = cache.ResultCache(size=2, ttl_s=10)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # "a" is now the most recently used
    c.put("c", 3)
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    now[0] += 11
    assert c.get("a") is None and len(c) == 1  # expired entries go when looked up
    c.clear()
    assert len(c) == 0
    off = cache.ResultCache(size=0, ttl_s=10)
    off.put("a", 1)
    assert not off.enabled and off.get("a") is None


def test_questions_arriving_together_share_one_model_call():
    import asyncio
    import time as _time

    from swsdb_server.embed import Embedder

    e = Embedder("local", query_cache=0)
    calls = []

    def fake(texts, query):
        calls.append(list(texts))
        _time.sleep(0.05)  # the model is busy: the others queue meanwhile
        return [[float(len(t))] for t in texts]

    e._local_dense = fake

    async def go():
        return await asyncio.gather(*(e.dense([q], query=True) for q in ["a", "bb", "ccc", "bb", "dddd"]))
    out = asyncio.run(go())
    assert out == [[[1.0]], [[2.0]], [[3.0]], [[2.0]], [[4.0]]]
    assert calls == [["a", "bb", "ccc", "dddd"]]  # one call, the duplicate embedded once
    calls.clear()

    async def staggered():
        first = asyncio.ensure_future(e.dense(["one"], query=True))
        await asyncio.sleep(0.01)  # the model is now busy with "one"
        rest = await asyncio.gather(*(e.dense([q], query=True) for q in ["two", "three"]))
        return [await first, *rest]
    assert asyncio.run(staggered()) == [[[3.0]], [[3.0]], [[5.0]]]
    assert calls == [["one"], ["two", "three"]]  # those that came while it was busy went in together

    def broken(texts, query):
        raise RuntimeError("model failed")
    e._local_dense = broken
    try:
        asyncio.run(e.dense(["x"], query=True))
    except RuntimeError as err:
        assert "model failed" in str(err)
    else:
        raise AssertionError("the model's error should reach the caller")


def test_nothing_migrates_the_database_while_a_restore_replaces_it():
    """A heartbeat or an outbox poll during pg_restore must not create swsdb's schema in the empty database."""
    import asyncio

    from swsdb_server.stores import SqlStore, Unavailable

    s = SqlStore("/nonexistent")  # never connects: the refusal comes first
    s.restoring = True
    with pytest.raises(Unavailable, match="being restored"):
        asyncio.run(s.migrate())
