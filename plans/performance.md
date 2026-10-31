# swsdb performance plan: millisecond reads at million scale

**Goal:** searching and fetching data in swsdb takes single-digit milliseconds on the server, with a million table rows,
a million vectors (passages) and a million graph relationships, on ordinary hardware.

**Status:** plan, not started. Written 2026-10-31 against commit `46b97fc`.

**How to read this:** section 1 is what we measured today and why it's slow. Section 2 sets the targets, and what's
realistic. Section 3 is the work, in five phases, each with what changes, where in the code, how it's measured and
when it counts as done. Sections 4 to 7 are how we verify, the risks, the decisions needed and the sources.

---

## 1. Where we are

### 1.1 How it was measured

- Server: the local `swsdb` container (all three engines: PostgreSQL 17, Qdrant 1.19, FalkorDB 4.22), default settings,
  the bundled `bge-small-en-v1.5` embedding model on CPU, one uvicorn process.
- Machine: AMD Ryzen 7 7445HS laptop, Docker Desktop on Windows (7.4 GB VM).
- Client: `curl` run **inside the container**, so Docker Desktop's port forwarding (about 40 ms from the Windows host)
  isn't counted. 60 requests per endpoint after 5 warm-up requests; median (p50) and 95th percentile (p95) of the total
  request time.
- Data: project `store`: 7 tables (200 customers, 120 products, 800 orders, ~1,600 order items, 300 reviews), vector
  collections of 80–500 points, a 5-document knowledge graph and two graphs of 11–12 nodes. **Small data**: the
  numbers below are almost entirely overhead, which is the point of this first measurement. Scale numbers come in
  phase 0.
- Script: `baseline.sh` (kept with the benchmarks in phase 0).

### 1.2 Results

