# Benchmarks

Measured for v0.1.0 on a laptop: AMD Ryzen 7 7445HS (6 cores, 12 threads), Docker Desktop on Windows with 7.4 GB for
its VM. Every setting is the default: all three engines, the bundled `bge-small-en-v1.5` embedding model on CPU, and
no LLM. The raw results are in
[`benchmarks/results/`](https://github.com/prosws2210/SwsDB/tree/main/benchmarks/results), and
[how to run them](https://github.com/prosws2210/SwsDB/blob/main/benchmarks/README.md) is in the repository.

The workload is 1,000 generated handbook-like documents (2,981 passages) and 300 queries. The client runs inside
Docker's network, because Docker Desktop's port forwarding adds about 40 ms to every request measured from the host.

## Search latency

| Mode | p50 | p95 | p99 | server p50 | queries/s (1 client) | queries/s (8 clients) |
|---|---|---|---|---|---|---|
| keyword (BM25) | 12.8 ms | 17.2 ms | 22.7 ms | 6.9 ms | 71 | 104 |
| meaning (dense) | 28.8 ms | 40.2 ms | 48.8 ms | 20.6 ms | 33 | 41 |
| hybrid | 30.3 ms | 41.0 ms | 52.0 ms | 22.2 ms | 31 | 38 |
| hybrid + graph (1 hop) | 43.1 ms | 72.9 ms | 111.1 ms | 34.1 ms | 22 | 32 |

*Server p50* is swsdb's own `took_ms`; the rest is HTTP and the client. Dense and hybrid queries spend most of their
time embedding the question on the CPU, so 8 concurrent clients mostly queue for the model: p50 rises to 200 ms for
hybrid search, at 38 queries a second.

**While documents are being indexed**, searches share the CPU with document embedding. Searches jump the queue between
small document batches, but the model's inference still competes for the cores: hybrid p50 was 206 ms and p95 576 ms
during bulk ingestion. An embeddings endpoint (`SWSDB_EMBEDDINGS=openai` with any compatible server) takes that load
off the machine.

## Ingestion

| | |
|---|---|
| Throughput | **11.1 passages/s**, 3.7 documents/s (6 paragraphs each), 2 workers |
| Where the time goes | embedding: the vectors stage is most of each document's time (`swsdb_ingest_stage_seconds`) |
| Accepting uploads | 1,000 documents queued in 18 s; uploads return once durable, indexing follows |

The bundled model is chosen to work offline on any machine, not for speed. For large corpora use an embeddings
endpoint (a GPU server or a hosted API): indexing is then bounded by Qdrant and Postgres, not the CPU.

## Memory and disk

| | Idle (model loaded) | With 2,981 passages |
|---|---|---|
| swsdb server (with the embedding model) | 371 MB | 410 MB |
| PostgreSQL | 111 MB | 192 MB |
| Qdrant | 92 MB | 131 MB |
| FalkorDB | 31 MB | 40 MB |
| **Total** | **606 MB** | **773 MB** |

Before the first document or search, the embedding model isn't loaded yet and the server is much smaller.

**Per million passages:**

- **Memory.** Qdrant and FalkorDB keep their data in memory. PostgreSQL's memory is capped by its settings (the data
  stays on disk), and the server's by the model. At this small size, Qdrant grew by 12.2 GiB per million passages and
  FalkorDB by 2.7. That is an upper bound, because segment and index overheads dominate the first few thousand points.
  Qdrant's own sizing rule (vectors × dimensions × 4 bytes × 1.5) gives 2.3 GB for a million 384-dimensional dense
  vectors, plus the BM25 vectors.
- **Disk.** About 7.1 GB in Postgres (passages, catalog, outbox history), 8.2 GB in Qdrant, 0.2 GB of graph without
  entities, and 2.6 GB of uploaded originals (here, generated text).

## Retrieval quality

[`benchmarks/datasets/kestrel.json`](https://github.com/prosws2210/SwsDB/blob/main/benchmarks/datasets/kestrel.json) is
the handbook of a fictional company: 30 documents and 44 questions. 37 questions are answered by one document; 7 need
two documents linked by a person, project or vendor. The server runs as shipped, with no model: entities are the
names documents mention. So these are the numbers a new user gets, not an LLM's. CI runs this on every change and
fails below the floors. Ranges are over two runs.

**All 44 questions, top 5:**

| Configuration | hit | recall | MRR | nDCG | answer found |
|---|---|---|---|---|---|
| hybrid | 1.000 | 0.989 | 0.989–1.000 | 0.976–0.987 | 0.977 |
| dense | 1.000 | 0.989 | 0.966 | 0.961 | 0.977 |
| keyword | 1.000 | 0.989 | 0.989 | 0.976 | 0.977 |
| hybrid + graph | 1.000 | **1.000** | 0.977–1.000 | 0.975–0.990 | **1.000** |
| hybrid + re-rank | 1.000 | **1.000** | 1.000 | 0.989 | **1.000** |

**The 7 multi-hop questions** (with 7 questions, one near-tie in the fused ranking moves a number by 0.07):

| Configuration | passages | recall | answer found |
|---|---|---|---|
| hybrid, top 5 | 5 | 0.929 | 0.857 |
| hybrid + graph, top 5 | 8 | **1.000** | **1.000** |
| hybrid + re-rank, top 5 | 5 | **1.000** | **1.000** |
| hybrid, top 2 | 2 | 0.71–0.79 | 0.43–0.57 |
| hybrid top 1 + 1 graph passage | 2 | **0.79–0.86** | **0.714** |
| hybrid, top 4 | 4 | 0.929 | 0.857 |
| hybrid top 1 + up to 3 graph passages | 3.1 | **1.000** | **1.000** |

Re-ranking 20 candidates with the bundled cross-encoder also lifts recall to 1.000, without extra passages, at about
240 ms per question on this CPU (hybrid alone: 21 ms). That is why it is opt-in.

The graph adds passages, so the fair comparison is at the same number of passages. There, following the graph beats
taking more search hits: with two passages, the answer phrase is found for 71% of multi-hop questions instead of 43–57%,
in every run. On single-document questions it costs nothing. CI enforces both: graph recall above plain search at top
5, and a higher answer-found rate at two passages.

!!! note "What this dataset doesn't show"
    30 documents is small, and the handbook is written in a way that lets similarity search find most second hops on
    its own. Larger corpora with more look-alike documents make the graph matter more. A chat model extracts more,
    and more varied, entities than names alone, plus relations for 2-hop expansion. Measure on your own questions with
    [evaluation](guides/evaluation.md).

## Graph engine

[ADR 0002](adr/0002-graph-engine-falkordb.md) measured two-hop expansion on the same data: FalkorDB 1.7 ms, Apache AGE
124 ms.
