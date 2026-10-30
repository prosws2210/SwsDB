# swsdb

**One database for AI applications: relational, vector and graph in a single Docker container.**

> **Status: early development (Phases 0–2 of [PLAN.md](PLAN.md) done).** Working today: PostgreSQL 17, Qdrant 1.19
> and FalkorDB 4.22 in one supervised image; a REST API for collections, vectors, graph (Cypher) and SQL; tenants;
> admin and read-only API keys; a Python SDK and `swsdb` CLI; tested crash recovery and durability. Document
> ingestion, hybrid retrieval, consistency guarantees and the console are next; sections marked *(planned)* describe
> them. No image is published yet: build it from source (below).

## Try it now (from source)

```bash
git clone https://github.com/prosws2210/SwsDB.git && cd SwsDB
docker build -f docker/Dockerfile -t swsdb/swsdb:dev .
docker run -d --name swsdb -p 6464:6464 -v swsdb_data:/swsdb/data swsdb/swsdb:dev

curl http://localhost:6464/readyz        # {"status":"ready","engines":{"sql":…,"vector":…,"graph":…}}
curl http://localhost:6464/v1/status     # versions, state and latency of each engine
docker exec swsdb swsdb-admin status
docker exec swsdb swsdb-admin credentials   # native connection strings for psql, Qdrant clients, Cypher
```

Use it from Python (`pip install ./sdk/python`; on PyPI later):

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", tenant="acme")        # api_key=... once SWSDB_API_KEY is set

docs = db.create_collection("docs", vector_size=3)          # vectors + a graph
docs.upsert([{"id": "handbook#1", "vector": [1, 0, 0], "payload": {"team": "support"}}])
docs.search([1, 0, 0], limit=5, where={"team": "support"})  # [{"id": "handbook#1", "score": 1.0, ...}]

docs.cypher("CREATE (:Doc {title: $t})", {"t": "Refund policy"})
docs.cypher("MATCH (d:Doc) RETURN d.title", read_only=True)

db.sql("CREATE TABLE notes (id int PRIMARY KEY, body text)")
db.sql("SELECT * FROM notes WHERE id = %s", [1], read_only=True)
```

or the HTTP API directly (interactive docs at `http://localhost:6464/docs`):

```bash
curl -X PUT localhost:6464/v1/collections/docs -H 'content-type: application/json' -d '{"vector": {"size": 3}}'
curl -X POST localhost:6464/v1/collections/docs/search -H 'content-type: application/json' \
     -d '{"vector": [1, 0, 0], "limit": 5}'
```

Measured on a laptop (Docker Desktop, 12 CPUs): ready **9.7 s** after a cold start on an empty volume, **166 MB**
idle memory with all three engines, image **246 MB**.

AI applications need three kinds of storage at once: a **relational** store for users, permissions and documents, a
**vector** store for semantic search, and a **graph** store for entities and how they relate. Today that means running
PostgreSQL, Qdrant and a graph database side by side, then writing the glue yourself: shared IDs, chunking, embedding,
entity extraction, permission filters, and code that keeps all three in sync when a document changes or is deleted.

swsdb is that glue, shipped as a database server. You start it the way you start Qdrant:

```bash
docker run -d --name swsdb \
  -p 6464:6464 \
  -v swsdb_data:/swsdb/data \
  swsdb/swsdb:latest
```

Inside the container swsdb runs proven engines (PostgreSQL, Qdrant and a graph engine), supervises them, and puts one
API in front of them. Pick only the parts you need:

```bash
docker run -d -p 6464:6464 -e SWSDB_ENGINES=vector swsdb/swsdb          # just vector search
docker run -d -p 6464:6464 -e SWSDB_ENGINES=vector,graph swsdb/swsdb    # GraphRAG without SQL
docker run -d -p 6464:6464 swsdb/swsdb                                  # all three (default)
```

## What you get *(planned unless noted)*

- **Ingest once.** Send a document (PDF, Markdown, HTML, text). swsdb records it, chunks it, embeds the chunks into the
  vector store and extracts entities and relationships into the graph, all linked by the same `document_id` and
  `chunk_id`.
- **Retrieve once.** One search call combines dense and keyword (sparse) vector search with optional graph expansion,
  filters by the caller's permissions at every step, and returns evidence with its source: document, page, chunk and
  the graph path that led there.
- **Stays consistent.** Updating or deleting a document reaches every engine, even when one is briefly down: a
  transactional outbox, idempotent retries and a reconciliation job repair any drift. No orphaned vectors, no ghost
  entities.
