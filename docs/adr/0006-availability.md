# ADR 0006: Availability and replication

Status: accepted · Date: 2026-10-31 · Updated: 2026-10-31 (several servers implemented)

## Context

One swsdb server owns the outbox workers, its data volume and the files uploaded to it. People will ask what happens
when that machine or container fails, and whether swsdb can run with replicas.

## Decision

**Durability and fast recovery for one server; several servers on shared external engines for more.**

1. **Nothing accepted is lost on a crash.** A write is acknowledged only after it is committed in Postgres with its
   outbox row (ADR 0003). The bundled engines run with fsync on (Postgres WAL, Qdrant WAL, FalkorDB append-only file
   fsynced every second). Restart resumes the outbox. Chaos tests kill each engine and the server mid-write.
2. **Restart is the recovery path for process failures.** s6 restarts a crashed engine inside the container, and
   Docker or Kubernetes restart the container (`/healthz` liveness, `/readyz` readiness). Readiness drops while an
   engine is down, and writes that only need Postgres keep being accepted.
3. **Backups are the recovery path for machine loss.** Scheduled snapshots, uploaded to S3-compatible storage, restore
   on a new machine. The recovery point is the backup interval, and the recovery time is the restore time (minutes
   for gigabytes).
4. **Replication comes from the engines, when they are external.** With `SWSDB_POSTGRES_URL` on a replicated Postgres
   (RDS Multi-AZ, Cloud SQL HA, Patroni), Qdrant with replicated collections, and a replicated FalkorDB, each engine
   fails over by itself. swsdb reconnects (it retries every engine call, and reconciliation repairs drift).
5. **Several swsdb servers can share those engines** (`deploy/compose/docker-compose.ha.yml`). Outbox claims already
   used `FOR UPDATE SKIP LOCKED`. The three things that were per-process are now shared through Postgres
   (`server/swsdb_server/cluster.py`):
   - **Uploaded originals:** `SWSDB_FILES=postgres` keeps them in `swsdb.originals` until processed, in the same
     transaction as the catalog row and the outbox entry.
   - **Key changes:** announced with `LISTEN/NOTIFY`. A server that loses its event connection resets its caches
     when it reconnects, since it may have missed events.
   - **The snapshot pause:** an advisory lock in the `postgres` database (a restore drops the swsdb one). Every
     change to a derived store holds it shared: outbox items and direct vector or graph writes. A snapshot or
     restore takes it exclusively, waiting for changes in flight on every server and keeping new ones out.

   Servers also keep a heartbeat row (`swsdb.servers`, shown in `/v1/status`), so a starting server frees only the
   outbox claims of servers that are gone. A leader lock gives scheduled backups and periodic reconciliation to one
   server at a time.

## Consequences

- A single swsdb is a single point of failure for writes. Choose the backup interval by the data loss you can accept,
  or run two servers on external engines.
- With external replicated engines and several servers, losing a server costs only its in-flight requests. Its
  claimed outbox rows return to the others when their lease ends (60 s), or at once if it stopped cleanly.
- Snapshots are files on the volume of the server that took them; with several servers, S3 backups make them
  reachable from all of them.
- `tests/image/test_cluster.py` runs two servers: shared work, takeover, key revocation, and a snapshot during
  ingestion on the other server.
