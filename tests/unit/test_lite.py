"""The lite profile's pure parts: Cypher result columns, AGE's agtype output, Qdrant filters as SQL."""

from __future__ import annotations

import json

import pytest

from swsdb_server.lite import agtype, filter_sql, return_columns
from swsdb_server.settings import Settings
from swsdb_server.stores import StoreError


@pytest.mark.parametrize(("query", "cols"), [
    ("MATCH (n) RETURN count(n)", ["count(n)"]),
    ("MATCH (c:Chunk)-[:NEXT]-(n:Chunk) WHERE c.id IN $ids RETURN c.id, n.id", ["c.id", "n.id"]),
    ("MATCH (a) RETURN DISTINCT a.x AS x, {k: [1, 2], j: 'a, b'} AS m ORDER BY x LIMIT 3", ["x", "m"]),
    ("MATCH (a) WITH a RETURN a.name, size([(a)-->(b) | b]) SKIP 1", ["a.name", "size([(a)-->(b) | b])"]),
    ("CREATE (:Person {name: 'RETURN x, y'})", []),
    ("MATCH (n) // RETURN a, b\nDETACH DELETE n", []),
    ("MATCH (a) RETURN a.x AS `odd name` UNION MATCH (b) RETURN b.x AS `odd name`", ["odd name"]),
    ("CALL { MATCH (n) RETURN n } RETURN 1 AS one", ["one"]),
    ("RETURN 'it''s', \"a\\\"b\" AS q", ["'it''s'", "q"]),
])
def test_return_columns(query, cols):
    assert return_columns(query) == cols


def test_return_star_is_refused_with_a_reason():
    with pytest.raises(StoreError, match="RETURN \\*"):
        return_columns("MATCH (n) RETURN *")


def test_agtype_values_in_the_falkordb_shape():
    v = '{"id": 1, "label": "Chunk", "properties": {"id": "c1", "n": 2}}::vertex'
    assert agtype(v) == {"id": 1, "labels": ["Chunk"], "properties": {"id": "c1", "n": 2}}
    e = '{"id": 9, "label": "NEXT", "end_id": 2, "start_id": 1, "properties": {}}::edge'
    assert agtype(e) == {"id": 9, "type": "NEXT", "src": 1, "dst": 2, "properties": {}}
    p = f'[{v}, {e}, {v}]::path'
    assert agtype(p) == {"nodes": [agtype(v), agtype(v)], "edges": [agtype(e)]}
    assert agtype('[1, 2.5, "x::vertex", true, null, {"k": -3}]') == [1, 2.5, "x::vertex", True, None, {"k": -3}]
    assert agtype('"a \\"quoted\\" \\u00e9"') == 'a "quoted" é'
    assert agtype("12::numeric") == 12 and agtype("NaN") != agtype("NaN")
    assert agtype(None) is None


def test_filters_become_containment_on_the_payload():
    params: list = []
    sql = filter_sql({"must": [{"key": "live", "match": {"value": True}},
                               {"key": "readers", "match": {"any": ["user:1", "*"]}},
                               {"key": "meta.team", "match": {"value": "support"}},
                               {"has_id": ["a", 7]}],
                      "must_not": [{"key": "version", "match": {"value": 3}}]}, params)
    assert sql.count("p.payload @> ANY(%s::jsonb[])") == 4 and "p.id = ANY(%s::text[])" in sql
    assert "NOT (" in sql
    live, readers, team, ids, version = params
    assert {json.dumps({"live": True}), json.dumps({"live": [True]})} == set(live)
    assert json.dumps({"readers": ["user:1"]}) in readers and json.dumps({"readers": "*"}) in readers
    assert json.dumps({"meta": {"team": "support"}}) in team
    assert ids[1] == "7" and ids[0] != "a"  # ids as the point ids Qdrant would use
    assert json.dumps({"version": 3}) in version
    assert filter_sql(None, []) == "true"
    assert filter_sql({"must": [{"key": "x", "match": {"any": []}}]}, []) == "false"
    with pytest.raises(StoreError):
        filter_sql({"must": [{"key": "x", "range": {"gt": 1}}]}, [])


def test_lite_profile_settings(monkeypatch):
    monkeypatch.delenv("SWSDB_ENGINES", raising=False)
    s = Settings(profile="lite")
    assert s.engines == ("sql", "vector", "graph")
    assert s.local_engines == ("sql",)
    assert [s.product(c) for c in s.engines] == ["postgres", "pgvector", "age"]
    assert s.in_postgres("vector") and not s.external("graph")
    with pytest.raises(ValueError, match="must include sql"):
        _ = Settings(profile="lite", engines_raw="vector").engines
    with pytest.raises(ValueError, match="SWSDB_PROFILE"):
        Settings(profile="tiny")
    assert Settings().local_engines == ("sql", "vector", "graph")
