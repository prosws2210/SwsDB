"""Name rules, id mapping, graph parameter encoding and result decoding."""

import uuid

import pytest

from swsdb_server.names import NameError_, check, engine_name, parse_engine_name, point_id
from swsdb_server.stores import StoreError, _qdrant_filter, _value, cypher_literal


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


def test_graph_values_are_decoded():
    node = [["id", 0], ["labels", [b"Note"]], ["properties", [["body", b"kept"], ["n", 1]]]]
    assert _value(node) == {"id": 0, "labels": ["Note"], "properties": {"body": "kept", "n": 1}}
    edge = [["id", 3], ["type", b"LINKS"], ["src_node", 0], ["dest_node", 1], ["properties", []]]
    assert _value(edge) == {"id": 3, "properties": {}, "type": "LINKS", "src": 0, "dst": 1}
    assert _value([b"a", 1]) == ["a", 1]
