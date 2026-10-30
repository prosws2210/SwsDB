# Snapshots, restore and upgrades

## Snapshots

A snapshot is one tar file holding everything:

- every engine: a `pg_dump` of the catalog and every tenant's SQL, a Qdrant snapshot of each collection, and a dump of
  each graph;
- the uploaded originals.

All of it is taken at one consistent point.

```bash
swsdb snapshot create --name before-upgrade             # kept on the server's volume
swsdb snapshot create --output swsdb-backup.tar         # ...and downloaded
swsdb snapshot list
```

From inside the container (for cron), with the admin key from the container's environment:

```bash
docker exec swsdb swsdb-admin snapshot create --name nightly
```

Snapshots cover every tenant, so they need the admin key from the environment (`SWSDB_API_KEY`).

!!! info "How it stays consistent"
    Postgres is dumped from a single transaction snapshot. Qdrant and the graph are derived from Postgres by the
    outbox workers, so those pause (finishing what they hold), and API writes that go straight to Qdrant or the graph
    wait a few seconds. Document uploads continue throughout. Changes still waiting in the outbox are in the dump and
    are applied after a restore. A test takes a snapshot while documents are being written and checks that the
    restored stores agree exactly.

Snapshots live in `/swsdb/data/snapshots` on the same volume. **Copy them elsewhere** (`--output`, or
`GET /v1/admin/snapshots/{id}/download`): a backup on the disk it protects isn't a backup.

## Restore

```bash
swsdb snapshot restore swsdb-20261031-020000-nightly     # a snapshot on the server
swsdb snapshot restore ./swsdb-backup.tar                # a file: uploaded, then restored
```

Restoring replaces **all** data, in every tenant, with the snapshot's:

1. By default the current data is snapshotted first (`before-restore`), so a restore can be undone (`--no-backup`
   skips this).
2. While it runs, the API answers only `/v1/status` and the snapshot list (503 with `Retry-After` otherwise), and
   `/readyz` reports `restoring`.
3. The stores are replaced. The database is brought up to this version's schema, and a reconciliation checks that
   every store agrees with the catalog.

A snapshot restores on another machine (new engine passwords, same data and keys) and on newer swsdb versions. swsdb
refuses, before changing anything, a snapshot it can't open safely: one from a newer swsdb, a newer engine, or one
needing an engine this deployment doesn't run.

## Upgrades

Within a release series, upgrading is replacing the image:

```bash
swsdb snapshot create --name before-upgrade --output before-upgrade.tar
docker stop swsdb && docker rm swsdb
docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data ghcr.io/prosws2210/swsdb:0.2.0
```

On start, swsdb:

- **migrates its schema** automatically, and keeps a `pg_dump` of the database as it was in
  `/swsdb/data/backups/before-schema-N-<time>.dump`;
- **resumes** any document that was still queued before the upgrade;
- **refuses** a volume or database written by a *newer* swsdb, rather than corrupting it. `/readyz` says why.

CI runs the upgrade path on every change: the previous release writes documents, vectors, a graph and SQL, the new
image starts on the same volume, and everything must be there and searchable.

### Rolling back

Run the previous image and restore the snapshot you took before upgrading:

```bash
docker run -d --name swsdb -p 6464:6464 -v swsdb-data-rollback:/swsdb/data ghcr.io/prosws2210/swsdb:0.1.0
swsdb snapshot restore ./before-upgrade.tar --no-backup
```

### Engine major versions

A new PostgreSQL major version (or a Qdrant or FalkorDB release that changes its storage format) can't open the old
files directly. Such releases say so in their notes. Upgrade through a snapshot: take one with the old image, start the
new image on a fresh volume, and restore.
