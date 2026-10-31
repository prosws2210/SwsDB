"""The Neo4j adapter's pure parts: scoping swsdb's queries to a collection, and the Query API's typed values."""

from __future__ import annotations

from swsdb_server.neo4j import _typed, namespace, scope
from swsdb_server.settings import Settings


def test_every_document_chunk_and_entity_pattern_gets_the_collection_label():
    ns = namespace("sw_acme__kb")
    assert ns.startswith("C_") and ns != namespace("sw_globex__kb")
    q = ("MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(n:Chunk) WHERE c.id IN $ids "
         "AND n.name <> ':Chunk' RETURN c.id")
    out = scope(q, ns)
    assert out.count(f":{ns}") == 3
    assert f"(c:Chunk:{ns})" in out and f"(e:Entity:{ns})" in out and "':Chunk'" in out  # strings untouched
    assert "[:MENTIONS]" in out  # relationship types are not node labels
    assert scope("CREATE (d)-[:HAS_CHUNK]->(:Chunk {id: r.id})", ns) == \
        f"CREATE (d)-[:HAS_CHUNK]->(:Chunk:{ns} {{id: r.id}})"
    assert scope("MERGE ( d :Document {id: $doc})", ns) == f"MERGE ( d :Document:{ns} {{id: $doc}})"


def test_typed_values_in_the_shape_of_the_other_graph_engines():
    ns = "C_x"
    node = {"$type": "Node", "_value": {"_element_id": "4:a:0", "_labels": ["Chunk", ns],
                                         "_properties": {"n": {"$type": "Integer", "_value": "2"}}}}
    rel = {"$type": "Relationship", "_value": {"_element_id": "5:a:0", "_start_node_element_id": "4:a:0",
                                                "_end_node_element_id": "4:a:1", "_type": "NEXT", "_properties": {}}}
    assert _typed(node, ns) == {"id": "4:a:0", "labels": ["Chunk"], "properties": {"n": 2}}
    assert _typed(rel, ns) == {"id": "5:a:0", "type": "NEXT", "src": "4:a:0", "dst": "4:a:1", "properties": {}}
    path = _typed({"$type": "Path", "_value": [node, rel, node]}, ns)
    assert len(path["nodes"]) == 2 and path["edges"][0]["type"] == "NEXT"
    assert _typed({"$type": "List", "_value": [{"$type": "Float", "_value": "1.5"}, {"$type": "Null",
                                                                                   "_value": None}]}, ns) == [1.5, None]
    assert _typed({"$type": "Map", "_value": {"k": {"$type": "Boolean", "_value": True}}}, ns) == {"k": True}


def test_neo4j_is_the_graph_engine_when_its_url_is_set():
    s = Settings(neo4j_url="http://neo4j:7474")
    assert s.product("graph") == "neo4j" and s.external("graph") and "graph" not in s.local_engines
    try:
        _ = Settings(neo4j_url="http://neo4j:7474", falkordb_url="redis://x:6379").engines
    except ValueError as e:
        assert "not both" in str(e)
    else:
        raise AssertionError("both graph engines were accepted")
