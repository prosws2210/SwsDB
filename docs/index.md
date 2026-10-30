# swsdb

**One database for AI applications.** swsdb runs PostgreSQL, Qdrant and FalkorDB in one container. It keeps them consistent behind one API, and gives you permission-aware hybrid and graph retrieval with cited answers.

```bash
docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data ghcr.io/prosws2210/swsdb:latest
pip install swsdb
swsdb demo        # loads a sample handbook and answers questions with citations
```

Then open <http://localhost:6464/console>.

## What it does

<div class="grid cards" markdown>

- **Documents in, cited answers out.** Upload PDFs, Markdown, HTML or text. swsdb chunks them by structure, embeds them (dense and BM25, locally and offline by default) and links the entities they mention in a graph. `answer` replies with `[n]` citations to the passages it used.
- **Three stores, one truth.** Postgres holds the catalog. Qdrant and the graph are derived from it through an outbox, with retries and reconciliation, so they converge after any crash. This was proven by 100 chaos rounds that killed every engine mid-write.
- **Permissions in every query.** Each document lists who may read it. Search, graph expansion, answers and MCP tools all apply the same filter inside the engine, so denied text never leaves the server. Tenants are isolated down to a Postgres role each.
- **Built for agents.** An MCP server (`/mcp`, read-only; `/mcp/rw`, read-write) gives assistants search, answers, documents and long-term memory, with the key's permissions.
- **Measured, not guessed.** Golden question sets, recall/MRR/nDCG per configuration, a "why isn't this searchable?" diagnosis, Prometheus metrics and a retrieval-quality gate in CI.
- **One backup.** `swsdb snapshot create` copies every engine at one consistent point. A snapshot restores on another machine, and on newer versions of swsdb.

</div>

## Where to go next

- [Quickstart](quickstart.md): `docker run` to a cited answer in 10 minutes.
- [RAG app](quickstarts/rag-app.md), [GraphRAG](quickstarts/graphrag.md) and [agent memory](quickstarts/agent-memory.md): three small projects, each with code in `examples/`.
- [Security and tenants](guides/security.md): what a key can and can't reach.
- [Benchmarks](benchmarks.md): ingest throughput, latency, memory and retrieval quality.
