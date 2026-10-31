"""Scale data for the latency benchmark: a project with a million-row table, a large vector collection, a graph with a
million relationships and a documents collection, made through swsdb's own API (and the project's connection string)
the way an app would. Deterministic: the same seed makes the same data.

    python benchmarks/scale/generate.py --url http://localhost:6464 --key "$SWSDB_API_KEY" \\
        --rows 1000000 --points 250000 --edges 1000000 --docs 1000

Engines are filled one after another, so the machine only holds one load's working set at a time.
"""

from __future__ import annotations

import argparse
import json
import random
import time

import httpx
import numpy as np
import psycopg

CATEGORIES = ["kitchen", "office", "garden", "lighting", "outdoor", "electronics", "toys", "books"]
REGIONS = ["eu", "us", "in", "apac"]
TOPICS = ["refunds", "shipping", "warranty", "privacy", "billing", "onboarding", "security", "travel", "payroll",
          "support", "hiring", "laptops", "parental leave", "expenses", "incidents", "data retention"]
PEOPLE = ["Priya Nair", "Mateo Ruiz", "Amara Okafor", "Lena Fischer", "Tomas Weber", "Elif Demir", "Ravi Kumar",
          "Ana Silva", "Maya Chen", "Leo Park", "Zara Ali", "Omar Haddad"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tables(api: httpx.Client, rows: int) -> None:
    cred = api.post("/v1/postgres/credentials").json()
    customers = max(rows // 10, 1000)
    with psycopg.connect(cred["url"], autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS order_lines, orders, products, customers CASCADE")
        c.execute("""
        CREATE TABLE customers (id bigint PRIMARY KEY, email text UNIQUE NOT NULL, name text NOT NULL,
          region text NOT NULL, created_at timestamptz NOT NULL);
        CREATE TABLE products (id bigint PRIMARY KEY, sku text UNIQUE NOT NULL, category text NOT NULL,
          price numeric(10,2) NOT NULL);
        CREATE TABLE orders (id bigint PRIMARY KEY, customer_id bigint NOT NULL REFERENCES customers(id),
          product_id bigint NOT NULL REFERENCES products(id), status text NOT NULL, qty int NOT NULL,
          total numeric(12,2) NOT NULL, placed_at timestamptz NOT NULL);
        """)
        log(f"tables: {customers:,} customers")
        c.execute("""INSERT INTO customers SELECT i, 'user' || i || '@example.com', 'Customer ' || i,
                     (ARRAY['eu','us','in','apac'])[1 + i %% 4], now() - (i %% 900) * interval '1 day'
                     FROM generate_series(1, %s) i""", [customers])
        c.execute("""INSERT INTO products SELECT i, 'SKU-' || lpad(i::text, 6, '0'),
                     (ARRAY['kitchen','office','garden','lighting','outdoor','electronics','toys','books'])[1 + i % 8],
                     round((5 + (i * 7919 % 49500) / 100.0)::numeric, 2) FROM generate_series(1, 10000) i""")
        step = 200_000
        for a in range(1, rows + 1, step):
            b = min(a + step - 1, rows)
            c.execute("""INSERT INTO orders SELECT i, 1 + (i * 2654435761 %% %s), 1 + (i * 40503 %% 10000),
                         (ARRAY['pending','paid','paid','shipped','cancelled'])[1 + i %% 5], 1 + i %% 4,
                         round(((i * 7919 %% 50000) / 100.0)::numeric, 2), now() - (i %% 365) * interval '1 day'
                         FROM generate_series(%s::bigint, %s::bigint) i""", [customers, a, b])
            log(f"tables: {b:,} / {rows:,} orders")
        c.execute("CREATE INDEX orders_customer_idx ON orders (customer_id)")
        c.execute("CREATE INDEX orders_placed_idx ON orders (placed_at)")
        c.execute("ANALYZE customers; ANALYZE products; ANALYZE orders")
    log("tables: done")


def vectors(api: httpx.Client, points: int, dim: int = 384) -> None:
    api.delete("/v1/collections/vecs")
    r = api.put("/v1/collections/vecs", json={"vector": {"size": dim, "distance": "cosine"}})
    r.raise_for_status()
    rng = np.random.default_rng(7)
    centres = rng.normal(0, 1, (len(CATEGORIES), dim))  # categories cluster, like real embeddings of topics
    batch = 1000
    t0 = time.time()
    for a in range(0, points, batch):
        n = min(batch, points - a)
        cat = rng.integers(0, len(CATEGORIES), n)
        v = centres[cat] + rng.normal(0, 1.2, (n, dim))
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        body = {"points": [{"id": a + i, "vector": np.round(v[i], 5).tolist(),
                            "payload": {"category": CATEGORIES[cat[i]], "region": REGIONS[(a + i) % 4],
                                        "price": round(float(5 + (a + i) * 7919 % 49500 / 100), 2)}}
                           for i in range(n)]}
        api.put("/v1/collections/vecs/points", json=body, timeout=120).raise_for_status()
        if (a // batch) % 20 == 0:
            log(f"vectors: {a + n:,} / {points:,} ({(a + n) / (time.time() - t0):.0f}/s)")
    log("vectors: done")


def graph(api: httpx.Client, edges: int) -> None:
    api.delete("/v1/collections/social")
    api.put("/v1/collections/social", json={"graph": True}).raise_for_status()
    people = max(edges // 5, 1000)

    def cypher(q: str, **params) -> None:
        r = api.post("/v1/collections/social/cypher", json={"query": q, "params": params or None, "read_only": False,
                                                              "timeout_s": 600}, timeout=700)
        r.raise_for_status()

    # the index first, on the empty graph: FalkorDB builds an index added later in the background, and queries that
    # run before it's ready scan every person
    cypher("CREATE INDEX FOR (p:Person) ON (p.id)")
    step = 50_000
    for a in range(0, people, step):
        b = min(a + step, people) - 1
        cypher("UNWIND range($a, $b) AS i CREATE (:Person {id: i, name: 'Person ' + toString(i), "
               "region: ['eu','us','in','apac'][i % 4]})", a=a, b=b)
    log(f"graph: {people:,} people")
    # a power-law-ish degree: a few people know many (hubs), most know a few. The pairs are made here and sent as a
    # parameter, so each MATCH is an indexed lookup by a literal id (a random target computed in Cypher can't use the
    # index, and would scan every person for every relationship)
    rnd = random.Random(9)
    step = 10_000
    for a in range(0, edges, step):
        ids = range(a, min(a + step, edges))
        # WITH makes the ids plain variables: FalkorDB then finds each person by the index (with `{id: list[k]}`
        # inline it scans every person instead)
        cypher("UNWIND range(0, size($xs) - 1) AS k WITH $xs[k] AS a, $ys[k] AS b, $ss[k] AS s "
               "MATCH (x:Person {id: a}) MATCH (y:Person {id: b}) CREATE (x)-[:KNOWS {since: s}]->(y)",
               xs=[i % people for i in ids], ys=[int((people - 1) * rnd.random() ** 3) for _ in ids],
               ss=[2000 + i % 25 for i in ids])
        if (a // step) % 10 == 0 or a + step >= edges:
            log(f"graph: {min(a + step, edges):,} / {edges:,} relationships")
    log("graph: done")


def documents(api: httpx.Client, docs: int) -> None:
    api.delete("/v1/collections/kb")
    api.put("/v1/collections/kb", json={"documents": True}).raise_for_status()
    rnd = random.Random(5)
    for i in range(docs):
        topic, a, b = rnd.choice(TOPICS), rnd.choice(PEOPLE), rnd.choice(PEOPLE)
        text = (f"# {topic.title()} policy {i}\n\n{topic.capitalize()} requests are handled by {a} within "
                f"{rnd.randint(1, 30)} days. Escalations go to {b}.\n\nEvery {topic} case is logged, reviewed "
                f"weekly and closed with a note for the customer.")
        api.post("/v1/collections/kb/documents/text", json={"id": f"doc-{i}", "text": text, "format": "markdown",
                                                           "metadata": {"topic": topic}}).raise_for_status()
    log(f"documents: {docs:,} queued; waiting for them to be searchable")
    while True:
        st = api.get("/v1/collections/kb").json()["documents"]["by_status"]
        if st.get("ready", 0) + st.get("failed", 0) >= docs:
            break
        time.sleep(5)
    log(f"documents: done {st}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:6464")
    p.add_argument("--key", required=True, help="an admin key (the environment's key: a project key is made)")
    p.add_argument("--project", default="scale")
    p.add_argument("--rows", type=int, default=1_000_000)
    p.add_argument("--points", type=int, default=250_000)
    p.add_argument("--edges", type=int, default=1_000_000)
    p.add_argument("--docs", type=int, default=1000)
    p.add_argument("--only", nargs="*", choices=["tables", "vectors", "graph", "documents"])
    p.add_argument("--keyfile", default=None, help="write the project's key here (for the latency runner)")
    a = p.parse_args()
    admin = httpx.Client(base_url=a.url, headers={"api-key": a.key}, timeout=120)
    key = admin.post("/v1/keys", json={"tenant": a.project, "role": "admin", "name": "scale benchmark"}).json()["key"]
    if a.keyfile:
        json.dump({"key": key}, open(a.keyfile, "w"))
    api = httpx.Client(base_url=a.url, headers={"api-key": key}, timeout=120)
    todo = a.only or ["tables", "vectors", "graph", "documents"]
    if "tables" in todo:
        tables(api, a.rows)
    if "vectors" in todo:
        vectors(api, a.points)
    if "graph" in todo:
        graph(api, a.edges)
    if "documents" in todo:
        documents(api, a.docs)


if __name__ == "__main__":
    main()
