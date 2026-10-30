# swsdb: project plan

Last updated: 2026-10-30 · Owner: Shakti Swaroop Sahu · Status: Phase 2 done, Phase 3 next

## Progress

| Phase | Status | Notes |
|---|---|---|
| 0 Spikes and decisions | **done** | Image size 246 MB, idle 166 MB, cold start 9.7 s. L1 decided: FalkorDB ([ADR 0002](docs/adr/0002-graph-engine-falkordb.md), 70x faster 2-hop than AGE). Container design: [ADR 0001](docs/adr/0001-one-container-with-s6.md) |
| 1 The container | **done** | s6-overlay, bootstrap, engine selection, health, crash recovery, durability; container test suite in CI |
| 2 API, adapters, SDK | **done** | REST v1 (collections, vectors, graph, SQL), adapters, Python SDK + `swsdb` CLI, read-only keys, tenants, sandboxed SQL. TLS moved to Phase 6 (terminate TLS at a proxy until then) |
| 3 Ingestion and hybrid retrieval | next | |
| 4–7 | planned | |

Decisions made while building (beyond the ADRs):
- **No catalog in Phase 2.** A collection is the engine objects named `sw_<tenant>__<collection>`; a vector-only
  deployment works without Postgres. Documents and the outbox (Phases 3–4) will require the SQL engine.
- **Pass-through SQL** connects as a separate non-superuser login role (`swsdb_app`, peer-mapped) and is server-side
  prepared, so it can't escalate, run several statements or leave read-only mode.
- The CLI ships in the SDK package (`pip install swsdb` gives `swsdb`) and inside the image.

## 1. The product in one paragraph

swsdb is a database server for AI applications, distributed as a Docker image and started like Qdrant
(`docker run -p 6464:6464 swsdb/swsdb`). One container runs PostgreSQL (relational), Qdrant (vector) and a graph engine,
supervises them, and exposes one API, SDK, CLI, web console and MCP server. Its value is not the bundling but what sits
on top: ingest a document once into all three stores with shared IDs, retrieve with vector + keyword + graph in one
permission-checked call, keep the three stores consistent through updates, deletions and outages, and measure
retrieval quality with built-in evaluation.

**Who it's for:** AI engineers building RAG, GraphRAG, agent memory and enterprise search, who today spend days wiring
three databases together and then maintain that glue forever.

**What it is not:** a new storage engine, a RAG framework you import, or a hosted service (at first). Engines are
proven open software; swsdb is the server around them.

## 2. Goals and non-goals

### Goals for v1.0
1. `docker run` gives a working relational + vector + graph store with a REST API in under a minute, on a laptop.
2. Engines are optional per deployment (`SWSDB_ENGINES`), and memory use is reasonable on 8 GB machines.
3. One ingestion call writes SQL rows, chunk vectors and graph entities linked by shared, stable IDs.
4. One retrieval call does dense + sparse + graph retrieval with permissions enforced at every stage.
5. Updates and deletions converge across all engines after any single-engine failure (proved by chaos tests).
6. Built-in retrieval evaluation and a web console.
7. Python SDK, CLI and MCP server; TypeScript SDK by v1.0.
8. One consistent snapshot and restore across all engines.

### Non-goals for v1
- Writing our own storage engine, query planner or query language.
- Distributed clustering, sharding or replication across machines (single-node first; see Phase 7).
- Distributed ACID transactions across engines (we guarantee eventual consistency with a clear model instead).
- Migrating data between engine vendors.
- A hosted cloud service.

## 3. What makes it worth using (the differentiators)

Bundling three databases in Docker is useful but easy to copy. These are the reasons someone picks swsdb over running
the three engines themselves, or over a RAG library:

