"""swsdb benchmark: ingest throughput, query latency and memory per chunk, against a running container.

Run it inside a helper container that shares the server's network, processes and volume, so the numbers aren't skewed
by Docker Desktop's port forwarding (which adds ~40 ms to every request on Windows and macOS):

    docker run -d --name swsdb-bench -v swsdb-bench:/swsdb/data swsdb/swsdb:dev
    docker run --rm --network container:swsdb-bench --pid container:swsdb-bench -v swsdb-bench:/swsdb/data:ro \
        -v "$PWD/benchmarks:/bench" --entrypoint python swsdb/swsdb:dev /bench/rag_bench.py --local \
        --docs 1000 --out /bench/results/bench.json

(`--container NAME` instead of `--local` measures from the host through `docker exec`.)

Documents are generated (deterministic, seeded): handbook-like paragraphs from a fixed vocabulary, so runs are
comparable across versions and machines. Everything uses the default local embedding model (bge-small-en-v1.5 plus
BM25 on CPU) and no LLM, so the numbers measure swsdb, not a provider.

Memory is sampled per engine as the corpus grows, and RAM per million chunks is the fitted slope. Only Qdrant and
FalkorDB hold data in memory in proportion to it; PostgreSQL's memory is bounded by its configuration (the data stays
on disk) and the server's by its embedding model, so their growth here is warm-up, reported but not extrapolated.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from swsdb import SwsDB, SwsDBError

WORDS = """account access address agent analysis answer approval archive audit backup balance batch billing branch
budget cache calendar capacity catalog certificate change channel checkout client cluster code compliance config
contract cost customer dashboard data database deadline delivery deploy design device discount document domain draft
email engine error estimate event expense export feature feedback file filter firewall forecast form gateway goal
guide hardware holiday incident index invoice issue job key kubernetes label latency launch leave license limit log
manager market meeting memory metric migration milestone mobile model monitor network notice office onboarding order
outage owner package parcel partner password payment payroll permission pipeline plan platform policy portal price
priority privacy process product project queue quota receipt record refund region release report request resource
retention review risk roadmap role rollback route salary schedule schema search secret security server service session
shipping signature snapshot software sprint staff storage subscription supplier support survey system task team
template tenant test ticket timeline token traffic training transfer travel update upgrade usage user vacation vendor
version volume warehouse webhook workflow""".split()
NAMES = ["Priya Nair", "Mateo Ruiz", "Amara Okafor", "Lena Fischer", "Tomas Weber", "Elif Demir", "Jonas Berg",
         "Sofia Rossi", "Kenji Sato", "Nadia Haddad", "Omar Farouk", "Ines Costa"]
PLACES = ["Lisbon", "Berlin", "Vienna", "Lagos", "Rotterdam", "Madrid", "Oslo", "Prague"]


def paragraph(rnd: random.Random) -> str:
    out = []
    for _ in range(rnd.randint(4, 7)):
        words = rnd.sample(WORDS, rnd.randint(8, 14))
        if rnd.random() < 0.3:
            words.insert(rnd.randint(0, len(words)), rnd.choice(NAMES))
        if rnd.random() < 0.2:
            words.insert(rnd.randint(0, len(words)), f"in {rnd.choice(PLACES)}")
        if rnd.random() < 0.3:
            words.insert(rnd.randint(0, len(words)), f"within {rnd.randint(2, 90)} days")
        out.append(" ".join(words).capitalize() + ".")
    return " ".join(out)


def document(i: int, paragraphs: int) -> tuple[str, str]:
    rnd = random.Random(i)
    title = " ".join(rnd.sample(WORDS, 3)).title()
    body = "\n\n".join(paragraph(rnd) for _ in range(paragraphs))
    return f"doc-{i:06d}", f"# {title}\n\n{body}"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))], 1)


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def inside(container: str | None, *cmd: str) -> str:
    """Run a command in the server's container (from the host), or here (--local: sharing its processes and volume)."""
    if container is None:
        return subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout
    return docker("exec", container, *cmd)


def slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y over x."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / var if var else 0.0