- **Multi-tenant from day one.** Tenants, collections and access rules are part of the data model and are enforced in
  SQL, vector and graph queries alike.
- **Measures your retrieval.** Built-in evaluation (recall@k, MRR, nDCG, latency) against a golden set, so you can
  compare chunking, embedding models or graph settings with numbers instead of guesses.
- **See everything.** A web console for collections, ingestion jobs, failures, vector and graph growth and query
  latency; Prometheus metrics and OpenTelemetry traces for your own dashboards.
- **No lock-in.** The native engines stay reachable. Use `psql`, the Qdrant client or Cypher directly whenever you need
  something swsdb's API doesn't cover.
- **Works offline.** A small local embedding model is built in, so search works with no API key; switch to OpenAI,
  Cohere, Ollama or any OpenAI-compatible endpoint in one setting.
- **One backup.** `swsdb snapshot` captures all engines at one consistent point; `swsdb restore` brings them back
  together.
- **Built for agents.** A built-in MCP server lets coding agents and AI assistants search and update the knowledge
  base with the same permissions as your API.

## A taste of the SDK *(planned)*

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", api_key="...")
docs = db.collection("support-docs", tenant="acme")

docs.ingest("handbook.pdf", metadata={"team": "support"}, readers=["group:support"])

hits = docs.search(
    "What is the refund policy for damaged items?",
    k=5,
    graph_hops=1,          # also follow related entities
    as_user="user_42",     # only what this user may read
)
for h in hits:
    print(h.score, h.text[:80], h.source.document, h.source.page, h.graph_path)

docs.delete("handbook.pdf")   # removed from SQL, vectors and graph, with retries
```

Plain database access stays available:

```python
db.sql("SELECT id, title FROM documents WHERE tenant = %s", ["acme"])
db.cypher("MATCH (p:Product)-[:MENTIONED_IN]->(d:Document) RETURN p.name, count(d)")
```

## Configuration

Environment variables cover the common cases; mount a `swsdb.yaml` for the rest *(planned)*. Working today:
`SWSDB_ENGINES`, `SWSDB_API_KEY`, `SWSDB_READ_ONLY_API_KEY`, `SWSDB_EXPOSE_NATIVE` and `SWSDB_LOG_LEVEL`.
`SWSDB_EMBEDDINGS` and `SWSDB_LLM` arrive with ingestion (Phase 3).

| Variable | Default | Meaning |
|---|---|---|
| `SWSDB_ENGINES` | `sql,vector,graph` | Which engines to start |
| `SWSDB_API_KEY` | none | Full-access key; required once the port is reachable by others |
| `SWSDB_READ_ONLY_API_KEY` | none | Search-only key |
| `SWSDB_EMBEDDINGS` | `local` | `local`, `openai`, `cohere`, `ollama` or an OpenAI-compatible URL |
| `SWSDB_LLM` | none | Model used for entity extraction (graph ingestion is off without one) |
| `SWSDB_EXPOSE_NATIVE` | `false` | Also publish the engines' own ports (5432, 6333, 6379) |

| Port | What |
|---|---|
| `6464` | swsdb REST API, MCP endpoint and web console (`/console`) |
| `5432`, `6333`/`6334`, `6379` | Native PostgreSQL, Qdrant (HTTP/gRPC) and graph engine, when `SWSDB_EXPOSE_NATIVE=true` |

## How it fits together

```
                   your app · Python/TS SDK · CLI · MCP clients · web console
                                         │  :6464
┌────────────────────────────── swsdb container ───────────────────────────────┐
│  swsdb server: API · auth & tenants · ingestion · hybrid retrieval · eval     │
│        │              sync engine (outbox · retries · reconciliation)         │
│        ▼                         ▼                          ▼                 │
│   PostgreSQL               Qdrant                      graph engine           │
│   source of truth          dense + sparse vectors      entities & relations   │
│   docs · ACLs · jobs       chunk embeddings            Cypher                 │
│                                                                               │
│  supervisor: ordered start/stop · health checks · restarts · one data volume  │
└───────────────────────────────────────────────────────────────────────────────┘
```

## License

swsdb itself will be source-available under the Functional Source License (FSL-1.1-ALv2). The bundled engines keep
their own licenses (PostgreSQL License, Apache-2.0 for Qdrant, and the graph engine's license); see
[PLAN.md](PLAN.md#10-licensing) for how that affects distribution.
