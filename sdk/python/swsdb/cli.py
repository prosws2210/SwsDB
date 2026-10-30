"""`swsdb` command line: talk to a swsdb server (SWSDB_URL, SWSDB_API_KEY, SWSDB_TENANT)."""

from __future__ import annotations

import argparse
import json
import sys

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
    p.add_argument("--tenant", help="tenant (default $SWSDB_TENANT or 'default')")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="engine states and versions")
    sub.add_parser("collections", help="list collections")
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
    a = p.parse_args(argv)

    db = SwsDB(a.url, a.api_key, a.tenant)
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


if __name__ == "__main__":
    sys.exit(main())
