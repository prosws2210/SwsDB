# Benchmarks

Results are in [`results/`](results) and summarised on the docs site's [Benchmarks](../docs/benchmarks.md) page.

| Script | Measures |
|---|---|
| [`rag_bench.py`](rag_bench.py) | ingest throughput, query latency (p50/p95/p99, sequential and concurrent, and during ingestion), memory and disk per million chunks |
| [`../tests/image/test_quality.py`](../tests/image/test_quality.py) | retrieval quality on [`datasets/kestrel.json`](datasets/kestrel.json); runs in CI and fails below its floors |
| [`locustfile.py`](locustfile.py) | sustained mixed load (searches, answers, writes) with Locust |
| [`graph_engines.py`](graph_engines.py) | FalkorDB against Apache AGE for graph expansion (the input to ADR 0002) |

## Running the main benchmark

Run it from a helper container that shares the server's network, processes and volume. Measured from the host, Docker
Desktop's port forwarding adds about 40 ms to every request on Windows and macOS:

```bash
docker build -f docker/Dockerfile -t swsdb/swsdb:dev .
docker run -d --name swsdb-bench -v swsdb-bench:/swsdb/data swsdb/swsdb:dev
docker run --rm --network container:swsdb-bench --pid container:swsdb-bench -v swsdb-bench:/swsdb/data:ro \
  -v "$PWD/benchmarks:/bench" --entrypoint python swsdb/swsdb:dev /bench/rag_bench.py --local \
  --docs 1000 --out /bench/results/bench-$(date +%Y%m%d).json
```

The corpus is generated from a fixed seed (handbook-like paragraphs), so runs compare across versions and machines.
Everything uses the default local embedding model on CPU and no LLM.

## Retrieval quality

```bash
SWSDB_QUALITY_REPORT=quality.json uv run --project server pytest -m image tests/image/test_quality.py -s
```

The dataset is a fictional company's handbook: 30 documents and 44 questions, 7 of which need two documents linked by
a name. The server runs as shipped, with no model, so entities are the names the documents mention. The numbers are
what a new user gets, not what a particular LLM gets. When retrieval improves, raise `FLOORS` in the test to the new
numbers minus a small margin.
