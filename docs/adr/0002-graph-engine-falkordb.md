# ADR 0002: FalkorDB is the default graph engine (decision L1)

Status: accepted · Date: 2026-10-30

## Context

The graph store answers GraphRAG questions: from the entities found in the top chunks, which entities are 1–2 hops
away. The two candidates (PLAN §10, L1):

- **FalkorDB** (Redis module, sparse-matrix graph engine). License **SSPLv1**.
- **Apache AGE** (Postgres extension, Cypher over tables). License **Apache-2.0**, and no extra process.

## Benchmark

`benchmarks/graph_engines.py`, same machine (Docker Desktop, 12 CPUs), 20,000 entities and 60,000 relations, each
engine indexed as its documentation recommends (AGE: GIN on node properties, B-tree on edge `start_id`/`end_id`;
without the edge indexes a single AGE 1-hop query took over a minute). 200 random seeds, 5 for the undirected case.

| Engine | Load (s) | out 1-hop p50 / p95 (ms) | out 2-hop p50 / p95 (ms) | undirected 1-hop p50 (ms) |
|---|---|---|---|---|
| FalkorDB 4.22.3 | 6.2 | 1.72 / 2.97 | 1.72 / 2.64 | 2 |
| Apache AGE 1.6.0 (PG 17) | 36.0 | 3.87 / 6.80 | 124.36 / 188.40 | 63,092 |

Also observed: AGE's undirected pattern `(a)-[:R]-(b)` ignores the edge indexes and scans every edge (63 s per
query); variable-length `*1..2` is ~70x slower than writing the two hops out explicitly (8 ms vs 581 ms on one seed).
Using AGE well would mean rewriting every traversal swsdb generates.

## Decision

**FalkorDB is the default and only bundled graph engine for v1.** It is 70x faster on 2-hop traversals, handles
undirected traversal natively, and loads 6x faster.

The `GraphStore` adapter keeps the engine swappable: AGE remains a candidate for a "lite" profile (PLAN L2, everything
in Postgres) where its speed is acceptable for small graphs, and Neo4j for external connections.

## Consequences

- **Licensing:** FalkorDB is SSPLv1. Distributing it inside the free swsdb image is permitted (with its source available
  from upstream). **Offering swsdb as a hosted service would trigger SSPL section 13** for the service, so a hosted
  offering needs either a commercial license from FalkorDB or the AGE profile. Revisit before any hosted plan, with a
  lawyer.
- Graph data lives in FalkorDB's RDB + AOF files on the volume (`appendfsync everysec`): at most ~1 s of graph writes
  can be lost on a hard crash. The Phase 4 outbox replays them from Postgres, the source of truth.
- Graph isolation: one FalkorDB graph per collection (`sw_<tenant>__<collection>`).
