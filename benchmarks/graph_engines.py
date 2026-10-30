"""Decision L1: FalkorDB vs Apache AGE on a GraphRAG-shaped workload.

Builds the same random entity graph in both engines (N entities, 3 outgoing relations each), indexed the way each
engine's docs recommend, then times the traversals GraphRAG retrieval makes from random seed entities:

  out-1hop   MATCH (a {id})-[:R]->(b)           LIMIT 50
  out-2hop   MATCH (a {id})-[:R*1..2]->(b)      DISTINCT, LIMIT 200
  any-1hop   MATCH (a {id})-[:R]-(b)            LIMIT 50   (undirected; few seeds, capped per query)

    docker run -d --name bench-falkor -p 127.0.0.1:16379:6379 falkordb/falkordb-server:v4.22.3
    docker run -d --name bench-age -p 127.0.0.1:15432:5432 -e POSTGRES_PASSWORD=bench apache/age:release_PG17_1.6.0
    uv run --project server python benchmarks/graph_engines.py
"""

from __future__ import annotations

import os
import random
import statistics
import time

import psycopg
import redis

N = int(os.environ.get("BENCH_ENTITIES", "20000"))
EDGES_PER = 3
SEEDS = int(os.environ.get("BENCH_SEEDS", "200"))
UNDIRECTED_SEEDS = 5
CAP_S = 120  # per-query cap for the undirected case

random.seed(7)
nodes = list(range(N))
edges = [(i, random.randrange(N)) for i in range(N) for _ in range(EDGES_PER)]
seeds = [random.randrange(N) for _ in range(SEEDS)]

QUERIES = {
    "out-1hop": ("MATCH (a:Entity {{id:{s}}})-[:RELATES_TO]->(b) RETURN b.id LIMIT 50", seeds),
    "out-2hop": ("MATCH (a:Entity {{id:{s}}})-[:RELATES_TO*1..2]->(b) RETURN DISTINCT b.id LIMIT 200", seeds),
    "any-1hop": ("MATCH (a:Entity {{id:{s}}})-[:RELATES_TO]-(b) RETURN b.id LIMIT 50", seeds[:UNDIRECTED_SEEDS]),
}


def batches(items, size):
    for k in range(0, len(items), size):
        yield items[k:k + size]


class Falkor:
    name = "FalkorDB 4.22.3"

    def __init__(self):
        self.r = redis.Redis(port=16379)
        self.r.delete("bench")

    def q(self, cypher: str, ro: bool = False):
        return self.r.execute_command("GRAPH.RO_QUERY" if ro else "GRAPH.QUERY", "bench", cypher,
                                      "--timeout", CAP_S * 1000)

    def load(self):
        for b in batches(nodes, 2000):
            self.q(f"UNWIND {b} AS i CREATE (:Entity {{id: i}})")
        self.q("CREATE INDEX FOR (e:Entity) ON (e.id)")
        for b in batches(edges, 5000):
            pairs = ",".join(f"[{a},{c}]" for a, c in b)
            self.q(f"UNWIND [{pairs}] AS p MATCH (a:Entity {{id:p[0]}}), (b:Entity {{id:p[1]}}) "
                   "CREATE (a)-[:RELATES_TO]->(b)")

    def run(self, cypher: str):
        self.q(cypher, ro=True)


class Age:
    name = "Apache AGE 1.6.0 (PG 17)"

    def __init__(self):
        self.c = psycopg.connect("host=127.0.0.1 port=15432 user=postgres password=bench dbname=postgres",
                                 autocommit=True)
        self.c.execute("CREATE EXTENSION IF NOT EXISTS age")
        self.c.execute("LOAD 'age'")
        self.c.execute('SET search_path = ag_catalog, "$user", public')
        if self.c.execute("SELECT 1 FROM ag_graph WHERE name = 'bench'").fetchone():
            self.c.execute("SELECT drop_graph('bench', true)")
        self.c.execute("SELECT create_graph('bench')")

    def q(self, cypher: str):
        return self.c.execute(f"SELECT * FROM cypher('bench', $$ {cypher} $$) AS (v agtype)").fetchall()

    def load(self):
        for b in batches(nodes, 2000):
            self.q(f"UNWIND {b} AS i CREATE (e:Entity {{id: i}}) RETURN count(e)")
        # `{id: 5}` compiles to `properties @> '{"id": 5}'`, served by a GIN index; edge tables get no endpoint
        # indexes by default, so add them (without these a 1-hop query took over a minute)
        self.c.execute('CREATE INDEX ON bench."Entity" USING gin (properties)')
        self.c.execute('ANALYZE bench."Entity"')
        for b in batches(edges, 5000):
            pairs = ",".join(f"[{a},{c}]" for a, c in b)
            self.q(f"UNWIND [{pairs}] AS p MATCH (a:Entity {{id:p[0]}}), (b:Entity {{id:p[1]}}) "
                   "CREATE (a)-[r:RELATES_TO]->(b) RETURN count(r)")
        self.c.execute('CREATE INDEX ON bench."RELATES_TO" (start_id)')
        self.c.execute('CREATE INDEX ON bench."RELATES_TO" (end_id)')
        self.c.execute('ANALYZE bench."RELATES_TO"')
        self.c.execute(f"SET statement_timeout = '{CAP_S}s'")

    def run(self, cypher: str):
        self.q(cypher)


def measure(engine) -> dict:
    t0 = time.perf_counter()
    engine.load()
    out = {"load_s": time.perf_counter() - t0}
    for key, (template, use) in QUERIES.items():
        times, capped = [], 0
        for s in use:
            t = time.perf_counter()
            try:
                engine.run(template.format(s=s))
            except Exception:  # a timeout counts as the cap
                capped += 1
            times.append((time.perf_counter() - t) * 1000)
        out[key] = (statistics.median(times), sorted(times)[min(len(times) - 1, int(0.95 * len(times)))], capped)
        print(f"  {engine.name} {key}: p50 {out[key][0]:.2f} ms, p95 {out[key][1]:.2f} ms, capped {capped}",
              flush=True)
    return out


if __name__ == "__main__":
    print(f"{N} entities, {len(edges)} relations, {SEEDS} seeds ({UNDIRECTED_SEEDS} for any-1hop)", flush=True)
    rows = [(e.name, measure(e)) for e in (Falkor(), Age())]
    print("\n| Engine | Load (s) | out-1hop p50 / p95 (ms) | out-2hop p50 / p95 (ms) | any-1hop p50 (ms) |")
    print("|---|---|---|---|---|")
    for name, r in rows:
        cap = f" ({r['any-1hop'][2]} hit the {CAP_S}s cap)" if r["any-1hop"][2] else ""
        print(f"| {name} | {r['load_s']:.1f} | {r['out-1hop'][0]:.2f} / {r['out-1hop'][1]:.2f} | "
              f"{r['out-2hop'][0]:.2f} / {r['out-2hop'][1]:.2f} | {r['any-1hop'][0]:.0f}{cap} |")
