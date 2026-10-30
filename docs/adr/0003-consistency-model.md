# ADR 0003: Consistency model: catalog first, outbox, idempotent sync, reconciliation

Status: accepted · Date: 2026-10-30

## Context

A document lives in three engines: Postgres (catalog, chunks, extracted entities), Qdrant (vectors) and FalkorDB
(graph). There is no transaction across them. Any engine can be down, slow or killed in the middle of a write, and the
swsdb server itself can crash. The failure customers notice is drift: a deleted document still answering questions,
half a new version in results, vectors with no document, a graph out of step with the text.

## Decision

**1. Postgres is the source of truth; the other stores are derived.** Everything needed to rebuild vectors and graph is
in the catalog: chunk text, extracted entities and relations, the uploaded file. Re-extraction never needs the LLM again.

**2. Changes are accepted in one Postgres transaction with an outbox row.** An upload writes the document row (new
version, `queued`), the file and an `index` outbox row; a delete marks the row `deleting` and writes a `delete` row.
Once the API answers 202, the change can't be lost. Uploads and deletes need only Postgres: with Qdrant or FalkorDB
down they are accepted and applied on recovery.

**3. Workers apply outbox rows with idempotent steps.** Rows are claimed with `FOR UPDATE SKIP LOCKED`, a lease
(renewed by a heartbeat) and per-document ordering (a row waits for earlier rows of the same document). Indexing:
chunks in the catalog (parsed once) → vectors (skipped if Qdrant already holds exactly them) → entity extraction (only
chunks not yet extracted) → graph rebuilt from the catalog → switch on. Every id is deterministic, so a retry rewrites
the same objects instead of adding duplicates.

**4. Switch-on order makes versions atomic for readers.** (a) mark the new version's vectors live, (b) commit
`live_version = V` in the catalog, (c) delete other versions' vectors. Queries are checked against the catalog and only
keep hits from each document's `live_version`, so before (b) they return the old version, after it only the new one.

**5. Deletes hide immediately.** A document in `deleting` is dropped from every query result by the catalog check, even
while Qdrant still holds its vectors (for example while the graph engine is down and the delete is waiting).

**6. Errors are classified.** Transient (an engine unreachable, timeouts, Postgres connection loss, embedding or LLM
endpoint failures): retried indefinitely with backoff 2, 4, 8 ... capped at 120 s, and retried at once when the health
monitor sees the engine come back. Permanent (unreadable file, no text, wrong embedding model): the document is marked
`failed` with the reason. A document being retried shows `error: "retrying: ..."` and its attempt count.

**7. Reconciliation finds and repairs drift.** Per document collection it compares the catalog with a full scan of
Qdrant and the graph: missing or non-live vectors of the live version, stale versions, vectors or graph nodes of
unknown documents, graph chunk counts, changes with no outbox row, and a missing Qdrant collection. Repairs are outbox
rows (or direct deletes of orphans), so they go through the same idempotent path. It runs every
`SWSDB_RECONCILE_INTERVAL_S` (default 600) and on demand (`POST /v1/admin/reconcile`, `swsdb doctor [--repair]`),
dry-run by default.

## Guarantees

- An acknowledged upload or delete is never lost (it is in Postgres before the API answers).
- Queries never return a deleted document, nor a version other than the live one.
- After any engine or server failure, the stores converge to the catalog once the failure ends, without operator
  action; reconciliation repairs drift caused outside swsdb (manual changes, a lost volume).
- Not guaranteed: read-your-writes before a document is `ready` (it is eventually consistent, with a visible status).

## Evidence

`tests/image/test_consistency.py` with an independent checker that reads the three engines directly: uploads during a
Qdrant outage, a graph outage, deletes hidden before the stores catch up, drift in every store repaired (without new
LLM calls), a lost Qdrant collection rebuilt, a server crash mid-ingest, and a randomised chaos run (kills of Postgres,
Qdrant, FalkorDB and the server, and paused engines) checked after every round. Latest full run: 100 of 100
rounds consistent (11 min 54 s). An outage means killed and held down: a graceful stop is not one, because Qdrant
keeps serving existing keep-alive connections for ~20 s while it shuts down.

## Consequences

- Every query does one indexed catalog lookup for the documents in its hits (a few milliseconds).
- Repairs re-embed from catalog text: a lost Qdrant collection costs embedding time, never LLM calls.
- Raw vector and graph collections (no documents) have no catalog, so reconciliation covers document collections only.