| # | Differentiator | Why it matters | How swsdb does it |
|---|---|---|---|
| D1 | **Ingest once, linked everywhere** | Teams hand-write chunk → embed → upsert → extract → link code, and the IDs drift | One ingestion pipeline; `document_id`, `chunk_id`, `entity_id` are swsdb IDs used in every engine |
| D2 | **Consistency you can rely on** | The usual failure: Postgres says "deleted", Qdrant still returns the chunk | Transactional outbox in Postgres, idempotent versioned writes, tombstones, a reconciliation job, `swsdb doctor` |
| D3 | **Permission-aware hybrid retrieval** | Permission bugs in RAG leak other tenants' data into prompts | ACLs live in SQL and are pushed into Qdrant payload filters and graph traversal; denied content never leaves the server |
| D4 | **Vector + keyword + graph in one call** | GraphRAG is powerful but each team rebuilds it | Dense + sparse search, fusion, optional k-hop graph expansion, optional rerank, results with provenance and graph paths |
| D5 | **Built-in retrieval evaluation** | Nobody knows whether a chunking or model change helped | `swsdb eval` with golden sets: recall@k, MRR, nDCG, latency; compare two configs side by side |
| D6 | **One container, one volume, one backup** | Three engines mean three backup stories | Supervisor manages all engines; `swsdb snapshot` captures one consistent point |
| D7 | **Escape hatches** | Abstractions that hide the engines eventually block you | Native ports and raw SQL/Cypher/Qdrant access stay available; swsdb's metadata is documented |
| D8 | **Built for agents** | Agents need memory and search with guardrails | Built-in MCP server with read-only and read-write tool sets and the same ACLs as the API |
| D9 | **Observability built in** | Ingestion failures are silent until users complain | Console for jobs, failures, counts, latency; Prometheus metrics; OpenTelemetry traces |
| D10 | **Works offline by default** | API keys and egress block evaluation in companies | Bundled small embedding model; any provider in one setting |

The ones to lead with publicly are **D2 (consistency)**, **D3 (permissions)** and **D5 (evaluation)**: they're the
hardest to build yourself and the least covered by alternatives.

## 4. Landscape

