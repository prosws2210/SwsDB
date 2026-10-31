# Deployment: one container, Compose, Kubernetes, managed engines

swsdb runs the same server in every layout; only where the engines run changes.

| Layout | Engines | Use it for |
|---|---|---|
| `docker run` (default) | inside the swsdb container, on one volume | laptops, single servers, the simplest backups |
| [Docker Compose](#docker-compose) | each in its own container | scaling or tuning engines separately, using their own tooling |
| [Kubernetes (Helm)](#kubernetes) | inside the pod, or external | clusters |
| [Managed engines](#managed-engines) | RDS / Cloud SQL, Qdrant Cloud, FalkorDB Cloud, Neo4j | not running databases yourself |
| [Lite profile](#lite-profile) | PostgreSQL only (pgvector, Apache AGE) | small machines, one engine to run and back up, Postgres you already have |
| [Several servers](#several-servers) | external, shared by two or more swsdb servers | more request capacity, no single server to lose |

One swsdb server is the simplest setup. Several can share one set of external engines; see
[several servers](#several-servers).

## Docker Compose

[`deploy/compose/docker-compose.yml`](https://github.com/prosws2210/SwsDB/tree/main/deploy/compose) runs PostgreSQL 17,
Qdrant 1.19 and FalkorDB 4.22 in their official images, and swsdb with only its API server:

```bash
cd deploy/compose
cp .env.example .env      # set SWSDB_API_KEY, POSTGRES_PASSWORD, QDRANT_API_KEY, FALKORDB_PASSWORD
docker compose up -d
curl localhost:6464/readyz
```

The swsdb container gets the engines' addresses and doesn't start its own:

```yaml
SWSDB_POSTGRES_URL: postgresql://swsdb:${POSTGRES_PASSWORD}@postgres:5432/swsdb
SWSDB_QDRANT_URL: http://qdrant:6333
SWSDB_QDRANT_API_KEY: ${QDRANT_API_KEY}
SWSDB_FALKORDB_URL: redis://:${FALKORDB_PASSWORD}@falkordb:6379
```

Everything works as in the single container: documents, tenants with their own SQL roles, snapshots (dumped from and
restored into the Postgres container), backups and MCP. `tests/image/test_external.py` runs this file in CI.

## Kubernetes

The Helm chart in [`deploy/helm/swsdb`](https://github.com/prosws2210/SwsDB/tree/main/deploy/helm/swsdb) runs swsdb as a
StatefulSet (one pod by default) with a persistent volume per pod:

```bash
kubectl create secret generic swsdb-keys --from-literal=api-key="$(openssl rand -base64 32)"
helm install swsdb deploy/helm/swsdb --set apiKey.existingSecret=swsdb-keys
kubectl port-forward svc/swsdb 6464:6464
```

| Value | Meaning |
|---|---|
| `apiKey.existingSecret` / `apiKey.value` | the admin key (required: the chart refuses to install without one) |
| `persistence.size`, `persistence.storageClass` | the data volume (default 20 Gi) |
| `externalEngines.existingSecret` | a Secret with `postgres-url`, `qdrant-api-key`, `falkordb-url` to use engines elsewhere |
| `externalEngines.qdrantUrl` | Qdrant's address |
| `externalEngines.neo4jUrl` | Neo4j as the graph engine (its password under `neo4j-password` in the Secret) |
| `profile` | `default` or `lite` (vectors and graphs inside Postgres) |
| `replicas` | servers; more than 1 needs external engines, and sets `SWSDB_FILES=postgres` ([several servers](#several-servers)) |
| `llm.url`, `llm.model`, `llm.existingSecret` | a chat model for entities and written answers |
| `backups.intervalHours`, `backups.s3.*` | scheduled backups to S3 |
| `ingress.*` | expose it (put TLS on the ingress) |
| `extraEnv` | any other `SWSDB_*` setting |

Probes use `/readyz` (start and readiness) and `/healthz` (liveness), so an engine restarting inside the pod takes it
out of the Service without restarting the pod. The chart is linted and rendered in CI. It hasn't been run against
every Kubernetes distribution, so check the manifests (`helm template`) for your cluster's policies.

## Managed engines

Point swsdb at any engine you already run; the others can stay inside its container.

**PostgreSQL** (RDS, Cloud SQL, Azure, Crunchy, Neon...). `SWSDB_POSTGRES_URL=postgresql://user:pass@host:5432/swsdb?sslmode=require`.
The user must:

- own the database;
- be allowed to `CREATE ROLE`: each tenant's SQL runs as its own role, and with an external Postgres these roles log in
  with generated passwords kept in swsdb's catalog;
- for snapshot restores, be allowed to drop and recreate the database.

Role names are global to a Postgres cluster, so give each swsdb its own cluster (or instance).

**Qdrant** (Qdrant Cloud or your own). `SWSDB_QDRANT_URL=https://xyz.cloud.qdrant.io:6333` and `SWSDB_QDRANT_API_KEY`.

**FalkorDB** (FalkorDB Cloud or your own). `SWSDB_FALKORDB_URL=rediss://:password@host:6379` (`rediss` for TLS). If you
run FalkorDB yourself, don't keep its image's default `TIMEOUT 1000` (one second per query): large documents need
longer to write.

All three accept `_FILE` variants for their secrets (`SWSDB_POSTGRES_URL_FILE`, ...). Snapshots of external engines
work the same way: `pg_dump`, Qdrant's snapshot API and FalkorDB's `DUMP`, all over the network.

### Neo4j

An existing Neo4j (5.19 or later, Community or Enterprise) can be the graph engine instead of FalkorDB:

```bash
SWSDB_NEO4J_URL=http://neo4j:7474  SWSDB_NEO4J_PASSWORD=...  # SWSDB_NEO4J_USER, SWSDB_NEO4J_DATABASE default to neo4j
```

swsdb talks to Neo4j's HTTP Query API. Every collection's graph lives in that one database: each node carries its
collection's own label, and swsdb's queries (document structure, entities, graph expansion, reconciliation) are
limited to it. Graph retrieval works as with FalkorDB and meets the same quality floors (`tests/image/test_neo4j.py`).

**Pass-through Cypher is off with Neo4j** (`POST /collections/{name}/cypher` answers 403). Keeping a query you write
inside one collection of a shared database would mean rewriting it, and swsdb doesn't build tenant isolation on query
parsing ([ADR 0007](../adr/0007-other-graph-engines.md)). Use FalkorDB or the lite profile when you need your own
Cypher. Snapshots copy each collection's subgraph as JSON and restore it into the same Neo4j.

## Lite profile

`SWSDB_PROFILE=lite` keeps vectors and graphs inside PostgreSQL: pgvector for dense vectors (HNSW), an inverted
index for BM25 keywords, and Apache AGE for the graphs. Postgres is the only engine process.

```bash
docker run -d -p 6464:6464 -v swsdb-data:/swsdb/data -e SWSDB_PROFILE=lite ghcr.io/prosws2210/swsdb
```

- **The same API and results.** Documents, hybrid search, graph expansion, pass-through Cypher (AGE's openCypher),
  SQL, tenants, snapshots and MCP work as in the default profile. CI runs the security suite and the retrieval
  quality gate on both profiles: the lite numbers are in [benchmarks](../benchmarks.md).
- **Smaller.** One engine process and one data directory; about 160 MB of memory when idle.
- **Tenants stay apart through Postgres permissions.** Each collection's graph is an AGE graph owned by its tenant's
  SQL role, and Cypher runs as that role, so a query can't open another tenant's graph (not even with SQL). The
  vector tables are in swsdb's own schema, which no tenant role can read.
- **One graph writer per collection at a time.** Writes to one collection's graph are serialized (an advisory lock);
  reads aren't. For large graphs FalkorDB stays faster at multi-hop traversal (ADR 0002).
- **Not supported in Cypher:** `RETURN *` (AGE needs the columns named), and FalkorDB-specific procedures
  (`CALL db.labels()` and the like).
- **With your own Postgres** (`SWSDB_POSTGRES_URL`), the server needs the `vector` and `age` extensions installed and
  `age` in `shared_preload_libraries` (Azure Database for PostgreSQL offers both; RDS and Cloud SQL offer pgvector
  but not AGE). swsdb creates the extensions if its user may.

A snapshot keeps the profile it was taken with: a lite snapshot doesn't restore into the default profile or the other
way around (move data between them through the API).

## Several servers

Two or more swsdb servers can serve the same data: point them at the same external engines (Postgres, Qdrant and
FalkorDB or Neo4j, or Postgres alone with the lite profile) and set `SWSDB_FILES=postgres` on each:

```bash
cd deploy/compose
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d   # servers on ports 6464 and 6465
```

Put a load balancer in front of them. What they share and how:

| What | How |
|---|---|
| Ingestion | Every server runs workers; outbox rows are claimed with `FOR UPDATE SKIP LOCKED`. A server that stops releases its claims; one that crashes loses them after the lease (60 s), and another server picks the work up. |
| Uploaded files | `SWSDB_FILES=postgres` keeps originals in Postgres until they are processed, so any server can process any upload. `/v1/status` warns when servers share a database without it. |
| API keys | Creating or revoking a key on one server is announced through Postgres (`LISTEN/NOTIFY`): the others refuse a revoked key within a second. |
| Snapshots and restores | Every change to the vector or graph store, on any server, holds a shared advisory lock. A snapshot takes it exclusively: changes in flight finish first and new ones wait, so the copy is consistent across servers. After a restore every server resets its caches. |
| Scheduled jobs | Backups and periodic reconciliation run on one server (the holder of a leader lock); another takes over within seconds when it stops. |

`/v1/status` lists the servers (`cluster.servers`) and says which one leads. Snapshots are written to the volume of
the server that takes them: with several servers, set `SWSDB_BACKUP_S3_*` so any server can fetch them.
`tests/image/test_cluster.py` runs two servers in CI. It covers the following:

- an upload to one server served by the other;
- one server stopped with uploads still queued, and the other finishing them;
- a key revoked on one server and refused by the other;
- a snapshot on one server while the other is ingesting, restored consistently.
