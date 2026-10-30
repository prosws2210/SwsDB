# ADR 0005: Snapshots: pause the derived stores, copy each engine natively

Status: accepted · Date: 2026-10-31

## Context

One volume holds three engines. A backup made of three independent backups taken at different moments can disagree:
a document in the catalog whose vectors were copied a second earlier, before they existed. Restoring must also work on
another machine (new engine passwords) and on newer swsdb versions (newer schema, newer engines).

## Decision

A snapshot is one tar file with a manifest, a `pg_dump` (custom format), one native Qdrant snapshot per collection, a
FalkorDB `DUMP` per graph, and the uploaded files.

**Consistency comes from the consistency model** ([ADR 0003](0003-consistency-model.md)). Postgres is the source of
truth and a `pg_dump` is one MVCC snapshot. Qdrant and the graph change only through the outbox workers and a few direct
API writes. During a snapshot the workers pause (finishing the change each holds), and the direct writes get 503 with
a retry hint. The derived stores are then copied while nothing changes them. Document uploads keep working: they touch
only Postgres and the file store, and the dump either contains them, with their outbox rows, or not at all. A test
takes a snapshot under a steady stream of uploads and checks that the restored stores agree exactly
(reconciliation finds nothing).

**Restore** replaces everything: it recreates the database from the dump (roles first, since they live outside it),
uploads each Qdrant snapshot, restores each graph key, and swaps the files directory. Then it runs migrations, so older
snapshots open on newer versions, and a reconciliation. By default it snapshots the current data first. It refuses,
before changing anything, a snapshot from a newer swsdb, schema or engine, or one needing an engine that isn't running.

**Upgrades in place** keep a `pg_dump` from before the schema changes in `/swsdb/data/backups`, and a server refuses a
database migrated by a newer swsdb.

Considered and rejected:

- *Copying the volume's files.* Needs the engines stopped, and isn't portable across engine versions.
- *Rebuilding Qdrant and the graph from Postgres instead of copying them.* Possible for documents collections (it
  re-embeds everything), but raw vector collections and raw graphs exist only in their engine.

## Consequences

- A snapshot briefly delays writes that go straight to Qdrant or the graph (seconds per GB of vectors), not uploads or
  queries.
- Snapshots are stored on the data volume until downloaded. Copying them off the machine is the operator's job;
  scheduled uploads to S3-compatible storage are planned for after v0.1.
