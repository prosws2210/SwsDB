# Operations

## Health

| Endpoint | Answers |
|---|---|
| `GET /healthz` | 200 while the server process is alive (use for liveness) |
| `GET /readyz` | 200 when every engine is ready and documents can be accepted; 503 with the reason otherwise (`starting`, `degraded`, `restoring`) |
| `GET /v1/status` | engines with versions, latency and restarts seen; the sync backlog; the caller's role and tenant |

The image's Docker health check calls `/readyz` (over HTTPS when TLS is on). Inside the container,
`swsdb-admin status` prints the same.

## Metrics and traces

`GET /metrics` serves Prometheus metrics (with keys configured it needs the environment admin key, since the numbers
cover every tenant):

- engine state and probe latency;
- HTTP requests and latency by route;
- the outbox backlog and retries;
- ingest time per document and per stage (`swsdb_ingest_stage_seconds{stage="chunks|vectors|extract|graph|switch"}`);
- query time by mode;
- documents by status.

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to send OpenTelemetry traces over OTLP/HTTP.

## Consistency

```bash
swsdb doctor            # compare Qdrant and the graph with the catalog; changes nothing
swsdb doctor --repair   # queue the fixes through the same safe path as normal writes
```

The check also runs every `SWSDB_RECONCILE_INTERVAL_S` seconds (default 600). Its last result is in `/v1/status`
and on the console's Overview.

## Memory and CPU

An idle swsdb with all three engines uses about 600 MB, most of it the server with its embedding model. Embedding is the
main CPU cost of ingestion; searches take priority over it, so they stay fast while documents are being added. See
[Benchmarks](../benchmarks.md) for measured numbers, and use `SWSDB_ENGINES` to run fewer engines:

```bash
docker run -e SWSDB_ENGINES=sql,vector ...   # no graph
```

## Logs

Everything goes to the container's output, prefixed by component (`[swsdb]`, `[postgres]`, ...). Set
`SWSDB_LOG_LEVEL=debug` for per-document indexing timings.
