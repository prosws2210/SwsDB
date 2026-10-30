# Architecture

```text
            REST /v1 · MCP /mcp · console /console · /metrics
┌──────────────────────────── swsdb container ─────────────────────────────┐
│  swsdb server (Python, FastAPI)                                           │
│    auth: keys → tenant + role       retrieval: hybrid + graph + readers   │
│    documents: parse → chunk → embed (local ONNX or any endpoint)          │
│    outbox workers ─ idempotent, retried ─ reconciliation every 10 min     │
│        │                     │                        │                   │
│   PostgreSQL 17          Qdrant 1.19             FalkorDB 4.22            │
│   catalog, chunks,       dense + BM25 vectors,   documents, passages,     │
│   entities, outbox,      payload ACL filter      entities, relations      │
│   tenants' SQL                                                            │
│  s6-overlay supervises all four; one volume /swsdb/data; one lock on it   │
└───────────────────────────────────────────────────────────────────────────┘
```

- **One container, supervised** ([ADR 0001](adr/0001-one-container-with-s6.md)). s6-overlay starts the engines in
  order, restarts what crashes, and shuts down cleanly. A lock on the volume refuses a second container on the same
  data.
- **FalkorDB for the graph** ([ADR 0002](adr/0002-graph-engine-falkordb.md)): two-hop expansion in about 2 ms where
  Apache AGE took over 100 ms on the same data.
- **Postgres is the source of truth** ([ADR 0003](adr/0003-consistency-model.md)). Changes are accepted in one
  transaction with an outbox row. Workers apply them to Qdrant and the graph idempotently, and versions switch
  atomically for readers. Reconciliation finds and repairs drift. 100 chaos rounds of killed engines and servers ended
  converged, with no deleted content returned.
- **Tenants** ([ADR 0004](adr/0004-tenant-isolation.md)): keys bound to a tenant, a collection per tenant in each
  store, a Postgres role and schema per tenant for SQL.
- **Snapshots** ([ADR 0005](adr/0005-snapshots.md)): the derived stores pause while every engine is copied natively, at
  one consistent point.

## Request paths

**Ingest.** `POST /documents` → catalog row, file and outbox row in one transaction (202). A worker then:

1. parses and chunks;
2. embeds into Qdrant (hidden);
3. extracts entities (optional) and stores them in Postgres;
4. rebuilds the document's subgraph from the catalog;
5. switches the version on.

**Query.**

1. Embed the question; a search takes priority over document embedding.
2. Run dense and BM25 searches in Qdrant with the readers filter, and fuse them (RRF).
3. Keep only live versions (checked against the catalog).
4. Expand through the graph: neighbouring passages, shared entities, and with 2 hops, related entities.
5. Pass graph candidates through the same Qdrant filter.
6. For `answer`: number the passages and write the answer, or pick sentences, citing `[n]`.

**Isolation in each step.** The tenant selects the collection. The readers filter runs inside Qdrant. The catalog check
drops deleted or superseded versions. Nothing a caller may not read reaches the response, the model or an MCP client.