def memory(container: str | None) -> dict:
    """Resident memory per engine inside the container, in MiB (not counting this benchmark, with --local)."""
    out = inside(container, "ps", "-eo", "pid=,rss=,comm=")
    groups = {"postgres": 0, "qdrant": 0, "falkordb": 0, "swsdb-server": 0, "other": 0}
    mine = {os.getpid()}
    for line in out.strip().splitlines():
        pid, rss, comm = line.split(None, 2)
        if container is None and (int(pid) in mine or comm.startswith("ps")):
            continue
        kib = int(rss)
        name = "postgres" if comm.startswith("postgres") else "qdrant" if comm.startswith("qdrant") else \
            "falkordb" if comm.startswith("redis") else "swsdb-server" if comm.startswith(("python", "swsdb")) \
            else "other"
        groups[name] += kib
    mib = {k: round(v / 1024, 1) for k, v in groups.items()}
    mib["total"] = round(sum(groups.values()) / 1024, 1)
    return mib


def disk(container: str | None) -> dict:
    out = inside(container, "du", "-sk", "/swsdb/data/postgres", "/swsdb/data/qdrant", "/swsdb/data/graph",
                 "/swsdb/data/files")
    return {Path(p).name: round(int(k) / 1024, 1) for k, p in (line.split() for line in out.strip().splitlines())}


def machine(container: str | None) -> dict:
    if container is not None:
        return {"measured_from": "host", "platform": platform.platform(),
                "cpus": docker("info", "--format", "{{.NCPU}}").strip(),
                "memory_gib": round(int(docker("info", "--format", "{{.MemTotal}}").strip()) / 2**30, 1)}
    mem_kib = next(int(x.split()[1]) for x in open("/proc/meminfo") if x.startswith("MemTotal"))
    cpu = next((x.split(":", 1)[1].strip() for x in open("/proc/cpuinfo") if x.startswith("model name")), "?")
    return {"measured_from": "a container on the server's network", "platform": platform.platform(),
            "cpu": cpu, "cpus": os.cpu_count(), "memory_gib": round(mem_kib / 2**20, 1)}


