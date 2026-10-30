"""Operator commands inside the container: `docker exec swsdb swsdb-admin <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from .settings import Settings


def credentials(s: Settings) -> int:
    try:
        sec = json.loads(s.secrets_file.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"can't read {s.secrets_file}: {e} (run as root or the swsdb user)", file=sys.stderr)
        return 1
    note = "" if s.expose_native else "  (native ports are localhost-only; set SWSDB_EXPOSE_NATIVE=true to publish)"
    print(f"Native engine access{note}")
    if "sql" in s.engines:
        print(f"  postgres  postgresql://swsdb:{sec['postgres_password']}@<host>:5432/swsdb")
    if "vector" in s.engines:
        print(f"  qdrant    http://<host>:6333   api-key: {sec['qdrant_api_key']}")
    if "graph" in s.engines:
        print(f"  falkordb  redis://:{sec['falkordb_password']}@<host>:6379")
    return 0


def status(s: Settings) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s.port}/readyz", timeout=5) as r:
            body, code = json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body, code = json.loads(e.read()), e.code
    except OSError as e:
        print(f"swsdb server not answering on port {s.port}: {e}", file=sys.stderr)
        return 2
    print(f"swsdb: {body['status']}")
    for cap, e in body["engines"].items():
        print(f"  {cap:<7} {e['engine']:<9} {e['state']}")
    return 0 if code == 200 else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="swsdb-admin")
    p.add_argument("command", choices=["status", "credentials"])
    args = p.parse_args()
    s = Settings()
    sys.exit({"status": status, "credentials": credentials}[args.command](s))


if __name__ == "__main__":
    main()