| Option | What it is | Where swsdb differs |
|---|---|---|
| Running Postgres + Qdrant + Neo4j/FalkorDB yourself | Full control | swsdb is the glue: shared IDs, sync, permissions, eval, one backup |
| [R2R](https://r2r-docs.sciphi.ai/) (MIT) | RAG backend on Postgres + pgvector with graph extraction | swsdb uses a dedicated vector engine and graph engine, focuses on consistency, permissions and evaluation, and keeps native access |
| [Cognee](https://docs.cognee.ai/) (Apache-2.0) | Python memory layer with vector + graph backends | A library you import vs a database server you run; swsdb is language-agnostic over HTTP |
| Postgres + pgvector + Apache AGE | Everything inside Postgres | Simplest ops, but vector and graph performance is Postgres's; swsdb could offer this as a "lite" engine set (decision L2) |
| Qdrant / Weaviate / Neo4j alone | One excellent engine | swsdb combines them and owns the cross-engine problems |

These descriptions are from public docs at the time of writing; recheck before publishing comparisons.

## 5. Architecture

### 5.1 Inside the container

```
┌──────────────────────────────── swsdb container ─────────────────────────────────┐
│  s6-overlay (PID 1): ordered start, health checks, restarts, graceful shutdown     │
│                                                                                    │
│  swsdb-server (Python 3.12, FastAPI, uvicorn)                        :6464         │
│   ├─ API layer: REST /v1 · MCP /mcp · console /console · /metrics · /healthz       │
│   ├─ Auth: API keys (admin / read-only), tenants, ACL resolution                   │
│   ├─ Ingestion: parse → chunk → embed → extract entities → enqueue engine writes   │
│   ├─ Retrieval: dense + sparse + graph expansion → fusion → rerank → provenance    │
│   ├─ Sync engine: outbox worker · retries · reconciliation · tombstones            │
│   ├─ Evaluation: golden sets, metrics, comparisons                                 │
│   └─ Engine adapters: SqlStore · VectorStore · GraphStore (interfaces)             │
│                                                                                    │
│  PostgreSQL 17    127.0.0.1:5432   /swsdb/data/postgres                            │
│  Qdrant           127.0.0.1:6333/6334   /swsdb/data/qdrant                         │
│  Graph engine     127.0.0.1:6379 (FalkorDB) or in-Postgres (AGE)  /swsdb/data/graph│
└────────────────────────────────────────────────────────────────────────────────────┘
```

- **One container by design.** The user asked for the Qdrant experience: one image, one `docker run`, one volume.
  Multiple processes inside one container are handled by **s6-overlay**, a proven init/supervisor for exactly this
  (ordered startup with readiness, restart on crash, clean shutdown so engines flush to disk).
- **Engines listen on localhost only** unless `SWSDB_EXPOSE_NATIVE=true`.
- **A compose / Helm layout comes later** (Phase 7) for people who want engines in separate containers or external
  managed engines (for example an existing Neo4j or Qdrant Cloud). The adapters make that a configuration change.
- **Why Python for the server:** fastest path for this team and the AI ecosystem (parsers, embedding, extraction).
  The hot paths are inside the engines. Revisit (Go or Rust for the API and sync worker) only if profiling shows the
  server is the bottleneck.

### 5.2 Data model (owned by Postgres)

```
tenant ─┬─ api_key / principal (user, group)
        └─ collection ─┬─ document (id, version, status, source, metadata, acl)
                       │     └─ chunk (id, document_id, version, ordinal, text, page, hash)
                       │           ├─ vector point in Qdrant (id = chunk_id, payload: tenant, collection, doc, version, acl)
                       │           └─ MENTIONS edges in the graph
                       └─ entity (id, type, canonical name, aliases) ── RELATES_TO ── entity
outbox (id, op, target_engine, key, version, payload, attempts, status, error)
tombstone (kind, id, version, deleted_at)
eval_set / eval_case / eval_run
```

- IDs are swsdb-generated UUIDv7 (sortable), identical in all engines. Nothing is joined by name or text.
- Every engine write carries `(id, version)`. An older version never overwrites a newer one.
- Graph isolation: one graph per tenant (FalkorDB supports many named graphs cheaply), plus tenant checks in queries.
- Vector isolation: one Qdrant collection per swsdb collection with tenant payload index and mandatory filters.

### 5.3 Write path (ingest)

1. API validates, stores the original file and `document` (status `pending`) in Postgres.
2. Parse → clean → chunk (structure-aware: headings, tables, pages) → hash chunks (skip unchanged on re-ingest).
3. In **one Postgres transaction**: write chunks, set document `version+1`, insert outbox rows
   (`vector.upsert`, `graph.upsert`, and `vector.delete` / `graph.delete` for removed chunks).
4. Outbox worker embeds (batched, cached by chunk hash) and writes to Qdrant; extracts entities with the configured
   LLM (optional), resolves duplicates, writes to the graph.
5. Document status goes to `ready` only when all its outbox rows succeed; partial states are visible in the console.

### 5.4 Read path (retrieve)

1. Resolve the caller's principals → allowed collections and ACL filter (cached briefly).
2. Dense and sparse search in Qdrant **with the ACL filter applied inside the query**.
3. Optional graph expansion: entities mentioned by the top chunks → k-hop neighbors → their chunks (ACL-checked again).
4. Fuse (reciprocal rank fusion), optional rerank (cross-encoder or provider), drop stale versions.
5. Return chunks with document, page, score breakdown and the graph path that led to each.

### 5.5 Consistency model (the core promise)

- **Postgres is the source of truth.** Qdrant and the graph are derived and can always be rebuilt from Postgres.
- **Read-your-writes per document** after `status = ready`; eventual consistency before that, with an explicit status.
- **Deletes:** document marked deleted + tombstone + outbox rows in one transaction; search filters tombstoned IDs
  immediately even before engines catch up, so deleted content never appears in results.
- **Retries:** exponential backoff, idempotent writes keyed by `(id, version)`, poison rows parked with their error.
- **Reconciliation:** periodic and on demand (`swsdb doctor --repair`) comparing counts and ID sets per collection,
  re-enqueueing missing writes and deleting orphans.
- **Proof:** chaos tests kill each engine mid-ingest and mid-delete and assert convergence (Phase 4 exit criteria).

### 5.6 Interfaces

| Surface | v0.1 | v1.0 |
|---|---|---|
| REST API (OpenAPI) | collections, documents, search, sql, cypher, health | + eval, snapshots, admin, webhooks |
| Python SDK `pip install swsdb` | sync + async client | + LangChain and LlamaIndex integrations |
| CLI `swsdb` | status, ingest, search, doctor | + snapshot/restore, eval, tenants |
| Web console `/console` | health, collections, jobs | + search playground, graph explorer, eval reports |
| MCP server `/mcp` | search, get_document | + ingest/delete (read-write key only) |
| TypeScript SDK | none | yes |
| Native engine ports | opt-in | opt-in |

### 5.7 Configuration

Environment variables for the common cases (see README); a mounted `/swsdb/config/swsdb.yaml` for the rest:

```yaml
engines: [sql, vector, graph]
graph:
  engine: falkordb            # falkordb | age (in Postgres) | neo4j (external only)
embeddings:
  provider: local             # local | openai | cohere | ollama | openai-compatible
  model: BAAI/bge-small-en-v1.5
extraction:
  llm: openai:gpt-4.1-mini    # unset: graph gets document/chunk nodes only, no entities
chunking: { strategy: structure, max_tokens: 512, overlap: 64 }
retrieval: { fusion: rrf, rerank: none, graph_hops: 1 }
limits: { max_upload_mb: 100, ingest_workers: 2 }
```

## 6. Tech stack

| Area | Choice | Notes |
|---|---|---|
| Base image | Debian 12 slim | multi-arch: linux/amd64, linux/arm64 |
| Supervisor | s6-overlay v3 | readiness, restarts, graceful stop |
| Relational | PostgreSQL 17 | PostgreSQL License |
| Vector | Qdrant (pinned release binary) | Apache-2.0; dense + sparse vectors |
| Graph (default) | FalkorDB module on Redis-compatible server | SSPLv1, see Licensing |
| Graph (alt) | Apache AGE in Postgres | Apache-2.0, no extra process |
| Graph (alt) | Neo4j | external connection only (JVM size, license) |
| Server | Python 3.12, FastAPI, uvicorn, Pydantic v2 | psycopg 3, qdrant-client, falkordb client |
| Parsing | pypdf / pdfminer, markdown, HTML → text, optional unstructured | pluggable |
| Embeddings | fastembed (ONNX, CPU) default | providers via HTTP |
| Extraction | LLM with structured output; optional spaCy NER fallback | |
| Console | Next.js static export served by the server | |
| Observability | Prometheus client, OpenTelemetry | |
| Tests | pytest, testcontainers, chaos scripts, k6 / locust | |
| CI/CD | GitHub Actions: tests, image build, Trivy scan, SBOM, multi-arch push to Docker Hub + GHCR | |

## 7. Roadmap

Estimates are for one developer working consistently; adjust after Phase 0.

### Phase 0: Spikes and decisions (week 1)
- Prove s6-overlay runs Postgres + Qdrant + FalkorDB in one image; measure image size and idle RAM.
- Prove AGE as the in-Postgres graph option; compare with FalkorDB on a small GraphRAG workload.
- Decide L1–L4 (section 10). Write ADRs.
- **Done when:** a throwaway image starts all three engines and survives `docker stop` without data loss.

### Phase 1: The container (weeks 2–3)
- Dockerfile, s6 services, readiness checks, one data volume layout, `SWSDB_ENGINES` selection.
- `/healthz` and `/readyz` reporting each engine. Graceful shutdown order.
- Persistent data across restarts and image upgrades within a minor version.
- **Done when:** `docker run swsdb/swsdb` is ready in <30 s on a laptop; kill -9 of any engine is restarted and
  reported; data survives `docker rm` + new container on the same volume.

### Phase 2: API, adapters and SDK (weeks 3–5)
- Adapter interfaces `SqlStore`, `VectorStore`, `GraphStore`; implementations for Postgres, Qdrant, FalkorDB.
- REST v1: tenants, collections, raw upsert/search of vectors, `sql` and `cypher` pass-through (parameterised).
- API keys (admin / read-only); TLS option.
- Python SDK and CLI basics.
- **Done when:** the SDK can create a collection, upsert vectors, search, run SQL and Cypher against a fresh container.

### Phase 3: Ingestion and hybrid retrieval (weeks 5–7)
- Parsers, structure-aware chunking, local embeddings, optional providers.
- Entity extraction and deduplication into the graph.
- Hybrid search with ACL filters, fusion, graph expansion, provenance.
- **Done when:** ingest a 200-page PDF and get cited answers; graph expansion measurably adds relevant chunks on the
  sample dataset.

### Phase 4: Consistency (weeks 7–9)
- Outbox, worker, retries, versioning, tombstones, reconciliation, `swsdb doctor --repair`.
- Chaos test suite: kill Qdrant / graph / server during ingest, update and delete.
- **Done when:** 100 chaos runs end converged with zero orphans and zero deleted content returned.

### Phase 5: Console, evaluation and observability (weeks 9–11)
- Web console: health, collections, documents and their per-engine status, failed jobs, search playground,
  graph explorer.
- `swsdb eval`: golden sets, metrics, config comparisons, report in the console.
- Prometheus metrics, OpenTelemetry traces.
- **Done when:** a user can see why a document isn't searchable and which config retrieves better, without logs.

### Phase 6: Hardening and public v0.1 (weeks 11–13)
- `swsdb snapshot` / `restore` across engines; upgrade path between versions.
- MCP server (read-only and read-write).
- Security review: tenant isolation tests, injection tests on SQL/Cypher pass-through, secrets handling.
- Benchmarks published: ingest throughput, query latency p50/p95, RAM per million chunks.
- Docs site, quickstarts (RAG app, GraphRAG app, agent memory), example repos.
- Docker Hub + GHCR release, multi-arch, signed images, SBOM.
- **Done when:** a new user goes from `docker run` to a working cited-answer demo in 10 minutes following the docs.

### Phase 7: After v0.1
- TypeScript SDK; LangChain / LlamaIndex integrations.
- Compose and Helm layouts with engines in separate containers; external managed engines.
- Neo4j adapter (external), AGE "lite" profile.
- Replication / HA story; scheduled backups to S3-compatible storage.
- Possible hosted offering (depends on L1).

## 8. Testing strategy
- **Unit:** adapters, chunking, fusion, ACL resolution, versioning rules.
- **Integration:** every API endpoint against a real container (testcontainers), per engine selection.
- **Chaos:** engine kills and network delays during writes; convergence assertions (Phase 4).
- **Security:** cross-tenant probes on every read path, including graph expansion and MCP tools.
- **Retrieval quality:** a fixed public dataset with golden questions; regressions fail CI.
- **Performance:** k6 / locust load tests; tracked over releases.
- **Upgrade:** start version N, write data, upgrade to N+1, verify.

## 9. Distribution and operations
- Images: `swsdb/swsdb:<version>` and `:latest` on Docker Hub and `ghcr.io/<owner>/swsdb`; amd64 + arm64.
- Image targets: < 1 GB compressed, idle RAM < 1 GB with all engines and the default embedding model (to verify in
  Phase 0; the embedding model loads lazily).
- Volumes: `/swsdb/data/{postgres,qdrant,graph,files,swsdb}`; config in `/swsdb/config`.
- Runs as a non-root user; read-only root filesystem supported.
- Versioning: SemVer; data format changes only in minor versions with automatic migration and a documented rollback.

## 10. Licensing

- **swsdb's own code:** source-available under FSL-1.1-ALv2 (same model as AgentCrucible): anyone may use it,
  including companies internally; nobody may sell it, or something substantially similar, as a competing product or
  service. Each version becomes Apache-2.0 after two years.
- **Bundled engines keep their licenses** and their notices ship in the image (`/licenses`):
  - PostgreSQL: PostgreSQL License (permissive).
  - Qdrant: Apache-2.0 (permissive).
  - FalkorDB: **SSPLv1** ([FalkorDB docs](https://docs.falkordb.com/)). Redistributing it inside a free image is
    allowed with its source, but **offering swsdb as a hosted service** would trigger SSPL's service-source
    obligations or need a commercial license from FalkorDB.
  - Apache AGE: Apache-2.0 ([project page](https://age.incubator.apache.org/overview)).
  - Neo4j Community: reported as GPLv3; verify on Neo4j's own licensing page. Planned as an external connection only.

### Open decisions

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| L1 | Default graph engine | FalkorDB (fast GraphRAG, SSPL) · Apache AGE (in Postgres, Apache-2.0) | **Decided: FalkorDB** ([ADR 0002](docs/adr/0002-graph-engine-falkordb.md)); hosted offering needs a FalkorDB commercial license or the AGE profile |
| L2 | "Lite" profile | none · Postgres-only (pgvector + AGE) | Offer later as `SWSDB_PROFILE=lite` for tiny machines |
| L3 | API port | 6464 · other | 6464 (no clash with the engines' defaults); confirm no common clash |
| L4 | Server language | Python · Go · Rust | Python for v1; revisit with profiling |
| L5 | Engine pinning | pin exact engine versions per swsdb release | Yes; upgrade engines only in minor releases with migration tests |

Get a lawyer's review of the bundling and the hosted-service question before the first public image.

## 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Several engines in one container use too much RAM | Bad laptop experience | Engine selection, lazy model loading, tuned defaults, measured in Phase 0 |
| Engine upgrade breaks on-disk data | Data loss | Pinned versions, upgrade tests, snapshots before migration |
| Consistency bugs | Loss of trust (the core promise) | Chaos suite in CI, reconciliation, Postgres as rebuildable source of truth |
| Permission leak through graph expansion | Security incident | ACL check at every hop, cross-tenant tests on every read path |
| Graph-engine licensing blocks a hosted product | Business model | Decision L1, AGE alternative |
| Scope creep (clustering, query language) | Never ships | Non-goals list; v0.1 scope frozen at Phase 6 |
| Seen as "just a docker-compose" | No adoption | Lead with D2, D3, D5 and publish benchmarks and chaos results |

## 12. Success metrics for v0.1
- Time from `docker run` to first cited answer: < 10 minutes with the quickstart.
- Chaos suite: 100% convergence; zero deleted content returned.
- Cross-tenant tests: zero leaks.
- Retrieval: graph expansion improves recall@10 over vector-only on the benchmark dataset (or we say clearly where it
  doesn't).
- Published numbers: image size, idle RAM, ingest throughput, p95 search latency.

## 13. Repository layout (planned)

```
swsdb/
  server/            swsdb-server (FastAPI): api/, auth/, ingest/, retrieval/, sync/, eval/, adapters/
  sdk/python/        pip install swsdb
  sdk/typescript/    (Phase 7)
  cli/               swsdb CLI
  console/           web console (Next.js static export)
  docker/            Dockerfile, s6 service definitions, engine configs
  tests/             unit, integration, chaos, security, perf
  benchmarks/        datasets, golden sets, scripts, results
  docs/              docs site, ADRs
  LICENSE            FSL-1.1-ALv2
  THIRD_PARTY_LICENSES/
```

## 14. Immediate next steps
1. Create the repository and the Phase 0 spike branch.
2. Build the spike image (Postgres + Qdrant + FalkorDB under s6) and measure size, idle RAM and start time.
3. Benchmark FalkorDB vs AGE on a small GraphRAG task; record decision L1 as an ADR.
4. Freeze the v0.1 API shape (OpenAPI draft) before writing the server.
