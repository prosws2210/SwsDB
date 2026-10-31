"""Latency of every read the plan targets (plans/performance.md, section 2.1), at scale. Run it next to the server
(inside the same network, so port forwarding isn't counted), after benchmarks/scale/generate.py:

    docker run --rm --network container:swsdb -v "$PWD/benchmarks:/bench" --entrypoint /opt/swsdb/venv/bin/python \\
        swsdb/swsdb:dev /bench/scale/latency.py --key "$SCALE_KEY" --out /bench/results/scale-latency.json

For each endpoint: p50, p95 and p99 over N requests with one client, then with 8 at once, and the average of each
stage of the server's own Server-Timing header. The 8 clients are 8 processes: one Python process can't send more than
~130 requests a second, which made it, not the server, the queue. Parameters vary per request (ids, vectors,
questions), so caches don't flatter the numbers, except in the rows that measure a repeat on purpose.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import random
import statistics
import time

import httpx

TOPICS = ["refunds", "shipping", "warranty", "privacy", "billing", "onboarding", "security", "travel", "payroll",
          "support", "hiring", "laptops", "parental leave", "expenses", "incidents", "data retention"]
ASK = ["who handles {t}", "how long do {t} requests take", "where do {t} escalations go", "what is the {t} policy",
       "who reviews {t} cases", "{t} process for customers", "when are {t} cases closed", "{t} escalation contact"]


def questions(n: int, seed: int) -> list[str]:
    rnd = random.Random(seed)
    out = set()
    while len(out) < n:
        out.add(rnd.choice(ASK).format(t=rnd.choice(TOPICS)) + f" {rnd.randint(1, 10_000)}")
    return list(out)


rnd = random.Random(1)  # ids and vectors; each client process reseeds its own
_MAKE = None  # the endpoint being measured, inherited by the forked client processes


def _client(args) -> tuple[list[float], list[dict]]:
    """One of the 8 clients: its share of the requests, one at a time, on its own connection."""
    url, key, label, ids = args
    rnd.seed(ids[0] * 7919 + 17)

    async def go():
        lat, st = [], []
        async with httpx.AsyncClient(base_url=url, headers={"api-key": key}, timeout=60) as c:
            for i in ids:
                m, p, b = _MAKE(i)
                t0 = time.perf_counter()
                r = await c.request(m, p, json=b)
                lat.append((time.perf_counter() - t0) * 1000)
                if r.status_code >= 400:
                    raise RuntimeError(f"{label}: {r.status_code} {r.text[:200]}")
                st.append(stages(r.headers.get("server-timing", "")))
        return lat, st
    return asyncio.run(go())


def stats(xs: list[float]) -> dict:
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"p50": round(q(0.50), 2), "p95": round(q(0.95), 2), "p99": round(q(0.99), 2), "n": len(xs)}


def stages(header: str) -> dict[str, float]:
    out = {}
    for part in (header or "").split(","):
        bits = part.strip().split(";")
        dur = next((b for b in bits if b.startswith("dur=")), None)
        if bits and dur:
            out[bits[0]] = float(dur[4:])
    return out


class Bench:
    def __init__(self, url: str, keys: dict[str, str], n: int):
        self.url, self.keys, self.n = url, keys, n
        self.rows: list[dict] = []

    def client(self, project: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, headers={"api-key": self.keys[project]}, timeout=60,
                                 limits=httpx.Limits(max_keepalive_connections=16))

    async def run(self, label: str, project: str, make, warm: int = 5) -> None:
        """make(i) -> (method, path, json body), i from -warm to 2n - 1 (unique per request); timed once with one
        client and once with 8 client processes at once."""
        global _MAKE  # noqa: PLW0603 (read by the forked client processes)
        async with self.client(project) as c:
            for i in range(warm):
                m, p, b = make(-1 - i)
                await c.request(m, p, json=b)
        _MAKE = make
        single, st1 = await asyncio.to_thread(_client, (self.url, self.keys[project], label, list(range(self.n))))
        shares = [(self.url, self.keys[project], label, list(range(self.n + w, 2 * self.n, 8))) for w in range(8)]
        with multiprocessing.get_context("fork").Pool(8) as pool:
            parts = await asyncio.to_thread(pool.map, _client, shares)
        conc = [x for lat, _ in parts for x in lat]
        st8 = [s for _, st in parts for s in st]
        keys = sorted({k for s in st1 for k in s})
        breakdown = {k: round(statistics.mean(s.get(k, 0.0) for s in st1), 2) for k in keys}
        total8 = round(statistics.mean(s.get("total", 0.0) for s in st8), 2)
        row = {"endpoint": label, "one_client": stats(single), "eight_clients": stats(conc), "server_timing": breakdown,
               "eight_clients_server_total": total8,
               # what the CI check compares: the server's own time, far steadier than a shared runner's client
               "server_p50": stats([s.get("total", 0.0) for s in st1])["p50"],
               "eight_clients_server_p50": stats([s.get("total", 0.0) for s in st8])["p50"]}
        self.rows.append(row)
        print(f"{label:46s} 1 client p50 {row['one_client']['p50']:7.2f} p95 {row['one_client']['p95']:7.2f} | "
              f"8 clients p50 {row['eight_clients']['p50']:7.2f} p95 {row['eight_clients']['p95']:7.2f} "
              f"(server {total8:6.2f}) | "
              + " ".join(f"{k}={v}" for k, v in breakdown.items()), flush=True)


async def main_async(a) -> dict:
    keys = {"scale": a.key}
    b = Bench(a.url, keys, a.n)
    rows, customers, people, points = a.rows, max(a.rows // 10, 1000), max(a.edges // 5, 1000), a.points
    sql = lambda q, params: ("POST", "/v1/sql", {"query": q, "params": params, "read_only": True})  # noqa: E731

    await b.run("sql: row by primary key", "scale", lambda i: sql("SELECT * FROM orders WHERE id = %s",
                                                                  [rnd.randint(1, rows)]))
    await b.run("sql: indexed filter (customer's orders)", "scale",
                lambda i: sql("SELECT id, status, total FROM orders WHERE customer_id = %s ORDER BY placed_at DESC "
                              "LIMIT 20", [rnd.randint(1, customers)]))
    await b.run("sql: join + aggregate for one customer", "scale",
                lambda i: sql("SELECT p.category, count(*), sum(o.total) FROM orders o JOIN products p ON "
                              "p.id = o.product_id WHERE o.customer_id = %s GROUP BY 1", [rnd.randint(1, customers)]))
    await b.run("rest: row by filter", "scale",
                lambda i: ("GET", f"/v1/rest/orders?id=eq.{rnd.randint(1, rows)}", None))
    await b.run("vectors: get 1 point", "scale",
                lambda i: ("POST", "/v1/collections/vecs/points/get", {"ids": [rnd.randrange(points)]}))

    def vec() -> list[float]:
        v = [rnd.gauss(0, 1) for _ in range(384)]
        s = sum(x * x for x in v) ** 0.5
        return [round(x / s, 5) for x in v]
    await b.run("vectors: search by vector (top 10)", "scale",
                lambda i: ("POST", "/v1/collections/vecs/search", {"vector": vec(), "limit": 10}))
    await b.run("vectors: filtered search (category)", "scale",
                lambda i: ("POST", "/v1/collections/vecs/search", {"vector": vec(), "limit": 10,
                                                                   "where": {"category": "garden"}}))
    await b.run("vectors: similar to a point", "scale",
                lambda i: ("POST", "/v1/collections/vecs/similar", {"like": rnd.randrange(points), "limit": 10}))
    await b.run("graph: 1 hop (who a person knows)", "scale",
                lambda i: ("POST", "/v1/collections/social/cypher", {
                    "query": "MATCH (p:Person {id: $id})-[:KNOWS]->(f) RETURN f.id, f.name LIMIT 20",
                    "params": {"id": rnd.randrange(people)}, "read_only": True}))
    await b.run("graph: 2 hops (friends of friends, count)", "scale",
                lambda i: ("POST", "/v1/collections/social/cypher", {
                    "query": "MATCH (p:Person {id: $id})-[:KNOWS]->()-[:KNOWS]->(f) RETURN count(DISTINCT f)",
                    "params": {"id": rnd.randrange(people)}, "read_only": True}))
    span = 2 * a.n + 10  # every question new: each row gets its own slice, indexed by the request's unique i
    qs = questions(4 * span, a.seed)
    for row, mode in enumerate(("sparse", "dense", "hybrid")):
        await b.run(f"documents: {mode} search, new question", "scale",
                    lambda i, m=mode, o=row * span: ("POST", "/v1/collections/kb/query", {
                        "query": qs[o + i + 5], "k": 5, "mode": m, "graph_hops": 0}))
    await b.run("documents: hybrid + graph, new question", "scale",
                lambda i: ("POST", "/v1/collections/kb/query", {"query": qs[3 * span + i + 5], "k": 5,
                                                                "mode": "hybrid", "graph_hops": 1}))
    await b.run("documents: hybrid search, repeated question", "scale",
                lambda i: ("POST", "/v1/collections/kb/query", {"query": "who handles refunds", "k": 5,
                                                                "mode": "hybrid", "graph_hops": 0}))
    return {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "n": a.n,
            "scale": {"rows": rows, "points": points, "edges": a.edges}, "results": b.rows}


def check(report: dict, baseline_path: str, tolerance: float, floor_ms: float) -> list[str]:
    """Endpoints whose server-side p50 got worse than the baseline's by more than `tolerance` (a fraction) and by
    more than `floor_ms` (so a 1 ms request isn't failed for a 0.3 ms wobble)."""
    with open(baseline_path) as f:
        base = {r["endpoint"]: r for r in json.load(f)["results"]}
    worse = []
    for r in report["results"]:
        b = base.get(r["endpoint"])
        if b is None:
            continue
        for metric in ("server_p50", "eight_clients_server_p50"):
            now, then = r.get(metric), b.get(metric)
            if now is None or then is None:
                continue
            if now > then * (1 + tolerance) and now - then > floor_ms:
                worse.append(f"{r['endpoint']}: {metric} {then:.2f} -> {now:.2f} ms")
    return worse


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:6464")
    p.add_argument("--key", help="the scale project's key")
    p.add_argument("--keyfile", help="or the file generate.py --keyfile wrote it to")
    p.add_argument("--n", type=int, default=200, help="requests per endpoint, per client mode")
    p.add_argument("--rows", type=int, default=1_000_000)
    p.add_argument("--points", type=int, default=250_000)
    p.add_argument("--edges", type=int, default=1_000_000)
    p.add_argument("--out", default=None)
    p.add_argument("--seed", type=int, default=None, help="questions' seed (default: new each run, so the server's "
                   "question cache can't make 'new question' rows look faster)")
    p.add_argument("--check", default=None, help="a baseline report: fail if an endpoint's server-side p50 is worse")
    p.add_argument("--tolerance", type=float, default=0.2, help="how much worse than the baseline fails (0.2: 20%%)")
    p.add_argument("--floor-ms", type=float, default=2.0, help="and by at least this much")
    a = p.parse_args()
    if a.keyfile:
        with open(a.keyfile) as f:
            a.key = json.load(f)["key"]
    if not a.key:
        p.error("--key or --keyfile is required")
    if a.seed is None:
        a.seed = int(time.time())
    report = asyncio.run(main_async(a))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2)
    if a.check:
        if not os.path.exists(a.check):
            print(f"no baseline at {a.check}: nothing to compare (record one from this run's report)")
            return
        worse = check(report, a.check, a.tolerance, a.floor_ms)
        for w in worse:
            print("SLOWER", w)
        if worse:
            raise SystemExit(1)
        print("latency check: no endpoint is slower than the baseline")


if __name__ == "__main__":
    main()
