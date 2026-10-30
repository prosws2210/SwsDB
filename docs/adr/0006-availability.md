# ADR 0006: Availability and replication

Status: accepted · Date: 2026-10-31

## Context

swsdb is a single-node database: one server process owns the outbox, the engines' data volume and the uploaded files.
People will ask what happens when that machine or container fails, and whether swsdb can run with replicas.

## Decision

**v0.1 promises durability and fast recovery, not automatic failover.**

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

**Not in v0.1: several swsdb servers at once.** Two servers on the same engines would each be correct for the outbox
(claims use `FOR UPDATE SKIP LOCKED`), but three things are per-process today:

- uploaded originals on the local volume;
- the managed-key cache (a revocation on one server isn't seen by the other until it restarts);
- the snapshot pause (only the local workers stop).

Running active-active needs these moved out:

- originals into Postgres or S3;
- key changes broadcast with `LISTEN/NOTIFY`;
- the snapshot pause coordinated with an advisory lock that every worker checks.

That is the planned path. It is tracked in PLAN.md and doesn't change the data model.

## Consequences

- A single swsdb is a single point of failure for writes. Choose the backup interval by the data loss you can accept.
- With external replicated engines and a fast restart (Kubernetes reschedules the pod, the volume holds only
  originals and snapshots), swsdb is back in the time it takes to start a container, with nothing lost.
