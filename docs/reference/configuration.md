# Configuration

swsdb is configured with environment variables. Secrets marked † can also be given as a file with the `_FILE`
suffix (e.g. `SWSDB_API_KEY_FILE=/run/secrets/admin`).

## A config file instead

Every setting can also go in `/swsdb/config/swsdb.yaml` (or the file `SWSDB_CONFIG_FILE` names): lower case, without
the `SWSDB_` prefix, with sections joined by `_`. Environment variables win over the file, and an unknown key stops the
server with its name, so a typo doesn't go unnoticed.

```yaml
# docker run -v ./config:/swsdb/config:ro ...
engines: [sql, vector, graph]
rerank: true
llm:
  url: https://api.openai.com/v1
  model: gpt-4.1-mini          # the key stays in SWSDB_LLM_API_KEY_FILE
backup:
  interval_h: 24
  keep: 14
  s3: {url: "https://s3.eu-west-1.amazonaws.com", bucket: acme-swsdb, region: eu-west-1}
```

Keep secrets out of the file: use the `_FILE` variables for them.

## Server

| Variable | Default | Meaning |
|---|---|---|
| `SWSDB_API_KEY` † | unset | admin key for every tenant. Unset (and no managed keys): the API is open, for local use |
| `SWSDB_READ_ONLY_API_KEY` † | unset | read-only key for every tenant |
| `SWSDB_ENGINES` | all | which engines run: any of `sql`, `vector`, `graph` (or `postgres`, `qdrant`, `falkordb`) |
| `SWSDB_PROFILE` | `default` | `default`: Qdrant and FalkorDB beside Postgres. `lite`: vectors (pgvector) and graphs (Apache AGE) inside Postgres, which is then the only engine ([deployment](../guides/deployment.md#lite-profile)) |
| `SWSDB_FILES` | `disk` | where uploads wait to be processed: `disk` (this server's volume) or `postgres` (needed to [run several servers](../guides/deployment.md#several-servers)) |
| `SWSDB_SERVER_ID` | host name | this server's name among several (status, outbox claims) |
| `SWSDB_PORT` | `6464` | API port inside the container |
| `SWSDB_TLS_CERT_FILE`, `SWSDB_TLS_KEY_FILE` | unset | serve HTTPS with this certificate and key (both or neither) |
| `SWSDB_ALLOWED_HOSTS` | empty | comma-separated host names (and browser origins' hosts) accepted besides localhost, IP addresses and the container's hostname |
| `SWSDB_POSTGRES_URL` † | unset | use this PostgreSQL instead of the bundled one ([deployment](../guides/deployment.md)) |
| `SWSDB_QDRANT_URL`, `SWSDB_QDRANT_API_KEY` † | unset | use this Qdrant instead of the bundled one |
| `SWSDB_FALKORDB_URL` † | unset | use this FalkorDB instead of the bundled one (`redis://` or `rediss://`) |
| `SWSDB_NEO4J_URL` | unset | use this Neo4j (5.19 or later, HTTP port) as the graph engine instead ([deployment](../guides/deployment.md#neo4j)) |
| `SWSDB_NEO4J_USER`, `SWSDB_NEO4J_PASSWORD` †, `SWSDB_NEO4J_DATABASE` | `neo4j`, unset, `neo4j` | its login and database |
| `SWSDB_EXPOSE_NATIVE` | `false` | make the engines' own ports (5432, 6333, 6379) listen beyond localhost |
| `SWSDB_CONFIG_FILE` | `/swsdb/config/swsdb.yaml` | settings file (see above) |
| `SWSDB_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `SWSDB_ENGINE_START_TIMEOUT_S` | `120` | how long an engine may take to start before it's reported failed |

## Documents

| Variable | Default | Meaning |
|---|---|---|
| `SWSDB_EMBEDDINGS` | `local` | `local` (bundled ONNX model, offline) or `openai` (any OpenAI-compatible `/embeddings`) |
| `SWSDB_EMBEDDINGS_MODEL` | `BAAI/bge-small-en-v1.5` / `text-embedding-3-small` | dense model |
| `SWSDB_EMBEDDINGS_URL` | `https://api.openai.com/v1` | for `openai` |
| `SWSDB_EMBEDDINGS_API_KEY` † | unset | for `openai` |
| `SWSDB_LLM_URL` | `https://api.openai.com/v1` | OpenAI-compatible chat endpoint for entity extraction and answers |
| `SWSDB_LLM_MODEL` | unset | the chat model. Unset: entities are linked by name, and answers are extractive |
| `SWSDB_LLM_API_KEY` † | unset | key for the chat endpoint |
| `SWSDB_ANSWER_MODEL` | `SWSDB_LLM_MODEL` | a different model for cited answers |
| `SWSDB_RERANK` | `false` | re-rank search hits with the bundled cross-encoder unless a query says otherwise |
| `SWSDB_ENTITIES` | `auto` | entities for the graph: `auto` (the chat model if set, else names in the text), `llm`, `names` or `off` |
| `SWSDB_INGEST_WORKERS` | `2` | documents indexed at the same time |
| `SWSDB_MAX_UPLOAD_MB` | `100` | largest accepted upload |
| `SWSDB_BACKUP_INTERVAL_H` | `0` | take a backup every N hours (`0`: no schedule) |
| `SWSDB_BACKUP_KEEP` | `7` | backups kept on the volume and in the bucket |
| `SWSDB_BACKUP_S3_URL` | `https://s3.amazonaws.com` | S3 or S3-compatible endpoint |
| `SWSDB_BACKUP_S3_BUCKET` | unset | copy backups to this bucket (unset: they stay on the volume) |
| `SWSDB_BACKUP_S3_PREFIX` | `swsdb` | key prefix inside the bucket |
| `SWSDB_BACKUP_S3_REGION` | `us-east-1` | signing region |
| `SWSDB_BACKUP_S3_ACCESS_KEY` †, `SWSDB_BACKUP_S3_SECRET_KEY` † | unset | credentials |
| `SWSDB_RECONCILE_INTERVAL_S` | `600` | how often the stores are compared with the catalog (`0`: only on request) |

A collection remembers the embedding model it was created with. Changing `SWSDB_EMBEDDINGS_MODEL` later doesn't
silently mix vectors: documents in older collections fail with a message saying which model to set back.

## Client (SDK, CLI and `swsdb mcp`)

| Variable | Meaning |
|---|---|
| `SWSDB_URL` | server address (default `http://localhost:6464`) |
| `SWSDB_API_KEY` | key to send |
| `SWSDB_TENANT` | tenant to act for (default: the key's tenant, else `default`) |

## Observability

| Variable | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | send OpenTelemetry traces over OTLP/HTTP |
| `OTEL_SERVICE_NAME` | service name in traces (default `swsdb`) |
