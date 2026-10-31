"""An existing Neo4j as the graph engine (SWSDB_NEO4J_URL), over its HTTP Query API.

Neo4j Community has one user database, so every collection's graph shares it. Each collection gets its own label
(`C_<hash of its engine name>`) on every node it owns, and swsdb's own queries (document structure, entities,
graph expansion, reconciliation) are scoped to it. Pass-through Cypher is refused: keeping a user's query inside its
collection would mean rewriting it, and swsdb doesn't build a security boundary on parsing (ADR 0004, ADR 0007).
Graph-backed retrieval works the same as with FalkorDB.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from .lite import _mask
from .names import parse_engine_name
from .stores import Forbidden, NotFound, StoreError, Unavailable, cypher_literal

REGISTRY = "SwsdbGraph"  # one node per collection graph: {name: engine name}
_LABELLED = re.compile(r"\(\s*\w*\s*:(Document|Chunk|Entity)\b")


def namespace(name: str) -> str:
    return "C_" + hashlib.sha256(name.encode()).hexdigest()[:20]


def scope(query: str, ns: str) -> str:
    """swsdb's own query with every Document, Chunk and Entity node pattern limited to one collection's label."""
    code = _mask(query)
    out, last = [], 0
    for m in _LABELLED.finditer(code):
        out.append(query[last:m.end()])
        out.append(f":{ns}")
        last = m.end()
    out.append(query[last:])
    return "".join(out)


def _typed(v: Any, ns: str) -> Any:
    """The Query API's typed JSON as plain values, nodes and relationships in the shape the FalkorDB adapter uses."""
    if not isinstance(v, dict) or "$type" not in v:
        return v
    t, x = v["$type"], v.get("_value")
    if t in ("Null",):
        return None
    if t == "Integer":
        return int(x)
    if t == "Float":
        return float(x)
    if t in ("String", "Boolean"):
        return x
    if t == "List":
        return [_typed(i, ns) for i in x]
    if t == "Map":
        return {k: _typed(i, ns) for k, i in x.items()}
    if t == "Node":
        return {"id": x["_element_id"], "labels": [lab for lab in x["_labels"] if lab != ns],
                "properties": {k: _typed(i, ns) for k, i in x["_properties"].items()}}
    if t == "Relationship":
        return {"id": x["_element_id"], "type": x["_type"], "src": x["_start_node_element_id"],
                "dst": x["_end_node_element_id"],
                "properties": {k: _typed(i, ns) for k, i in x["_properties"].items()}}
    if t == "Path":
        items = [_typed(i, ns) for i in x]
        return {"nodes": items[0::2], "edges": items[1::2]}
    return x  # dates, durations, points: as Neo4j writes them


