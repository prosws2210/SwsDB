"""What a graph holds, for the console's graph browser (the ideas of FalkorDB's own browser): its node labels and
relationship types with counts, property keys, which labels connect to which (the schema), and memory.

FalkorDB and Apache AGE (the lite profile) both answer, with their own Cypher dialects. Neo4j doesn't: collections share
one Neo4j database there, so swsdb runs only its own graph queries.
"""

from __future__ import annotations

from typing import Any

from .stores import Conflict


def _dialect(store) -> dict[str, str]:
    if store.engine == "age":  # AGE names a node's or an edge's label with label()
        return {"nl": "label(n)", "rt": "label(r)", "al": "label(a)", "bl": "label(b)",
                "keys": "MATCH (n) UNWIND keys(n) AS k RETURN DISTINCT k AS k"}
    return {"nl": "labels(n)[0]", "rt": "type(r)", "al": "labels(a)[0]", "bl": "labels(b)[0]",
            "keys": "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey AS k"}


async def info(store, name: str) -> dict[str, Any]:
    if not getattr(store, "pass_through", False):
        raise Conflict("with Neo4j as the graph engine, swsdb runs only its own graph queries")
    d = _dialect(store)

    async def rows(q: str) -> list[list]:
        return (await store.query(name, q, None, read_only=True, timeout_s=15))["rows"]

    labels = await rows(f"MATCH (n) RETURN {d['nl']} AS l, count(*) AS c ORDER BY c DESC")
    rels = await rows(f"MATCH ()-[r]->() RETURN {d['rt']} AS t, count(*) AS c ORDER BY c DESC")
    keys = sorted({str(r[0]) for r in await rows(d["keys"]) if r and r[0] is not None})
    schema = await rows(f"MATCH (a)-[r]->(b) RETURN {d['al']} AS a, {d['rt']} AS t, {d['bl']} AS b, count(*) AS c")
    memory = None
    if store.engine == "falkordb":
        try:
            raw = await store._cmd("GRAPH.MEMORY", "USAGE", name)
            pairs = dict(zip(raw[::2], raw[1::2], strict=False))
            mb = pairs.get(b"total_graph_sz_mb", pairs.get("total_graph_sz_mb"))
            memory = int(float(mb) * 1024 * 1024) if mb is not None else None
        except Exception:
            memory = None
    return {
        "engine": store.engine,
        "nodes": sum(int(c) for _, c in labels), "edges": sum(int(c) for _, c in rels),
        "labels": [{"name": "" if lab is None else str(lab), "count": int(c)} for lab, c in labels],
        "relationships": [{"name": str(t), "count": int(c)} for t, c in rels],
        "property_keys": keys,
        "schema": [{"from": "" if a is None else str(a), "type": str(t), "to": "" if b is None else str(b),
                    "count": int(c)} for a, t, b, c in schema],
        "memory_bytes": memory,
    }