| Request | p50 | p95 | Engine's own time (approx.) | Where the rest goes |
|---|---|---|---|---|
| `GET /healthz` (the framework's floor) | 2.2 ms | 3.5 ms | – | uvicorn + FastAPI + curl process start |
| `GET /v1/status` (auth + engines) | 3.2 ms | 4.9 ms | – | auth, engine status |
| SQL: row by primary key | 6.5 ms | 7.8 ms | ~0.05 ms | pool checkout, 3–4 Postgres round trips (begin, read-only, statement timeout, the statement), result conversion, JSON |
| SQL: join + aggregate (800 orders) | 7.2 ms | 8.7 ms | ~0.5 ms | same |
| Data API: row by filter | 8.0 ms | 9.8 ms | ~0.1 ms | same, plus building the query and the users' role |
| Vector: get 1 point | 8.6 ms | 12.6 ms | ~0.3 ms | a Postgres catalog lookup (`documents_guard`), JSON over HTTP to Qdrant |
| Vector: similar by id | 9.5 ms | 11.0 ms | ~1 ms | same |
| Vector: filtered scroll | 9.2 ms | 11.3 ms | ~0.5 ms | same |
| Qdrant gate (`/qdrant`): query by id | 8.4 ms | 10.9 ms | ~1 ms | catalog lookup, body rewrite, proxy hop |
| Graph: 1-hop neighbourhood | 7.6 ms | 9.5 ms | <1 ms | catalog/existence checks, FalkorDB reply decoding |
| Graph: 2-hop path | 6.4 ms | 8.5 ms | <1 ms | same |
| Documents: keyword (BM25) search | 11.2 ms | 16.1 ms | ~2 ms | collection lookup, sparse encoding, Qdrant, a Postgres round trip to drop stale versions |
| Documents: dense search | 31.0 ms | 46.3 ms | ~2 ms | **~20 ms embedding the question on the CPU** |
| Documents: hybrid search | 29.9 ms | 44.5 ms | ~2 ms | same |
| Documents: hybrid + graph (1 hop) | 32.1 ms | 56.1 ms | ~3 ms | plus 2–3 sequential FalkorDB queries, a Qdrant call and a Postgres call |
| Documents: the same question again | 30.0 ms | 45.5 ms | – | **nothing is cached** |

From the v0.1.0 benchmarks (`docs/benchmarks.md`, 1,000 documents): with **8 concurrent clients**, hybrid search p50
rises to about **200 ms** at 38 queries a second, because one embedding runs at a time (a lock in `embed.py`) and
every request queues for the model.

### 1.3 What the search path does today

`Documents.query` in `server/swsdb_server/documents.py` runs these steps **one after another**:

1. `collection()`: a Postgres query for the collection's settings (every request, no cache).
2. `embedder.dense()`: the question through bge-small on the CPU, under a process-wide lock (~15–20 ms).
3. `embedder.sparse()`: BM25 token weights.
4. `vector.query()`: Qdrant over HTTP with a JSON body, returning full payloads (passage text included).
5. `current()`: a Postgres query to keep only each document's live version.
6. With the graph: `_expand()` runs 2–3 Cypher queries in sequence, then a Qdrant `scroll` for the permission
   check, then `current()` again.

Every raw vector and graph endpoint also calls `documents_guard`, which runs
`is_document_collection()`: another Postgres query.

### 1.4 What this tells us

- **The engines are fast.** Qdrant (Rust), FalkorDB (C, GraphBLAS) and Postgres answer these requests in well under a
  millisecond to a couple of milliseconds. The rest is swsdb.
- **Python overhead is 4–8 ms per request**: uncached catalog lookups, several database round trips, JSON encoding and
  middleware.
- **The CPU embedding is ~20 ms**, is never cached, and is serialized across all requests.
- API keys are **already** cached in memory (`auth.py` `KeyStore._load`), so key checking isn't the problem.

---

## 2. Targets

### 2.1 What we aim for

Server-side, measured inside the network, warm cache, at **1 million rows, 1 million vectors (384 dimensions) and
1 million+ relationships**:

| Request | Target p50 | Target p95 |
|---|---|---|
| SQL / Data API: indexed read (primary key, indexed filter) | ≤ 2 ms | ≤ 5 ms |
| Vector: get by id, search by a given vector (1M, quantized) | ≤ 3 ms | ≤ 6 ms |
| Documents: keyword search | ≤ 4 ms | ≤ 8 ms |
| Graph: 1–2 hop reads | ≤ 4 ms | ≤ 8 ms |
| Documents: hybrid search, repeated question (cache hit) | ≤ 2 ms | ≤ 4 ms |
| Documents: hybrid search, new question, current model (bge-small) | ≤ 12 ms | ≤ 20 ms |
| Documents: hybrid search, new question, opt-in fast model | ≤ 5 ms | ≤ 10 ms |
| Documents: hybrid + graph, new question, current model | ≤ 15 ms | ≤ 25 ms |
| 8 clients at once, hybrid search | p95 ≤ 30 ms (no queue collapse) | |

### 2.2 What's outside our control

- **Docker Desktop on Windows adds about 40 ms** to every request made from the host, through port forwarding. In
  production (Linux, or clients on the same Docker network) this disappears. Our numbers are always measured inside
  the network.
- **A transformer embedding costs ~10–20 ms per new question on a laptop CPU.** Below that needs a cache, a static
  embedding model (some quality loss) or a GPU / embeddings server (`SWSDB_EMBEDDINGS=openai` already supports any
  compatible endpoint).
- **Cold reads are slower than warm ones.** Everyone's millisecond numbers are warm-cache numbers: Turbopuffer, for
  example, reports a p50 of 14 ms for cached queries and 874 ms for the first, cold query at 1M documents. We report
  both, and warm caches after a restart.

---

## 3. The plan

Five phases, in order of payoff. Each phase ends with the same scale benchmark run before and after, and nothing
merges if retrieval quality or any test gets worse.

### Phase 0: measure at real scale (prerequisite)

Nothing gets optimized blind. This phase builds the measuring tools.

#### 0.1 Scale datasets

A generator (`benchmarks/scale/generate.py`) that creates a dedicated project with:

| Engine | Data | Fits this machine because |
|---|---|---|
| PostgreSQL | 1M-row `events` table plus related tables (customers, products, orders), realistic types, indexes and foreign keys | Postgres keeps data on disk; ~1 GB |
| Qdrant | 1M points, 384-dimension dense vectors, payloads with keyword/number/bool fields | int8 quantization in RAM (~400 MB), originals on disk |
| Qdrant (documents) | 100k–1M passages through the real ingestion path, if time allows (embedding 1M passages on CPU takes hours; we can embed with the fast model, or pre-compute vectors) | |
| FalkorDB | ~1M edges: people, companies, projects and a power-law degree distribution, so some nodes are hubs | FalkorDB is in memory; ~1M edges is a few hundred MB |

The generator is deterministic (seeded) so every run measures the same data. Engines are loaded one at a time to stay
inside the Docker VM's memory.

#### 0.2 A timing breakdown on every response

Add a `Server-Timing` HTTP header (a web standard that browsers' developer tools display) listing each stage:
`auth`, `catalog`, `embed`, `qdrant`, `postgres`, `graph`, `encode`, `total`. Implementation: a small per-request
timer in a context variable, filled by the stores (`stores.py`, `lite.py`, `documents.py`, `embed.py`) and written by a
middleware in `app.py`. It costs microseconds and is on by default (`SWSDB_TIMING=false` turns it off).

This tells us, for any slow request, exactly which stage took the time.

#### 0.3 The benchmark runner

`benchmarks/scale/latency.py`: runs inside the Docker network (a client container on the same network as swsdb, so
port forwarding doesn't count) and reports p50, p95 and p99 for every endpoint in section 2.1:

- one client (pure latency) and 8 clients (concurrency);
- warm (after warm-up) and cold (right after a restart);
- the `Server-Timing` breakdown averaged per stage.

Results go to `benchmarks/results/` as JSON and a Markdown table, like the existing benchmarks.

#### 0.4 A latency check in CI

A small version of the benchmark (10k rows / points / edges, so it runs in a minute) in the CI image job, failing if
any endpoint's p50 or p95 gets more than 20% worse than the recorded baseline. This keeps later changes from undoing
the gains.

**Done when:** the scale dataset loads, the runner produces the full table, `Server-Timing` shows every stage, and the
CI check runs. We then have the real "before" numbers at 1M scale.

---

### Phase 1: cut the Python overhead (biggest win, lowest risk)

#### 1.1 Cache the catalog in memory

- **What:** `Documents.collection()`, `is_document_collection()`, tenant roles (`SqlStore.tenant_role`, already partly
  cached), collection existence (`VectorStore` / `GraphStore.exists`) and `inspect` results kept in an in-process
  cache.
- **Correctness:** every write that changes the catalog (create or drop a collection, settings changes) clears the
  entry locally and sends a cluster event, so other swsdb servers clear theirs. `cluster.py` already delivers events
  like `"auth"` this way; we add `"catalog"`. A short TTL (for example 30 s) is a safety net.
- **Where:** `documents.py`, `api_v1.py` (`documents_guard`, `raw_collection`), `stores.py`, `cluster.py`.
- **Saves:** one Postgres round trip on almost every request (~1–2 ms).

#### 1.2 One round trip per SQL call

- **What:** today `SqlStore.query` sends `BEGIN`, `SET TRANSACTION READ ONLY`, `set_config('statement_timeout', …)`,
  then the statement, then `COMMIT`: 4–5 round trips. Instead:
  - use psycopg's pipeline mode to send them together (one network round trip), or fold the settings into the
    transaction start (`BEGIN READ ONLY` plus `SET LOCAL statement_timeout` in the same batch);
  - turn on automatic prepared statements (`prepare_threshold`), so repeated queries skip parsing and planning.
- **Where:** `stores.py` (`SqlStore.query`, `query_as_user`), `dataapi.py`.
- **Care:** the sandbox guarantees (one statement, read-only, timeout, the tenant's role) must hold exactly as before.
  The security test module (`test_security`) proves it.
- **Saves:** ~2–3 ms per SQL and Data API call.

#### 1.3 Drop the stale-version Postgres check from search

- **What:** `Documents.current()` queries Postgres after every search to drop passages of old document versions.
  Passages in Qdrant already carry a `live` flag that the sync worker flips when a version goes live. Filter on
  `live = true` inside the Qdrant query instead, and keep a cheap in-memory list of documents being deleted (a few
  ids, refreshed by events) as a safety check.
- **Where:** `documents.py` (`query`, `current`, `_expand`), `sync.py` (to make sure `live` is set and cleared in the
  same step as `live_version`).
- **Care:** the existing consistency guarantee ("never return a deleted document") is tested by the chaos and
  consistency tests (`test_consistency`), which must stay green.
- **Saves:** one or two Postgres round trips per search (~1–2 ms).

#### 1.4 Run independent steps in parallel

- Dense and sparse encoding of the question at the same time (the sparse one is cheap, but runs while the dense one
  waits).
- Graph expansion: the 2–3 Cypher queries (adjacent passages, shared entities, related entities) folded into one query
  with `UNION`, or run concurrently.
- The permission check of graph candidates (`vector.scroll`) and the version check started as soon as the candidates
  are known.
- **Where:** `documents.py`.
- **Saves:** a few milliseconds on graph searches.

#### 1.5 Talk to Qdrant over gRPC

- **What:** use Qdrant's gRPC port (6334) with a persistent HTTP/2 connection for the hot calls (query, get, scroll)
  instead of REST + JSON. gRPC uses compact binary messages and keeps one connection open.
- Also: request only the payload fields a response uses (`with_payload: {include: [...]}`), never the vectors unless
  asked.
- **Where:** `stores.py` (`VectorStore`): a gRPC client next to the HTTP one; the `/qdrant` gate stays REST (it's
  Qdrant's REST API by definition).
- **Care:** the gRPC client adds a dependency (`grpcio` via `qdrant-client`, or generated stubs); measure that it's
  actually faster on our payloads before switching.
- **Saves:** ~1–2 ms on vector calls, more on large results.

#### 1.6 Faster JSON

- **What:** encode hot responses with `orjson` (5–6x faster than the standard library), and skip Pydantic validation
  on responses we build ourselves.
- **Where:** `app.py` (default response class), hot endpoints in `api_v1.py`.
- **Saves:** small per request (~0.2–1 ms), more on big results (search hits with text, SQL results).

#### 1.7 Server settings

- **uvloop and httptools** for the event loop and HTTP parsing (faster than the defaults); check they're active.
- **Several worker processes**, so more than one CPU core serves requests: Python runs one request at a time per
  process. Today there is one. Workers share nothing in memory, so the caches in 1.1 and phase 2 become per worker,
  cleared by the same cluster events. The embedding model is loaded once per worker (memory cost: ~100 MB each), or
  served by one shared embedding worker (see 3.4).
- **Leaner middleware:** the request log, metrics and restore checks run on every request; make them cheap (no
  allocations on the hot path, batched writes).
- **Where:** `__main__.py`, `app.py`, `observe.py`.

**Phase 1 expected result:** SQL by key ~6.5 → ~2 ms, vector reads ~9 → 2–3 ms, graph reads ~7 → 2–3 ms, keyword
search ~11 → ~4 ms, hybrid search ~30 → ~22 ms (embedding still dominates).

---

### Phase 2: caches

#### 2.1 Question-embedding cache

- **What:** an in-memory LRU (least recently used) cache from the normalized question text (and the model name) to its
  dense and sparse vectors. The same question is never embedded twice.
- **Size:** 10,000 questions × 384 floats ≈ 15 MB. Configurable (`SWSDB_EMBED_CACHE`).
- **Correctness:** a question's vector never changes for a given model, so there's nothing to invalidate.
- **Saves:** the whole ~20 ms embedding on every repeated question.

#### 2.2 Result cache

- **What:** the final search result cached, keyed by everything that decides it: tenant, collection, question,
  mode, k, filters, graph settings, re-ranking, **the caller's permissions (principals)** and **the collection's
  version**.
- **Correct by construction:** every write to a collection (a document added, changed or deleted; a point upserted)
  bumps the collection's version number, in Postgres and in a cluster event. The old cache entries simply stop being
  looked up. A result is never served after its data changed.
- **Where:** a new module (`cache.py`), used by `documents.query`, raw vector search and the read endpoints in
  `api_v1.py`. Versions bumped in `documents.py` and `sync.py`.
- **Size and limits:** bounded by memory (configurable), with a TTL as a safety net. Per worker, or optionally in
  FalkorDB's Redis (which is already running) so all workers and servers share it.
- **Saves:** a repeated search becomes a memory lookup: ~1 ms.

#### 2.3 Semantic cache (opt-in, off by default)

- **What:** questions that mean the same thing ("refund time?" and "how long do refunds take") share a cached result,
  matched by embedding similarity above a threshold (typically 0.85–0.95).
- **Why opt-in:** "similar" isn't always "same": two questions can be close in meaning but need different answers.
  Reported gains are large (one production Qdrant deployment measured a 75% hit rate and a 316x speed-up), but the
  risk of a wrong answer means it's the user's choice (`SWSDB_SEMANTIC_CACHE=0.92`, for example).
- **Monitoring:** hit rate, wrong-hit reports and the latency of hits and misses in the metrics.

#### 2.4 Hot SQL results

- **What:** results of read-only saved queries (the Reports page) cached for a few seconds, and invalidated when the
  tables they read change (via a statement-level trigger or a version bump on writes through the Data API).
- **Why limited:** caching arbitrary SQL is risky (functions like `now()`, data changed outside swsdb). Keep it to
  saved, read-only queries with a short TTL.

---

### Phase 3: tune the engines at 1M scale

#### 3.1 Qdrant

- **int8 scalar quantization** held in RAM (`always_ram: true`), original float vectors on disk, **rescoring** on
  (the top candidates re-ranked with the original vectors). Memory: 4x smaller; speed: faster; recall: kept within
  ~1% by rescoring. Qdrant's own numbers: quantized search at 1.49 ms vs 2.25 ms unquantized, with recall 0.99.
  - Binary quantization (32x smaller, fastest) for large models only (it works best on 1,000+ dimensions; ours are
    384), measured before use.
- **HNSW `ef` tuned** per collection using our own Index quality tab (recall@k against an exact scan): lowest `ef`
  that keeps recall ≥ 0.98.
- **Payload indexes** on every field we filter by in documents collections (`readers`, `live`, `document_id`,
  metadata fields), so filtered searches don't scan.
- **Segment settings:** a sensible default segment count for the core count, so searches run segments in parallel.
- **Where:** `stores.py` (`create_documents`, `create`), plus a migration that updates existing collections'
  settings in place.

#### 3.2 PostgreSQL

- **Memory settings sized to the machine** at bootstrap: `shared_buffers`, `effective_cache_size`, `work_mem`.
- **`jit = off`**: Postgres's just-in-time compiler adds milliseconds to short queries and only pays off for long
  analytical ones.
- **SSD costs:** `random_page_cost = 1.1`, so the planner uses indexes when it should.
- **Warm after restart:** `pg_prewarm` for swsdb's catalog and the hot tables, so the first queries after a restart
  aren't cold.
- **Index advice:** the Advisors page already flags foreign keys without an index and large sequential scans; make
  its suggestions one click.
- **Where:** `bootstrap.py` (generated `postgresql.conf`), `advisors.py`.

#### 3.3 FalkorDB

- **Indexes on every lookup key** (`Chunk.id`, `Entity.key`, `Document.id`): check they exist on every graph,
  including user graphs (suggest them in the graph browser).
- **One query per graph expansion**, with bounded fan-out (a `LIMIT` per hop) so hub entities can't explode a query.
- **Thread and cache settings** (`THREAD_COUNT`, `CACHE_SIZE`) set for the machine.
- **Where:** `stores.py` (`GraphStore`), `documents.py` (`_expand`), `bootstrap.py`.

#### 3.4 The embedding model

- **Micro-batching:** a single embedding queue. Concurrent questions that arrive within a few milliseconds of each
  other are embedded together in one model call (a batch of 8 costs little more than a batch of 1). This fixes the
  ~200 ms queue with 8 clients.
- **Thread tuning:** set ONNX Runtime's thread count for the machine (and to leave cores for the engines), and measure
  the graph optimization levels.
- **int8 version of bge-small:** measure on this CPU. It's usually ~2x faster, but one study found int8 *slower* on
  some Intel CPUs without the right instructions, so it's only switched on where it's measured to help.
- **Opt-in fast mode:** a static embedding model (Model2Vec, for example a distilled version of our model): encoding is
  a table lookup and an average, under 1 ms per question, at a reported ~3–5% retrieval quality cost. Offered as
  `SWSDB_EMBEDDINGS=fast` only after it passes our own quality test set (`benchmarks/datasets/kestrel.json`) within an
  agreed floor.
- **Where:** `embed.py`, `settings.py`, `documents.py`.

---

### Phase 4: Rust, only where the numbers say so

After phases 1–3, about 1 ms of Python overhead per request should remain. Rust can bring that to ~0.2 ms, and, more
importantly, serve many more requests per second per core. But a second language is a real, permanent maintenance cost,
so we decide with phase 3's numbers in hand.

#### Option A: a Rust fast path inside the container (axum + tokio)

- **What it serves:** the hottest read-only requests:
  - vector get and search by a given vector or point id (Qdrant over gRPC);
  - SQL primary-key and indexed reads through prepared statements (`tokio-postgres`);
  - document search when the question's vector is cached or supplied;
  - graph reads (1–2 hops) on FalkorDB (Redis protocol).
- **Everything else** (writes, ingestion, admin, the console, auth management) stays in Python. The Rust process is
  the front door on port 6464 and passes unknown routes to Python over a local socket.
- **Shared rules:** auth (key hashes from Postgres), tenancy, roles and document readers must behave exactly as in
  Python. The same API tests run against both, so a difference fails CI.
- **Gains:** ~0.2 ms per request instead of ~1 ms; throughput no longer limited by one request per Python process.
- **Cost:** a second codebase, a Rust toolchain in the build, and every rule implemented twice.

#### Option B: Rust extensions inside Python (PyO3)

- For CPU hot spots inside requests: BM25 tokenizing and sparse encoding, result fusion, graph candidate scoring.
- Small, contained and safe (the Python fallback stays), with modest gains (tenths of a millisecond).

#### Option C: not worth it

- Rewriting the embedding in Rust: it's the same ONNX Runtime underneath, so the speed is the same.
- Rewriting the engines: Qdrant is Rust and FalkorDB is C already.

**Recommendation:** decide on option A after phase 3, from the scale benchmark's remaining Python overhead. Published
Python-vs-Rust web framework comparisons range from 6% to 30x depending on the test, so we decide on our own numbers.

---

## 4. How we know each step worked

- **Before and after, every phase:** the same scale benchmark (phase 0), p50 / p95 / p99, one client and 8 clients,
  warm and cold, with the `Server-Timing` breakdown. The tables go into `benchmarks/results/` and `docs/benchmarks.md`.
- **Quality never drops:** the retrieval quality test (`test_quality`, CI floors on recall, MRR and nDCG) must pass.
  Quantization, `ef` tuning and the fast model are accepted only within an agreed recall floor (proposed: recall@5
  ≥ 0.98 of the unoptimized value).
- **Correctness never drops:** every change keeps the full container test suite green: security, consistency and
  chaos tests, snapshots, cluster and lite profile.
- **No regressions later:** the CI latency check (0.4) guards the gains.

## 5. Risks

| Risk | Mitigation |
|---|---|
| A cache serves stale or someone else's data | Keys include permissions and the collection version; versions bump on every write; tests for "write then read" and "two users, same question" |
| Several workers multiply memory (the embedding model in each) | A shared embedding worker or a configurable worker count; measure memory per worker |
| Quantization or a lower `ef` loses recall | Accept only within the recall floor, measured by the quality tests and the Index quality tab |
| int8 model slower on some CPUs | Measure on the target CPU, switch on only where faster |
| gRPC client adds complexity | Measure first; keep REST as fallback |
| A Rust fast path diverges from Python's rules | The same API tests against both; decide only with numbers |
| Laptop memory limits the scale tests | Load engines one at a time; run bigger tests on another machine later |

## 6. Decisions needed

1. **Fast embedding mode:** offer it as an opt-in with a small quality cost, or stay quality-first only?
2. **Semantic cache:** opt-in only (recommended), or not at all?
3. **Rust:** decide after phase 3 on the numbers (recommended), or build the fast path regardless?
4. **Scale:** 1M rows, 1M quantized vectors and ~1M edges fit this laptop's Docker VM (7.4 GB) one engine at a time.
   Run bigger tests on another machine later?

## 7. Order of work

| Step | Phase | Size | Depends on |
|---|---|---|---|
| Scale dataset generator | 0.1 | medium | – |
| `Server-Timing` breakdown | 0.2 | small | – |
| Benchmark runner (inside the network) | 0.3 | medium | 0.1 |
| CI latency check | 0.4 | small | 0.3 |
| Catalog cache + cluster event | 1.1 | small | 0.2 |
| One round trip per SQL call | 1.2 | medium | 0.2 |
| Search without the version round trip | 1.3 | medium | 0.2 |
| Parallel steps, one graph query | 1.4 | small | 0.2 |
| Qdrant over gRPC | 1.5 | medium | 0.3 (measure first) |
| orjson, server settings, workers | 1.6–1.7 | small | 0.3 |
| Embedding cache | 2.1 | small | 1.x |
| Result cache with versions | 2.2 | medium | 1.1 |
| Semantic cache (opt-in) | 2.3 | medium | 2.1, decision 2 |
| Hot SQL results | 2.4 | small | 2.2 |
| Qdrant quantization and tuning | 3.1 | medium | 0.1 |
| Postgres settings | 3.2 | small | 0.1 |
| FalkorDB indexes and bounded expansion | 3.3 | small | 1.4 |
| Embedding micro-batching, threads, int8 | 3.4 | medium | 0.3 |
| Fast embedding mode | 3.4 | medium | decision 1 |
| Rust decision, then option A or B | 4 | large | phase 3 numbers, decision 3 |

## 8. Sources

- Qdrant: quantization (benchmark of binary quantization with rescoring): https://qdrant.tech/quantization/
- Qdrant: vector search resource optimization guide: https://qdrant.tech/articles/vector-search-resource-optimization/index.md
- Turbopuffer architecture (cold vs cached latency at 1M documents): https://turbopuffer.com/docs/architecture
- Model2Vec (static embeddings): https://pypi.org/project/model2vec
- Static vs BGE embeddings, speed and quality (CodeGraph PR): https://github.com/codegraph-ai/CodeGraph/pull/7
- Anyscale: RAG in production (embedding and response caches): https://docs.anyscale.com/rag/production-scalability
- Semantic caching on a production Qdrant deployment (arXiv): https://arxiv.org/html/2603.02206v2
- Redis: RAG at scale (semantic cache thresholds): https://redis.io/blog/rag-at-scale/
- Psycopg 3: pipeline mode: https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html
- Psycopg 3 pipeline mode article: https://psycopg.org/articles/2024/05/08/psycopg3-pipeline-mode/
- Qdrant REST vs gRPC guide: https://computingforgeeks.com/qdrant-rest-grpc-api-guide/
- FalkorDB benchmarks: https://benchmark.falkordb.com/
- AIMultiple: graph database benchmark: https://aimultiple.com/graph-databases
- FastAPI vs axum (independent comparison): https://github.com/zachcoleman/fastapi-vs-axum
- py2axum (FastAPI compiled to axum, latency figures): https://pypi.org/project/py2axum/
- FastrAPI (PyO3 + axum, latency figures): https://pypi.org/project/fastrapi/
- INT8 portability study (int8 can be slower on some CPUs, arXiv): https://arxiv.org/pdf/2609.16085
- IBM ContextForge: adopting orjson: https://ibm.github.io/mcp-context-forge/latest/architecture/adr/017-adopt-orjson-json-serialization/
- msgspec benchmarks: https://jcristharif.com/msgspec/benchmarks.html
