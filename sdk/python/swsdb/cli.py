"""`swsdb` command line: talk to a swsdb server (SWSDB_URL, SWSDB_API_KEY, SWSDB_TENANT)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import SwsDB, SwsDBError


def _table(columns: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in columns]] + [["" if v is None else json.dumps(v) if isinstance(v, (dict, list))
                                            else str(v) for v in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    lines = [" | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in cells]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swsdb", description="Command line for a swsdb server.")
    p.add_argument("--url", help="server URL (default $SWSDB_URL or http://localhost:6464)")
    p.add_argument("--api-key", help="API key (default $SWSDB_API_KEY)")
    p.add_argument("--tenant", help="tenant (default $SWSDB_TENANT, else the key's tenant or 'default')")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    p.add_argument("--insecure", action="store_true", help="don't verify the server's TLS certificate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="engine states and versions")
    sub.add_parser("collections", help="list collections")
    cr = sub.add_parser("create", help="create a documents collection (chunked, embedded and graphed for you)")
    cr.add_argument("collection")
    cr.add_argument("--no-graph", action="store_true", help="vectors only, no knowledge graph")
    ing = sub.add_parser("ingest", help="add files or folders to a documents collection (created if needed)")
    ing.add_argument("collection")
    ing.add_argument("paths", nargs="+", help="files or folders (PDF, Markdown, HTML, text)")
    ing.add_argument("--no-wait", action="store_true", help="return once queued, don't wait until searchable")
    ing.add_argument("--readers", help="comma-separated principals who may find them, e.g. group:hr")
    se = sub.add_parser("search", help="search a documents collection")
    se.add_argument("collection")
    se.add_argument("query")
    se.add_argument("-k", type=int, default=5)
    se.add_argument("--mode", choices=["hybrid", "dense", "sparse"], default="hybrid")
    ask = sub.add_parser("ask", help="answer a question from a documents collection, with citations")
    ask.add_argument("collection")
    ask.add_argument("question")
    ask.add_argument("-k", type=int, default=5)
    wh = sub.add_parser("webhooks", help="be told when documents are ready, fail or are deleted")
    whs = wh.add_subparsers(dest="wh_cmd", required=True)
    whc = whs.add_parser("create", help="register a URL (prints its signing secret once)")
    whc.add_argument("url")
    whc.add_argument("--events", help="comma-separated: document.ready,document.failed,document.deleted")
    whc.add_argument("--collections", help="comma-separated (default: all)")
    whs.add_parser("list", help="webhooks of the tenant, with delivery counts")
    whd = whs.add_parser("deliveries", help="recent deliveries of one webhook")
    whd.add_argument("id")
    whp = whs.add_parser("ping", help="send a test event")
    whp.add_argument("id")
    whx = whs.add_parser("delete", help="remove a webhook")
    whx.add_argument("id")
    sub.add_parser("tenants", help="tenants and what they hold (a key from the server's environment)")
    ev = sub.add_parser("eval", help="measure retrieval on a golden set and compare configurations")
    ev.add_argument("collection")
    ev.add_argument("set", help="the golden set's name")
    ev.add_argument("--questions", help="JSON file [{query, relevant: [ids], answer}] to save as the set first")
    ev.add_argument("-k", type=int, default=5)
    ev.add_argument("--rerank", action="store_true", help="also compare re-ranked configurations")
    dm = sub.add_parser("demo", help="load a sample company handbook and ask it questions (a one-minute tour)")
    dm.add_argument("--collection", default="kestrel")
    m = sub.add_parser("mcp", help="run the MCP server for agents (stdio; needs pip install 'swsdb[mcp]')")
    m.add_argument("--read-write", action="store_true", help="also add/delete documents and remember/forget")
    m.add_argument("--http", type=int, metavar="PORT", help="serve over streamable HTTP on this port instead")
    m.add_argument("--host", default="127.0.0.1", help="with --http: the address to listen on")
    q = sub.add_parser("sql", help="run one SQL statement")
    q.add_argument("query")
    q.add_argument("--read-only", action="store_true")
    doc = sub.add_parser("doctor", help="check that the vector and graph stores agree with the catalog")
    doc.add_argument("--repair", action="store_true", help="queue the fixes for what is found")
    doc.add_argument("--all-tenants", action="store_true")
    c = sub.add_parser("cypher", help="run a Cypher query on a collection's graph")
    c.add_argument("collection")
    c.add_argument("query")
    c.add_argument("--read-only", action="store_true")
    sn = sub.add_parser("snapshot", help="consistent snapshots of every engine, and restoring them")
    sns = sn.add_subparsers(dest="snap_cmd", required=True)
    sc = sns.add_parser("create", help="take a snapshot (kept on the server; --output also downloads it)")
    sc.add_argument("--name", help="a short label, e.g. before-upgrade")
    sc.add_argument("--output", "-o", help="also download it to this file")
    sns.add_parser("list", help="snapshots kept on the server")
    sd = sns.add_parser("download", help="download a snapshot")
    sd.add_argument("id")
    sd.add_argument("--output", "-o", help="file to write (default <id>.tar)")
    sr = sns.add_parser("restore", help="replace all data with a snapshot (an id on the server, or a .tar file)")
    sr.add_argument("source")
    sr.add_argument("--no-backup", action="store_true", help="don't snapshot the current data first")
    sr.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    sx = sns.add_parser("delete", help="delete a snapshot on the server")
    sx.add_argument("id")
    bk = sub.add_parser("backups", help="scheduled backups and their copies in S3-compatible storage")
    bks = bk.add_subparsers(dest="bk_cmd", required=True)
    bks.add_parser("status", help="schedule, last run and remote copies")
    bks.add_parser("run", help="take a backup now")
    bf = bks.add_parser("restore", help="fetch a remote copy and restore it (replaces all data)")
    bf.add_argument("key", help="the object key shown by `swsdb backups status`")
    bf.add_argument("--yes", "-y", action="store_true")
    k = sub.add_parser("keys", help="API keys bound to one tenant")
    ks = k.add_subparsers(dest="keys_cmd", required=True)
    kc = ks.add_parser("create", help="create a key for --tenant (printed once)")
    kc.add_argument("--role", choices=["admin", "read"], default="read")
    kc.add_argument("--name")
    kl = ks.add_parser("list", help="list keys (never the keys themselves)")
    kl.add_argument("--all-tenants", action="store_true")
    kr = ks.add_parser("revoke", help="revoke a key by its id")
    kr.add_argument("id")
    a = p.parse_args(argv)

    if a.cmd == "mcp":
        try:
            from .mcp_server import serve
        except ImportError:
            print("the MCP server needs the mcp package: pip install 'swsdb[mcp]'", file=sys.stderr)
            return 2
        serve(read_write=a.read_write, http_port=a.http, host=a.host, url=a.url, api_key=a.api_key, tenant=a.tenant,
              verify=not a.insecure)
        return 0
    db = SwsDB(a.url, a.api_key, a.tenant, verify=not a.insecure)
    try:
        if a.cmd == "status":
            out = db.status()
            if not a.json:
                print(f"swsdb {out['version']}: {out['status']}")
                for cap, e in out["engines"].items():
                    lat = f"{e['latency_ms']} ms" if e["latency_ms"] is not None else "-"
                    print(f"  {cap:<7} {e['engine']:<9} {e['state']:<9} {e['version'] or '':<8} {lat}")
                return 0 if out["status"] == "ready" else 1
        elif a.cmd == "collections":
            out = db.collections()
            if not a.json:
                rows = [[c["name"], "yes" if c["vector"] else "", "yes" if c["graph"] else ""] for c in out]
                print(_table(["collection", "vector", "graph"], rows) if rows else "no collections")
                return 0
        elif a.cmd == "doctor":
            out = db.reconcile(repair=a.repair, all_tenants=a.all_tenants)
            if not a.json:
                for c in out["collections"]:
                    issues = {k: v for k, v in c["found"].items() if v}
                    state = ", ".join(f"{k} {v}" for k, v in issues.items()) or "consistent"
                    print(f"  {c['tenant']}/{c['collection']:<20} {c['documents']:>6} documents  {state}")
                verdict = "no issues" if not out["issues"] else (
                    f"{out['issues']} issues, repairs queued" if a.repair else f"{out['issues']} issues "
                    "(run with --repair to fix)")
                print(f"swsdb doctor: {verdict} ({out['took_s']}s)")
                return 0 if not out["issues"] or a.repair else 1
        elif a.cmd == "create":
            db.create_collection(a.collection, documents=True, graph=False if a.no_graph else None)
            out = db.collection(a.collection).info()
            if not a.json:
                print(f"created documents collection {a.collection}" + (" (with a knowledge graph)"
                                                                         if out["documents"]["graph"] else ""))
                return 0
        elif a.cmd == "backups":
            return _backups(db, a)
        elif a.cmd == "webhooks":
            return _webhooks(db, a)
        elif a.cmd == "tenants":
            out = db.tenants()
            if not a.json:
                rows = [[t["tenant"], ", ".join(t["collections"]) or "-", str(t["documents"]), str(t["keys"]),
                         "yes" if t["sql"] else ""] for t in out]
                print(_table(["tenant", "collections", "documents", "keys", "own sql role"], rows) if rows
                      else "no tenants yet")
                return 0
        elif a.cmd == "eval":
            return _eval(db, a)
        elif a.cmd == "demo":
            return _demo(db, a)
        elif a.cmd == "ingest":
            return _ingest(db, a)
        elif a.cmd == "search":
            out = db.collection(a.collection).query(a.query, k=a.k, mode=a.mode)
            if not a.json:
                for n, h in enumerate(out, 1):
                    where = h["document"] + (f" p.{h['page']}" if h.get("page") is not None else "")
                    via = f"  (via graph: {' → '.join(h['graph_path'])})" if h.get("graph_path") else ""
                    print(f"[{n}] {where}  score {h['score']:.3f}{via}")
                    lines = (h.get("text") or "").splitlines()
                    if lines and lines[0].startswith("#"):  # the section heading, already known from the hit
                        lines = lines[1:]
                    print("    " + " ".join(" ".join(lines).split())[:400])
                if not out:
                    print("no passages found")
                return 0
        elif a.cmd == "ask":
            out = db.collection(a.collection).answer(a.question, k=a.k)
            if not a.json:
                _print_answer(out)
                if out["mode"] == "extractive":
                    print("\n(an extractive answer: set SWSDB_LLM_URL and SWSDB_LLM_MODEL on the server to have a "
                          "model write it)")
                return 0
        elif a.cmd == "snapshot":
            return _snapshot(db, a)
        elif a.cmd == "keys":
            if a.keys_cmd == "create":
                out = db.create_key(a.role, a.name)
                if not a.json:
                    print(f"{out['role']} key {out['id']} for tenant {out['tenant']} (shown once, store it now):")
                    print(out["key"])
                    return 0
            elif a.keys_cmd == "list":
                out = db.keys(all_tenants=a.all_tenants)
                if not a.json:
                    rows = [[x["id"], x["tenant"], x["role"], x["name"] or "", x["last_used_at"] or "never",
                             "revoked" if x["revoked_at"] else "active"] for x in out]
                    print(_table(["id", "tenant", "role", "name", "last used", "state"], rows) if rows else "no keys")
                    return 0
            else:
                out = db.revoke_key(a.id)
                if not a.json:
                    print(f"revoked {a.id}")
                    return 0
        elif a.cmd == "sql":
            out = db.sql(a.query, read_only=a.read_only)
            if not a.json:
                print(_table(out["columns"], out["rows"]) if out["columns"] else f"{out['row_count']} rows")
                return 0
        else:
            out = db.collection(a.collection).cypher(a.query, read_only=a.read_only)
            if not a.json:
                print(_table(out["columns"], out["rows"]) if out["columns"] else "\n".join(out["stats"]))
                return 0
        print(json.dumps(out, indent=2))
        return 0
    except SwsDBError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _print_answer(out: dict) -> None:
    print(out["answer"])
    cited = [p for p in out["passages"] if p["cited"]]
    if cited:
        print("Sources:")
        for p in cited:
            page = f", page {p['page']}" if p.get("page") is not None else ""
            title = f" ({p['title']})" if p.get("title") else ""
            print(f"  [{p['n']}] {p['document']}{title}{page}")


def _backups(db: SwsDB, a: argparse.Namespace) -> int:
    if a.bk_cmd == "run":
        r = db.run_backup()
        where = f", uploaded to {r['uploaded']}" if r.get("uploaded") else ""
        print(f"backup {r['snapshot']} ({_size(r['bytes'])}) in {r['took_s']}s{where}")
        return 0
    if a.bk_cmd == "restore":
        m = db.fetch_backup(a.key)
        print(f"fetched {a.key} as snapshot {m['id']} (taken {m.get('created_at')})")
        if not a.yes and input(f"Replace ALL data on {db.url} with it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled (the snapshot stays on the server)")
            return 1
        r = db.restore_snapshot(m["id"])
        print(f"restored in {r['took_s']}s; the data from before is in snapshot {r['backup']}")
        return 0
    b = db.backups()
    s = b["schedule"]
    print(f"schedule: every {s['interval_h']} h, keep {s['keep']}" if s["interval_h"] else
          "schedule: off (set SWSDB_BACKUP_INTERVAL_H)")
    if s["s3"]:
        print(f"storage:  {s['s3']['endpoint']} bucket {s['s3']['bucket']} prefix {s['s3']['prefix'] or '-'}")
    else:
        print("storage:  this server's volume only (set SWSDB_BACKUP_S3_BUCKET to copy backups off it)")
    if s["last"]:
        last = s["last"]
        print(f"last:     {last['at']} {'ok' if last.get('ok') else 'FAILED: ' + last.get('error', '?')}")
    if s["next_at"]:
        print(f"next:     {s['next_at']}")
    if b["remote_error"]:
        print(f"remote:   unreachable: {b['remote_error']}")
    for o in b["remote"]:
        print(f"  {o['key']}  {_size(o['bytes'])}  {o['modified']}")
    return 0


def _webhooks(db: SwsDB, a: argparse.Namespace) -> int:
    split = (lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None)  # noqa: E731
    if a.wh_cmd == "create":
        w = db.create_webhook(a.url, split(a.events), split(a.collections))
        print(f"webhook {w['id']} -> {w['url']} for {', '.join(w['events'])}")
        print(f"signing secret (shown once): {w['secret']}")
    elif a.wh_cmd == "list":
        rows = [[w["id"], w["url"], ",".join(w["events"]), ",".join(w["collections"] or []) or "all",
                 str(w["delivered"]), str(w["pending"]), str(w["failed"])] for w in db.webhooks()]
        print(_table(["id", "url", "events", "collections", "delivered", "pending", "failed"], rows) if rows
              else "no webhooks")
    elif a.wh_cmd == "deliveries":
        rows = [[str(d["id"]), d["event"], d["state"], str(d["attempts"]), str(d["last_status"] or ""),
                 (d["last_error"] or "")[:60]] for d in db.webhook_deliveries(a.id)]
        print(_table(["id", "event", "state", "attempts", "status", "error"], rows) if rows else "no deliveries")
    elif a.wh_cmd == "ping":
        print(f"queued ping {db.ping_webhook(a.id)['queued']}")
    else:
        db.delete_webhook(a.id)
        print(f"deleted {a.id}")
    return 0


def _eval(db: SwsDB, a: argparse.Namespace) -> int:
    col = db.collection(a.collection)
    if a.questions:
        cases = json.loads(Path(a.questions).read_text(encoding="utf-8"))
        col.put_eval_set(a.set, cases)
        print(f"saved {len(cases)} questions as {a.set!r}")
    configs = None
    if a.rerank:
        graph = bool(col.info().get("graph"))
        configs = [{"name": "hybrid", "mode": "hybrid"}, {"name": "hybrid+rerank", "mode": "hybrid", "rerank": True}]
        if graph:
            configs += [{"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1},
                        {"name": "hybrid+graph+rerank", "mode": "hybrid", "graph_hops": 1, "rerank": True}]
    run = col.evaluate(a.set, k=a.k, configs=configs)
    if a.json:
        print(json.dumps(run, indent=2))
        return 0
    rows = [[r["config"]["name"] + (" *" if r["config"]["name"] == run["best"] else ""),
             *(f"{r['metrics'].get(m, 0):.3f}" for m in ("hit", "recall", "mrr", "ndcg", "answer_found")),
             f"{r['metrics']['evidence']:.1f}", f"{r['metrics']['latency_p50_ms']:.0f} ms"] for r in run["results"]]
    print(_table(["configuration", "hit", "recall", "MRR", "nDCG", "answer found", "passages", "p50"], rows))
    print(f"\n{run['cases']} questions, top {run['k']}; best: {run['best']}")
    return 0


def _demo(db: SwsDB, a: argparse.Namespace) -> int:
    data = json.loads((Path(__file__).parent / "demo.json").read_text(encoding="utf-8"))
    try:
        db.collection(a.collection).info()
    except SwsDBError as e:
        if e.status != 404:
            raise
        db.create_collection(a.collection, documents=True)
    col = db.collection(a.collection)
    print(f"Loading the Kestrel Labs handbook ({len(data['documents'])} documents) into {a.collection!r}...")
    for d in data["documents"]:
        col.add_text(d["id"], d["text"], title=d["title"])
    for d in data["documents"]:
        col.wait_ready(d["id"], timeout=600)
    print("Ready. Asking it a few questions:\n")
    for q in data["questions"]:
        print(f"> {q}")
        _print_answer(col.answer(q, k=4))
        print()
    print(f"Next: open {db.url}/console, or ask your own question:")
    print(f'  swsdb ask {a.collection} "What is the meal allowance when travelling?"')
    return 0


SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".text", ".html", ".htm", ".rst", ".csv", ".json"}


def _ingest(db: SwsDB, a: argparse.Namespace) -> int:
    files: list[Path] = []
    for raw in a.paths:
        p = Path(raw)
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED)
        elif p.is_file():
            files.append(p)
        else:
            print(f"error: {raw} not found", file=sys.stderr)
            return 2
    if not files:
        print("no supported files found (PDF, Markdown, HTML, text)", file=sys.stderr)
        return 2
    try:
        db.collection(a.collection).info()
    except SwsDBError as e:
        if e.status != 404:
            raise
        db.create_collection(a.collection, documents=True)
        print(f"created documents collection {a.collection}")
    col = db.collection(a.collection)
    readers = [r.strip() for r in a.readers.split(",") if r.strip()] if a.readers else None
    roots = [Path(r) for r in a.paths if Path(r).is_dir()]
    ids = []
    for f in files:
        root = next((r for r in roots if r in f.parents), None)
        doc_id = f.relative_to(root).as_posix() if root else f.name
        r = col.upload(f, id=doc_id, readers=readers)
        ids.append(doc_id)
        print(f"  {'unchanged' if r.get('unchanged') else 'queued':<9} {doc_id}")
    if a.no_wait:
        print(f"{len(ids)} documents queued")
        return 0
    failed = 0
    for doc_id in ids:
        try:
            d = col.wait_ready(doc_id, timeout=1800)
            print(f"  ready     {doc_id} ({d['chunks']} passages)")
        except SwsDBError as e:
            failed += 1
            print(f"  failed    {doc_id}: {e.message}", file=sys.stderr)
    print(f"{len(ids) - failed} of {len(ids)} documents searchable in {a.collection}")
    return 1 if failed else 0


def _snapshot(db: SwsDB, a: argparse.Namespace) -> int:
    if a.snap_cmd == "create":
        m = db.create_snapshot(a.name)
        print(f"snapshot {m['id']}: {m['documents']} documents, {len(m['vector_collections'])} vector collections, "
              f"{len(m['graphs'])} graphs, {_size(m['bytes'])} in {m['took_s']}s")
        if a.output:
            print(f"downloaded to {db.download_snapshot(m['id'], a.output)}")
    elif a.snap_cmd == "list":
        rows = [[s["id"], s.get("created_at", ""), s.get("swsdb_version", ""), str(s.get("documents", "")),
                 _size(s["bytes"])] for s in db.snapshots()]
        print(_table(["id", "created", "swsdb", "documents", "size"], rows) if rows else "no snapshots")
    elif a.snap_cmd == "download":
        print(f"downloaded to {db.download_snapshot(a.id, a.output or f'{a.id}.tar')}")
    elif a.snap_cmd == "delete":
        db.delete_snapshot(a.id)
        print(f"deleted {a.id}")
    else:
        source = Path(a.source)
        sid = a.source
        if source.is_file():
            sid = db.upload_snapshot(source)["id"]
            print(f"uploaded {source} as {sid}")
        if not a.yes:
            answer = input(f"Replace ALL data on {db.url} with snapshot {sid}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 1
        r = db.restore_snapshot(sid, backup_first=not a.no_backup)
        print(f"restored {r['restored']} (taken {r['created_at']} by swsdb {r['swsdb_version']}) in {r['took_s']}s")
        if r.get("backup"):
            print(f"the data from before is in snapshot {r['backup']}")
        if r.get("reconcile") and r["reconcile"]["issues"]:
            print(f"queued {r['reconcile']['issues']} repairs to bring the stores in line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
