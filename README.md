# swsdb

**One database for AI applications: relational, vector and graph in a single Docker container.**

> **Status: early development (Phases 0–5 of [PLAN.md](PLAN.md) done).** Working today: PostgreSQL 17, Qdrant 1.19
> and FalkorDB 4.22 in one supervised image; document ingestion (PDF, Markdown, HTML, text) with hybrid retrieval
> (dense + BM25) and graph expansion; permissions per document; cross-store consistency (outbox, retries,
> reconciliation) proven by chaos tests; a web console; retrieval evaluation; Prometheus metrics and OpenTelemetry
> traces; a REST API for collections, vectors, graph (Cypher) and SQL; tenants; admin and read-only API keys; a Python
> SDK and `swsdb` CLI. Backups, the MCP server and hardening are next; items marked *(planned)* describe them. No
> image is published yet: build it from source (below).

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

Ingest documents and ask questions (Python, `pip install ./sdk/python`; on PyPI later):

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", tenant="acme")
kb = db.create_collection("handbook", documents=True)      # chunked, embedded (offline model) and graphed for you

kb.upload("employee-handbook.pdf", readers=["group:staff"])
kb.add_text("refunds", "# Refunds\n\nRefunds are issued within five business days.", metadata={"team": "support"})
kb.wait_ready("employee-handbook.pdf")

for hit in kb.query("how long do refunds take?", k=5, principals=["user:42", "group:staff"]):
    print(hit["score"], hit["via"], hit["document"], hit["page"], hit["heading"], hit["text"][:80])
```

Each hit cites its document, page and heading. `via` says how it was found (`hybrid` search, or `graph` with a
`graph_path` such as "mentions Maria Lopez"). Set `SWSDB_LLM_URL` and `SWSDB_LLM_MODEL` (any OpenAI-compatible
model) to also extract entities, so questions can hop between documents that mention the same people, products or
projects.

Lower level, with vectors you embed yourself:

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
idle memory with all three engines (the embedding model loads on first use), image **369 MB** including
the offline embedding models. A generated 200-page PDF is ingested in **12.6 s** and cited by page.

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

## What you get

- **Ingest once.** *(working)* Send a document (PDF, Markdown, HTML, text). swsdb records it, chunks it, embeds the chunks into the
  vector store and extracts entities and relationships into the graph, all linked by the same `document_id` and
  `chunk_id`.
- **Retrieve once.** *(working)* One search call combines dense and keyword (sparse) vector search with optional graph expansion,
  filters by the caller's permissions at every step, and returns evidence with its source: document, page, chunk and
  the graph path that led there.
- **Stays consistent.** *(working)* Updating or deleting a document reaches every engine, even when one is down: a
  transactional outbox, idempotent retries and a reconciliation job repair any drift. No orphaned vectors, no ghost
  entities, and a deleted document never comes back in results. See [Consistency](#consistency).
- **Multi-tenant from day one.** *(working)* Tenants, collections and access rules are part of the data model and are enforced in
  SQL, vector and graph queries alike.
- **Measures your retrieval.** *(working)* Built-in evaluation (recall@k, MRR, nDCG, latency) against a golden set, so you can
  compare chunking, embedding models or graph settings with numbers instead of guesses.
- **See everything.** *(working)* A web console for collections, ingestion jobs, failures, vector and graph growth and query
  latency; Prometheus metrics and OpenTelemetry traces for your own dashboards.
- **No lock-in.** *(working)* The native engines stay reachable. Use `psql`, the Qdrant client or Cypher directly whenever you need
  something swsdb's API doesn't cover.
- **Works offline.** *(working)* A small local embedding model is built in, so search works with no API key; switch
  to OpenAI, Ollama or any OpenAI-compatible endpoint in one setting.
- **One backup.** *(planned)* `swsdb snapshot` captures all engines at one consistent point; `swsdb restore` brings them back
  together.
- **Built for agents.** *(planned)* A built-in MCP server lets coding agents and AI assistants search and update the knowledge
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

## Console

Open `http://localhost:6464/console` (enter the API key once keys are set). It works offline, like the rest of swsdb.

