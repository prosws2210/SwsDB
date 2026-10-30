# ADR 0007: Neo4j and the Postgres-only "lite" profile: designed, not shipped in v0.1

Status: accepted (deferred) · Date: 2026-10-31

## Context

PLAN.md's Phase 7 lists a Neo4j adapter (an existing Neo4j as the graph engine) and an AGE "lite" profile (Postgres
alone, for small machines). The engine adapters (`SqlStore`, `VectorStore`, `GraphStore`) were built so engines can
change. While implementing Phase 7, both turned out to need more than an adapter. Shipping them half-done would weaken
the two guarantees swsdb leads with: tenant isolation and consistency.

## What each needs

**Neo4j (external).** swsdb gives every collection its own graph. That is what makes pass-through Cypher safe: a query
can't name another collection's graph (ADR 0004). Neo4j Community has one user database:

- Every collection would share it.
- Isolation would depend on rewriting user Cypher.
- That is a security boundary built on parsing, which swsdb deliberately avoids.

With Neo4j Enterprise or Aura (several databases), a collection maps to a database (names allow letters, digits, `.`
and `-`, so `sw_acme__kb` becomes `sw-acme--kb`). The adapter then needs:

- Bolt queries with native parameters;
- the expansion and graph-write Cypher, which is portable apart from FalkorDB's `CYPHER` header;
- snapshots through `neo4j-admin database dump`, which needs the database stopped or online backup (Enterprise),
  instead of `DUMP`/`RESTORE` per key;
- a schema-aware result decoder.

**Decision: support Neo4j only with multi-database (Enterprise, Aura Business), as a separate adapter with its own
test suite.** Pass-through Cypher on a shared database stays refused.

**AGE "lite".** Swapping FalkorDB for Apache AGE is small: AGE speaks Cypher inside Postgres. But it is 70x slower at
two-hop expansion (ADR 0002), and it saves only ~30 MB, because FalkorDB idles at 31 MB. The memory a small machine
needs comes from Qdrant and the embedding model. A real "lite" profile therefore means vectors in Postgres too
(pgvector). That needs:

- a `VectorStore` over pgvector that implements what retrieval asks of Qdrant: dense search with payload filters,
  sparse BM25 search with IDF weighting, reciprocal-rank fusion, has-id filters for graph expansion, and paging scans
  for reconciliation;
- a migration between profiles (re-embed or copy vectors).

**Decision: build it as `SWSDB_PROFILE=lite` = Postgres + pgvector (dense and `sparsevec`) + AGE.** Retrieval quality
must pass the same CI gate as the default profile before it is offered. Until then the default profile is the
supported one, and `SWSDB_ENGINES=sql,vector` already runs without a graph engine.

## Consequences

- v0.1 supports FalkorDB as the graph engine, bundled or external (`SWSDB_FALKORDB_URL`).
- Both adapters stay on the roadmap with the scope above; the quality gate and the security suite define "done" for
  each.