class Neo4jGraphStore:
    in_postgres = False
    engine = "neo4j"
    pass_through = False  # one shared database: only swsdb's own, scoped queries run

    def __init__(self, url: str, user: str, password: str, database: str = "neo4j"):
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), auth=(user, password), timeout=130.0,
                                      headers={"accept": "application/vnd.neo4j.query",
                                               "content-type": "application/json"})
        self.db = database
        self._known: set[str] = set()
        self._ready = False

    async def close(self) -> None:
        await self.http.aclose()

    def forget(self) -> None:
        self._known.clear()

    async def _run(self, statement: str, params: dict | None = None, *, read: bool = False) -> dict:
        body: dict[str, Any] = {"statement": statement, "parameters": params or {}}
        if read:
            body["accessMode"] = "READ"
        try:
            r = await self.http.post(f"/db/{self.db}/query/v2", content=json.dumps(body))
        except httpx.HTTPError as e:
            raise Unavailable(f"Neo4j: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise Unavailable(f"Neo4j refused swsdb's credentials ({r.status_code})")
        if r.status_code >= 500:
            raise Unavailable(f"Neo4j answered {r.status_code}")
        data = r.json()
        if data.get("errors"):
            err = data["errors"][0]
            code, msg = err.get("code", ""), (err.get("message") or "").splitlines()[0]
            if ".TransientError." in code or code.endswith("DatabaseUnavailable"):
                raise Unavailable(f"Neo4j: {msg}")
            raise StoreError(f"{code.rsplit('.', 1)[-1]}: {msg}")
        return data

    async def _setup(self) -> None:
        if not self._ready:
            await self._run(f"CREATE CONSTRAINT swsdb_graph_name IF NOT EXISTS FOR (g:{REGISTRY}) "
                            "REQUIRE g.name IS UNIQUE")
            self._ready = True

    async def all_names(self) -> list[str]:
        await self._setup()
        res = await self._run(f"MATCH (g:{REGISTRY}) RETURN g.name ORDER BY g.name", read=True)
        return [_typed(row[0], "") for row in res["data"]["values"]]

    async def list(self, tenant: str) -> list[str]:
        return sorted(n for g in await self.all_names() if (n := parse_engine_name(g, tenant)))

    async def exists(self, name: str) -> bool:
        if name in self._known:
            return True
        await self._setup()
        res = await self._run(f"MATCH (g:{REGISTRY} {{name: $name}}) RETURN count(g)", {"name": name}, read=True)
        found = _typed(res["data"]["values"][0][0], "") > 0
        if found:
            self._known.add(name)
        return found

    async def _ensure(self, name: str) -> None:
        if await self.exists(name):
            return
        ns = namespace(name)
        # entity keys are unique per collection, so concurrent MERGEs of one entity can't make two
        await self._run(f"CREATE CONSTRAINT swsdb_{ns}_key IF NOT EXISTS FOR (n:{ns}) REQUIRE n.key IS UNIQUE")
        await self._run(f"CREATE INDEX swsdb_{ns}_id IF NOT EXISTS FOR (n:{ns}) ON (n.id)")
        await self._run(f"MERGE (:{REGISTRY} {{name: $name}})", {"name": name})
        self._known.add(name)

    async def index(self, name: str, label: str, prop: str) -> None:
        await self._ensure(name)  # the collection's label carries the id index and the key constraint

    async def count_nodes(self, name: str) -> int:
        res = await self._run(f"MATCH (n:{namespace(name)}) RETURN count(n)", read=True)
        return _typed(res["data"]["values"][0][0], "")

    async def query(self, name: str, query: str, params: dict | None, *, read_only: bool, timeout_s: float) -> dict:
        """One of swsdb's own queries, scoped to the collection. Writes to one collection run one at a time (each
        locks the collection's registry node first), so MERGE and the orphan clean-up stay correct under load."""
        if not await self.exists(name):
            if read_only:
                raise NotFound("graph not found")
            await self._ensure(name)
        for k, v in (params or {}).items():
            if not isinstance(k, str) or not k.replace("_", "a").isalnum() or k[0].isdigit():
                raise StoreError(f"invalid parameter name {k!r}")
            cypher_literal(v)
        ns = namespace(name)
        statement = scope(query, ns)
        p = dict(params or {})
        if not read_only:
            statement = (f"MATCH (swsdb_lock:{REGISTRY} {{name: $swsdb_graph}}) "
                         "SET swsdb_lock.writes = coalesce(swsdb_lock.writes, 0) + 1 WITH * " + statement)
            p["swsdb_graph"] = name
        res = await self._run(statement, p, read=read_only)
        data = res.get("data") or {}
        columns = list(data.get("fields") or [])
        if not read_only and columns == []:
            return {"columns": [], "rows": [], "stats": []}
        return {"columns": columns, "rows": [[_typed(v, ns) for v in row] for row in data.get("values") or []],
                "stats": []}

    async def delete_orphan_entities(self, name: str) -> None:
        await self.query(name, "MATCH (e:Entity) WHERE NOT EXISTS { (e)<-[:MENTIONS]-() } DETACH DELETE e", None,
                         read_only=False, timeout_s=120)

    async def drop(self, name: str) -> bool:
        if not await self.exists(name):
            return False
        ns = namespace(name)
        await self._run(f"MATCH (n:{ns}) CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 1000 ROWS")
        await self._run(f"DROP CONSTRAINT swsdb_{ns}_key IF EXISTS")
        await self._run(f"DROP INDEX swsdb_{ns}_id IF EXISTS")
        await self._run(f"MATCH (g:{REGISTRY} {{name: $name}}) DELETE g", {"name": name})
        self._known.discard(name)
        return True

    # -- snapshots: the collection's nodes and relationships as JSON

    async def dump(self, name: str) -> bytes | None:
        if not await self.exists(name):
            return None
        ns = namespace(name)
        nodes = await self._run(f"MATCH (n:{ns}) RETURN elementId(n), [x IN labels(n) WHERE x <> $ns], "
                                "properties(n)", {"ns": ns}, read=True)
        rels = await self._run(f"MATCH (a:{ns})-[r]->(b:{ns}) RETURN elementId(a), elementId(b), type(r), "
                               "properties(r)", read=True)
        out = {"format": 1, "nodes": [[_typed(v, ns) for v in row] for row in nodes["data"]["values"]],
               "relationships": [[_typed(v, ns) for v in row] for row in rels["data"]["values"]]}
        return json.dumps(out, separators=(",", ":")).encode()

    async def restore(self, name: str, data: bytes) -> None:
        doc = json.loads(data)
        if doc.get("format") != 1:
            raise StoreError(f"unknown Neo4j graph dump format {doc.get('format')!r}")
        await self.drop(name)
        await self._ensure(name)
        ns = namespace(name)
        await self._run(f"CREATE INDEX swsdb_{ns}_eid IF NOT EXISTS FOR (n:{ns}) ON (n.swsdb_eid)")
        nodes = [{"eid": e, "labels": labels, "props": props} for e, labels, props in doc["nodes"]]
        for b in range(0, len(nodes), 1000):
            await self._run(f"UNWIND $rows AS row CREATE (n:{ns}) SET n = row.props, n.swsdb_eid = row.eid "
                            "SET n:$(row.labels)", {"rows": nodes[b:b + 1000]})
        rels = [{"a": a, "b": b, "type": t, "props": props} for a, b, t, props in doc["relationships"]]
        for b in range(0, len(rels), 1000):
            await self._run(f"UNWIND $rows AS row MATCH (a:{ns} {{swsdb_eid: row.a}}), (b:{ns} {{swsdb_eid: row.b}}) "
                            "CREATE (a)-[r:$(row.type)]->(b) SET r = row.props", {"rows": rels[b:b + 1000]})
        await self._run(f"MATCH (n:{ns}) REMOVE n.swsdb_eid")
        await self._run(f"DROP INDEX swsdb_{ns}_eid IF EXISTS")


def refuse_pass_through() -> None:
    raise Forbidden("pass-through Cypher isn't available with Neo4j as the graph engine: collections share one Neo4j "
                    "database, and swsdb won't rely on rewriting your query to keep it inside one collection. "
                    "Document retrieval with graph expansion works; for your own Cypher, use FalkorDB (the default) "
                    "or the lite profile.")