| | |
|---|---|
| ![Overview: engines, consistency backlog, collections](docs/img/console-overview.jpg) | ![Search playground with how each hit was found](docs/img/console-search.jpg) |
| **Overview**: engine health, changes waiting to reach every store, collections. | **Search**: try queries as any user; every hit shows how it was found. |
| ![Evaluation: four configurations compared on a golden set](docs/img/console-evaluation.jpg) | ![Document diagnosis: why a document is or isn't searchable](docs/img/console-diagnosis.jpg) |
| **Evaluation**: compare configurations on your own questions. | **Diagnosis**: why a document is or isn't searchable, per store. |

Also: a graph explorer (entities, what they're related to, where they're mentioned) and a Doctor page (check and
repair consistency).

## Evaluation

Write down a few questions and the documents that should answer them, then compare configurations with numbers:

```python
kb.put_eval_set("support-faq", [
    {"query": "How long do refunds take?", "relevant": ["refunds"], "answer": "five business days"},
    {"query": "Where do orders ship from?", "relevant": ["shipping"]},
])
run = kb.evaluate("support-faq", k=5)   # hybrid, dense, keyword and hybrid+graph by default
for r in run["results"]:
    print(r["config"]["name"], r["metrics"])   # hit, recall, mrr, ndcg, answer_found, evidence, latency p50/p95
```

Runs are stored, so a change to chunking, the embedding model or graph settings can be compared with the last run.

## Monitoring

- `GET /metrics`: Prometheus metrics (API key required once keys are set): engine health and probe latency, query
  and ingestion latency, documents by status, the sync backlog and its oldest change, failures by kind, requests per
  route.
- Traces: set `OTEL_EXPORTER_OTLP_ENDPOINT` (and optionally `OTEL_SERVICE_NAME`) to send OpenTelemetry traces over
  OTLP/HTTP.
- `GET /v1/collections/{name}/diagnose/{document}` (or `kb.diagnose(...)`): one document across all three stores,
  with a plain verdict.

## Consistency

Three engines, no shared transaction, so swsdb makes the guarantees itself ([ADR 0003](docs/adr/0003-consistency-model.md)):

- **Accepted means durable.** An upload or delete is committed to Postgres, the source of truth, together with an
  outbox entry before the API answers. Uploads and deletes keep working while Qdrant or FalkorDB is down; they are
  applied when it's back.
- **Readers see whole versions only.** A new version becomes visible in one step when every store has it; queries are
  checked against the catalog, so they never return a deleted document or a stale version, even before the stores
  have caught up.
- **Failures heal themselves.** Engine outages are retried with backoff (and immediately when the engine returns);
  crashes resume where they stopped; a broken file fails with its reason.
- **Drift is found and fixed.** Reconciliation compares the catalog with what Qdrant and the graph actually hold and
  repairs missing vectors, stale versions, orphans and graph mismatches, rebuilding the graph from the catalog without
  calling the LLM again. It runs every 10 minutes and on demand:

```bash
swsdb doctor            # report
swsdb doctor --repair   # fix
```

Proven by chaos tests that kill or pause Postgres, Qdrant, FalkorDB and the server mid-write and check, after every
round, that all three stores agree with the catalog (latest run: **100 of 100 rounds
consistent**: Postgres killed 13x, Qdrant killed 22x, FalkorDB killed 16x, the swsdb server killed 18x, Qdrant held
down 14x, FalkorDB held down 17x; no lost writes, no deleted document ever returned).

## Configuration

Environment variables cover the common cases; mount a `swsdb.yaml` for the rest *(planned)*. Working today:
`SWSDB_ENGINES`, `SWSDB_API_KEY`, `SWSDB_READ_ONLY_API_KEY`, `SWSDB_EXPOSE_NATIVE`, `SWSDB_LOG_LEVEL`,
`SWSDB_EMBEDDINGS` (`local` or `openai` with `SWSDB_EMBEDDINGS_URL` / `_MODEL` / `_API_KEY` for any OpenAI-compatible
endpoint), `SWSDB_LLM_URL` / `SWSDB_LLM_MODEL` / `SWSDB_LLM_API_KEY` (entity extraction), `SWSDB_INGEST_WORKERS` and
`SWSDB_MAX_UPLOAD_MB` and `SWSDB_RECONCILE_INTERVAL_S` (default 600; 0 = on request only).

| Variable | Default | Meaning |
|---|---|---|
| `SWSDB_ENGINES` | `sql,vector,graph` | Which engines to start |
| `SWSDB_API_KEY` | none | Full-access key; required once the port is reachable by others |
| `SWSDB_READ_ONLY_API_KEY` | none | Search-only key |
| `SWSDB_EMBEDDINGS` | `local` | `local`, `openai`, `cohere`, `ollama` or an OpenAI-compatible URL |
| `SWSDB_LLM_URL`, `SWSDB_LLM_MODEL` | none | OpenAI-compatible model for entity extraction (without it the graph holds document structure only) |
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
