"""Advisors: checks for security and performance problems, each with what to do about it (the console's Advisors page).

Every check reads only: the settings, swsdb's catalog, the outbox, and the tenant's own SQL schema (queried as the
tenant's sandbox role, so it sees what the tenant sees). A key from the environment also gets the server-wide checks.
"""

from __future__ import annotations

import datetime as dt
import logging

from .auth import Caller

log = logging.getLogger("swsdb.advisors")

STALE_KEY_DAYS = 30


def _s(n) -> str:
    """The plural ending for a count (or a list of things)."""
    return "" if (len(n) if isinstance(n, list) else n) == 1 else "s"


def finding(category: str, level: str, check: str, title: str, detail: str, fix: str,
            items: list | None = None) -> dict:
    return {"category": category, "level": level, "check": check, "title": title, "detail": detail, "fix": fix,
            "items": items or []}


async def run_advisors(app, tenant: str, caller: Caller) -> dict:
    st = app.state
    s = st.settings
    out: list[dict] = []
    ran: list[str] = []

    async def check(name: str, fn) -> None:
        ran.append(name)
        try:
            await fn()
        except Exception as e:  # one engine down shouldn't hide the other findings
            out.append(finding("performance", "info", name, f"Couldn't run the {name} check",
                               f"{type(e).__name__}: {e}"[:300], "Run the advisors again once every engine is ready."))

    # ---------------------------------------------------------------- security, server-wide
    if caller.is_global:
        async def open_api():
            if s.api_key is None and s.read_only_api_key is None and not await st.keys.any_active():
                out.append(finding("security", "error", "open_api", "The API accepts every request",
                                   "SWSDB_API_KEY isn't set and there are no managed keys, so anyone reaching port "
                                   f"{s.port} is an admin of every tenant.",
                                   "Set SWSDB_API_KEY (or SWSDB_API_KEY_FILE) before the port is reachable from other "
                                   "machines."))
        await check("open_api", open_api)

        async def native_ports():
            if s.expose_native:
                out.append(finding("security", "warning", "native_ports", "The engines' own ports are open",
                                   "SWSDB_EXPOSE_NATIVE is on: Postgres, Qdrant and FalkorDB listen beyond localhost, "
                                   "where their own passwords are the only protection and swsdb's tenants don't apply.",
                                   "Turn SWSDB_EXPOSE_NATIVE off unless you use the engines' own tools; don't publish "
                                   "those ports beyond the machines that need them."))
        await check("native_ports", native_ports)

        async def tls():
            if not s.tls and s.allowed_hosts_list:
                out.append(finding("security", "warning", "no_tls", "Plain HTTP on a named host",
                                   f"SWSDB_ALLOWED_HOSTS lists {', '.join(s.allowed_hosts_list)}: the API is meant to "
                                   "be reached by name, but swsdb serves plain HTTP: keys and data cross the network "
                                   "unencrypted unless a proxy in front adds TLS.",
                                   "Put HTTPS in front (a proxy or an ingress), or set SWSDB_TLS_CERT_FILE and "
                                   "SWSDB_TLS_KEY_FILE."))
        await check("no_tls", tls)

        async def backups():
            if s.backup_interval_h <= 0:
                out.append(finding("performance", "warning", "no_backups", "No scheduled backups",
                                   "Snapshots only happen when someone takes one, so losing this machine loses "
                                   "everything since the last manual snapshot.",
                                   "Set SWSDB_BACKUP_INTERVAL_H (for example 24) and SWSDB_BACKUP_S3_* to copy them "
                                   "off the machine."))
            elif not s.backup_s3_bucket:
                out.append(finding("performance", "info", "backups_local", "Backups stay on this machine",
                                   "Scheduled snapshots are kept on the data volume only.",
                                   "Set SWSDB_BACKUP_S3_URL and SWSDB_BACKUP_S3_BUCKET: a copy then survives it."))
        await check("backups", backups)

    # ---------------------------------------------------------------- security, per tenant
    if st.sql is not None:
        async def keys():
            rows = await st.keys.list(None if caller.is_global else tenant)
            now = dt.datetime.now(dt.UTC)
            active = [k for k in rows if not k.get("revoked_at")]
            stale = []
            for k in active:
                seen = k.get("last_used_at") or k.get("created_at")
                if seen and (now - dt.datetime.fromisoformat(seen)).days >= STALE_KEY_DAYS:
                    stale.append(f"{k['id']} ({k.get('name') or 'no name'}, {k['tenant']}, {k['role']})")
            if stale:
                out.append(finding("security", "warning", "stale_keys", f"{len(stale)} API key{_s(stale)} unused for "
                                   f"{STALE_KEY_DAYS}+ days", "Unused keys still work for whoever holds them.",
                                   "Revoke the ones you don't need (API keys page, or DELETE /v1/keys/{id}).", stale))
            admins = {}
            for k in active:
                if k["role"] == "admin":
                    admins.setdefault(k["tenant"], []).append(k["id"])
            many = [f"{t}: {len(ids)} admin keys" for t, ids in admins.items() if len(ids) > 3]
            if many:
                out.append(finding("security", "info", "many_admin_keys", "Many admin keys",
                                   "Every admin key can write and delete everything in its tenant.",
                                   "Give services that only search a read key, and revoke admin keys you don't need.",
                                   many))
        await check("keys", keys)

    docs = st.docs
    if docs is not None:
        async def public_docs():
            rows = await docs.rows("SELECT collection, count(*) AS n FROM swsdb.documents WHERE tenant = %s "
                                   "AND '*' = ANY(readers) GROUP BY collection ORDER BY n DESC", [tenant])
            if rows:
                out.append(finding("security", "info", "public_documents", "Documents everyone in the tenant can read",
                                   "These documents have readers [\"*\"], so every key of the tenant (and a read key "
                                   "without principals) finds them in search and answers.",
                                   "Upload documents with readers like [\"group:hr\"] when only some people may read "
                                   "them, and query with principals.",
                                   [f"{r['collection']}: {r['n']} documents" for r in rows]))
        await check("public_documents", public_docs)

        # ---------------------------------------------------------------- performance: documents and sync
        async def failed():
            rows = await docs.rows("SELECT collection, count(*) AS n, min(error) AS example FROM swsdb.documents "
                                   "WHERE tenant = %s AND status = 'failed' GROUP BY collection", [tenant])
            if rows:
                n = sum(r["n"] for r in rows)
                out.append(finding("performance", "error", "failed_documents", f"{n} document{_s(n)} failed to index",
                                   "They are not searchable. For example: " + (rows[0]["example"] or "")[:200],
                                   "Open each one in Collections to see why, fix the file or setting, and upload it "
                                   "again.", [f"{r['collection']}: {r['n']}" for r in rows]))
        await check("failed_documents", failed)

        async def backlog():
            stats = await docs.sync.stats()
            if stats["retrying"]:
                n = stats["retrying"]
                out.append(finding("performance", "warning", "sync_retrying", f"{n} change{_s(n)} being retried",
                                   "An engine refused or timed out on them; they are retried with backoff.",
                                   "Check the engines on the Overview page and the server log; the doctor shows what "
                                   "differs."))
            elif stats["pending"] > 200:
                out.append(finding("performance", "info", "sync_backlog", f"{stats['pending']} changes waiting",
                                   "Documents are accepted faster than they are indexed.",
                                   "Raise SWSDB_INGEST_WORKERS if the machine has spare CPU, or run several servers."))
            last = stats.get("last_reconcile") or {}
            if last.get("issues"):
                n = last["issues"]
                out.append(finding("performance", "warning", "drift", f"The last check found {n} issue{_s(n)}",
                                   "The vector store or the graph differed from the catalog; repairs were queued.",
                                   "Open the Doctor to see what differs; repeated drift points at an engine losing "
                                   "writes."))
        await check("sync", backlog)

    # ---------------------------------------------------------------- performance: the tenant's own tables
    if st.sql is not None:
        async def run(q: str) -> list:
            return (await st.sql.query(q, None, read_only=True, timeout_s=15, max_rows=200, tenant=tenant))["rows"]

        async def no_pk():
            rows = await run("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                             "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND NOT EXISTS ("
                             "SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p') ORDER BY 1")
            if rows:
                out.append(finding("performance", "warning", "no_primary_key",
                                   f"{len(rows)} table{_s(rows)} without a primary key",
                                   "Rows can't be told apart reliably, updates and deletes of one row are hard, "
                                   "and the table editor can't keep a stable order.",
                                   "Add one, e.g. ALTER TABLE t ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY "
                                   "PRIMARY KEY.", [r[0] for r in rows]))
        await check("no_primary_key", no_pk)

        async def fk_index():
            rows = await run(
                "SELECT c.conrelid::regclass::text, c.conname, "
                "(SELECT string_agg(a.attname, ', ') FROM unnest(c.conkey) k JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = k) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace WHERE c.contype = 'f' AND n.nspname = current_schema() "
                "AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid "
                "AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] @> c.conkey::int2[]) ORDER BY 1")
            if rows:
                out.append(finding("performance", "warning", "unindexed_foreign_keys",
                                   f"{len(rows)} foreign key{_s(rows)} without an index",
                                   "Joins on them, and deletes in the table they point to, scan the whole table.",
                                   "CREATE INDEX ON <table> (<columns>) for each.",
                                   [f"{r[0]} ({r[2]}) — {r[1]}" for r in rows]))
        await check("unindexed_foreign_keys", fk_index)

        async def seq_scans():
            rows = await run(
                "SELECT relname, n_live_tup, seq_scan, coalesce(idx_scan, 0) FROM pg_stat_user_tables "
                "WHERE schemaname = current_schema() AND n_live_tup >= 10000 AND seq_scan > 10 "
                "AND seq_scan > 10 * coalesce(idx_scan, 0) ORDER BY seq_scan DESC LIMIT 20")
            if rows:
                out.append(finding("performance", "info", "sequential_scans", "Large tables read without an index",
                                   "These tables have 10,000+ rows and are mostly read by scanning every row.",
                                   "Find the filters your queries use (Logs shows the slow routes) and index those "
                                   "columns.", [f"{r[0]}: {r[1]:,} rows, {r[2]:,} scans, {r[3]:,} index scans"
                                                for r in rows]))
        await check("sequential_scans", seq_scans)

    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: (order[f["level"]], f["category"], f["check"]))
    counts = {lvl: sum(f["level"] == lvl for f in out) for lvl in order}
    return {"tenant": tenant, "scope": "server and tenant" if caller.is_global else "tenant", "checks": ran,
            "counts": counts, "findings": out}
