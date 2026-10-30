# Deployment: one container, Compose, Kubernetes, managed engines

swsdb runs the same server in every layout; only where the engines run changes.

| Layout | Engines | Use it for |
|---|---|---|
| `docker run` (default) | inside the swsdb container, on one volume | laptops, single servers, the simplest backups |
| [Docker Compose](#docker-compose) | each in its own container | scaling or tuning engines separately, using their own tooling |
| [Kubernetes (Helm)](#kubernetes) | inside the pod, or external | clusters |
| [Managed engines](#managed-engines) | RDS / Cloud SQL, Qdrant Cloud, FalkorDB Cloud | not running databases yourself |

swsdb is a single-node database. Whatever the layout, run **one** swsdb server against one set of engines: it locks
its data volume, and the outbox workers assume they are the only writers.

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
one-pod StatefulSet with a persistent volume:

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