def time_queries(col, queries: list[str], concurrency: int, **kw) -> dict:
    lat: list[float] = []
    server: list[float] = []

    def one(q: str) -> None:
        t0 = time.perf_counter()
        r = col.db._call("POST", f"/v1/collections/{col.name}/query", {"query": q, **kw})
        lat.append((time.perf_counter() - t0) * 1000)
        server.append(r["took_ms"])

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        list(pool.map(one, queries))
    wall = time.perf_counter() - t0
    return {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "p99_ms": pct(lat, 99),
            "mean_ms": round(statistics.fmean(lat), 1), "server_p50_ms": pct(server, 50),
            "server_p95_ms": pct(server, 95), "qps": round(len(queries) / wall, 1),
            "concurrency": concurrency, "queries": len(queries)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="http://localhost:6464")
    p.add_argument("--api-key")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--container", help="the swsdb container's name (measure from the host with docker exec)")
    where.add_argument("--local", action="store_true", help="run inside a helper container sharing the server's "
                                                              "processes (--pid container:...) and volume")
    p.add_argument("--docs", type=int, default=2000)
    p.add_argument("--paragraphs", type=int, default=6, help="paragraphs per document (~4 chunks per 6)")
    p.add_argument("--queries", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--collection", default="bench")
    p.add_argument("--out", help="write the results as JSON here")
    a = p.parse_args()
    target = None if a.local else a.container
    os.environ["SWSDB_BENCH_PID"] = str(os.getpid())

    db = SwsDB(a.url, a.api_key, timeout=600)
    if not db.ready():
        print(f"swsdb at {a.url} is not ready", file=sys.stderr)
        return 2
    status = db.status()
    try:
        db.delete_collection(a.collection)
    except SwsDBError:
        pass
    col = db.create_collection(a.collection, documents=True)
    col.add_text("warmup", "Warm-up document so the embedding model is loaded before measuring.")
    col.wait_ready("warmup")
    col.query("warm up")
    time.sleep(2)
    idle_mem, idle_disk = memory(target), disk(target)

    docs = [document(i, a.paragraphs) for i in range(a.docs)]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda d: col.add_text(d[0], d[1]), docs))
    queued_s = time.perf_counter() - t0
    curve = [(0.0, idle_mem)]  # (chunks held, memory): the slope is the cost per chunk
    next_mark = 0.25
    while True:
        info = col.info()
        by = info["documents"]["by_status"]
        if by.get("failed"):
            print(f"failed documents: {by}", file=sys.stderr)
            return 1
        if (by.get("ready", 0) - 1) / a.docs >= next_mark:
            curve.append((float(info["vector"]["points"] - 1), memory(target)))
            next_mark += 0.25
        if by.get("ready", 0) >= a.docs + 1:
            break
        time.sleep(0.5)
    ingest_s = time.perf_counter() - t0
    chunks = col.info()["vector"]["points"] - 1  # less the warm-up document's chunk
    time.sleep(3)
    full_mem, full_disk = memory(target), disk(target)

    rnd = random.Random(42)
    queries = [" ".join(rnd.sample(WORDS, 4)) + (f" {rnd.choice(NAMES)}" if rnd.random() < 0.3 else "")
               for _ in range(a.queries)]
    latency = {}
    for name, kw in (("hybrid", {"mode": "hybrid", "graph_hops": 0}), ("dense", {"mode": "dense", "graph_hops": 0}),
                     ("keyword", {"mode": "sparse", "graph_hops": 0}),
                     ("hybrid+graph", {"mode": "hybrid", "graph_hops": 1})):
        latency[name] = {"sequential": time_queries(col, queries[:100], 1, k=10, **kw),
                         "concurrent": time_queries(col, queries, a.concurrency, k=10, **kw)}

    # searches while a batch of new documents is being indexed (they compete for the CPU with embedding)
    extra = [document(a.docs + i, a.paragraphs) for i in range(max(a.docs // 10, 20))]
    feeder = ThreadPoolExecutor(4)
    pending = [feeder.submit(col.add_text, d[0], d[1]) for d in extra]
    time.sleep(1)
    latency["hybrid during ingest"] = {"sequential": time_queries(col, queries[:100], 1, k=10, mode="hybrid",
                                                                  graph_hops=0)}
    for f in pending:
        f.result()
    feeder.shutdown()
    while col.info()["documents"]["by_status"].get("ready", 0) < a.docs + 1 + len(extra):
        time.sleep(0.5)

    grow = {k: round(full_mem[k] - idle_mem[k], 1) for k in full_mem}
    # MiB per chunk (fitted over the growing corpus) -> GiB per million chunks, for the engines holding data in RAM
    per_m = {k: round(slope([(x, m[k]) for x, m in curve]) * 1_000_000 / 1024, 2) for k in ("qdrant", "falkordb")}
    per_m["data_in_ram"] = round(per_m["qdrant"] + per_m["falkordb"], 2)
    disk_per_m = {k: round((full_disk[k] - idle_disk.get(k, 0)) / max(chunks, 1) * 1_000_000 / 1024, 2)
                  for k in full_disk}
    result = {
        "swsdb": status["version"], "engines": {c: e["version"] for c, e in status["engines"].items()},
        "machine": machine(target),
        "corpus": {"documents": a.docs, "chunks": chunks, "paragraphs_per_doc": a.paragraphs},
        "ingest": {"seconds": round(ingest_s, 1), "queue_seconds": round(queued_s, 1),
                   "documents_per_s": round(a.docs / ingest_s, 1), "chunks_per_s": round(chunks / ingest_s, 1)},
        "latency": latency,
        "memory_mib": {"idle": idle_mem, "loaded": full_mem, "growth": grow,
                       "curve": [{"chunks": int(x), **m} for x, m in curve]},
        "ram_gib_per_million_chunks": per_m,
        "disk_mib": {"idle": idle_disk, "loaded": full_disk},
        "disk_gib_per_million_chunks": disk_per_m,
    }
    print(json.dumps(result, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    db.delete_collection(a.collection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
