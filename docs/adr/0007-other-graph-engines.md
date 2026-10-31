# ADR 0007: Neo4j and the Postgres-only lite profile

Status: accepted · Date: 2026-10-31 · Updated: 2026-10-31 (both implemented)

## Context

PLAN.md's Phase 7 lists a Neo4j adapter (an existing Neo4j as the graph engine) and an AGE "lite" profile (Postgres
alone, for small machines). The engine adapters (`SqlStore`, `VectorStore`, `GraphStore`) were built so engines can
change. Both turned out to need more than an adapter. Neither may weaken the two guarantees swsdb leads with: tenant
isolation and consistency.

## Lite profile: `SWSDB_PROFILE=lite`

Swapping FalkorDB for Apache AGE alone saves little memory (FalkorDB idles at 31 MB); Qdrant and the embedding model
are what a small machine notices. So the lite profile moves vectors into Postgres too (`server/swsdb_server/lite.py`):

- **Vectors:** one table per collection in the `swsdb_lite` schema, which no tenant role can read. Dense vectors use
  pgvector with HNSW (iterative scans, so filters don't starve results). BM25 keywords use an inverted index
  `(term, point, weight)`, scored with the IDF formula Qdrant applies. Hybrid search fuses the two rankings with RRF.
  Qdrant-style filters become JSONB containment, which the GIN index serves.
- **Graphs:** one AGE graph per collection, owned by the tenant's sandbox role, and every Cypher query runs as that
  role. AGE lets Cypher call Postgres functions, so this matters. A query can't reach another tenant's graph: the
  boundary is Postgres permissions, not query parsing. Queries go in dollar quotes with a tag derived from the query
  (they can't close them), and the column list AGE needs is read from the final `RETURN`. A wrong count can only
  make Postgres refuse the query. Writes to one graph are serialized with an advisory lock, so MERGE and the orphan
  clean-up are as safe as FalkorDB's atomic queries.
- **Snapshots:** everything is in `pg_dump`. AGE identifies a graph by its schema's OID, which a restore changes, so
  every start and restore re-points AGE's catalog (an idempotent step).

**Done means:** the security suite and the retrieval quality gate run on both profiles in CI, and lite meets the same
floors (it measured the same recall and answer rates as the default profile on the benchmark set). Lite needs
nothing under SSPL, which also removes the licensing blocker for a hosted offering (decision L1).

## Neo4j: `SWSDB_NEO4J_URL`

swsdb gives every collection its own graph; that is what makes pass-through Cypher safe (ADR 0004). Neo4j Community
has one user database. The adapter (`server/swsdb_server/neo4j.py`, over the HTTP Query API) therefore runs in a
**shared-database mode**:

- Every node a collection owns carries the collection's label (`C_<hash>`). swsdb's own queries are fixed strings
  in the server, and their Document, Chunk and Entity patterns are scoped to that label. Entity keys have a
  uniqueness constraint per collection.
- Writes lock the collection's registry node first, so one writer at a time per collection.
- **Pass-through Cypher is refused** (403): keeping a user's query inside one collection would need rewriting it, and
  that is a security boundary built on parsing. Graph-backed retrieval, reconciliation and snapshots (JSON export of
  the collection's subgraph) work, and retrieval meets the quality floors (`tests/image/test_neo4j.py`).

**Not done:** a database per collection on Neo4j Enterprise or Aura, which would allow pass-through Cypher there. It
needs an Enterprise license to test, so it stays on the roadmap. Database names allow letters, digits, `.` and `-`,
so `sw_acme__kb` would become `sw-acme--kb`.

## Consequences

- Graph engines: FalkorDB (bundled or external, default), Apache AGE (lite profile), Neo4j (external, retrieval
  only).
- The default profile stays the recommended one for large graphs: ADR 0002 measured FalkorDB much faster than AGE at
  multi-hop expansion.
- A snapshot restores only into the profile and graph engine it was taken with; moving between them goes through the
  API.
