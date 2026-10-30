"""Retrieval quality on a fixed dataset (benchmarks/datasets/kestrel.json): a drop below the floors fails CI.

The server runs as shipped, with no model: the bundled embedding model, and entities linked by the names documents
mention. So the numbers are what a new user gets, not what a particular LLM gets. Set SWSDB_QUALITY_REPORT=<file> to
keep the report.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from swsdb import SwsDB
from test_container import Box, sh

pytestmark = pytest.mark.image

DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "kestrel.json"
ADMIN = "quality-admin-key"
K = 5

# Floors, a little under what this version measures (see benchmarks/README.md). Raise them when retrieval improves.
FLOORS = {
    "all": {"hybrid": {"hit": 0.97, "recall": 0.95, "mrr": 0.95, "answer_found": 0.93},
            "dense": {"recall": 0.95}, "keyword": {"recall": 0.95},
            "hybrid+graph": {"hit": 0.97, "recall": 0.97, "mrr": 0.95, "answer_found": 0.95},
            "hybrid+rerank": {"recall": 0.97, "mrr": 0.95}},
    "multihop": {"hybrid+graph": {"recall": 0.95, "answer_found": 0.85}, "hybrid+rerank": {"recall": 0.9}},
}


@pytest.fixture(scope="module")
def report():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    vol = f"swsdb-quality-{uuid.uuid4().hex[:8]}"
    b = Box(vol, {"SWSDB_API_KEY": ADMIN})
    b.start()
    try:
        b.wait_ready()
        db = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, timeout=600)
        kb = db.create_collection("kestrel", documents=True)
        for d in data["documents"]:
            kb.add_text(d["id"], d["text"], title=d["title"])
        for d in data["documents"]:
            kb.wait_ready(d["id"], timeout=600)
        # two workers wrote graphs sharing entities at once: every mention must have survived
        assert db.reconcile()["issues"] == 0
        cases = [{k: q[k] for k in ("query", "relevant", "answer")} for q in data["questions"]]
        kb.put_eval_set("all", cases)
        kb.put_eval_set("multihop", [c for c, q in zip(cases, data["questions"], strict=True) if q.get("hops") == 2])
        configs = [{"name": "hybrid", "mode": "hybrid"}, {"name": "dense", "mode": "dense"},
                   {"name": "keyword", "mode": "sparse"},
                   {"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1, "graph_k": 3},
                   {"name": "hybrid+graph2", "mode": "hybrid", "graph_hops": 2, "graph_k": 3},
                   {"name": "hybrid+rerank", "mode": "hybrid", "rerank": True},
                   {"name": "hybrid+graph+rerank", "mode": "hybrid", "graph_hops": 1, "graph_k": 3, "rerank": True}]
        out = {}
        for name in ("all", "multihop"):
            run = kb.evaluate(name, k=K, configs=configs)
            out[name] = {r["config"]["name"]: r["metrics"] for r in run["results"]}
            out[name]["_misses"] = {r["config"]["name"]: [m["query"] for m in r["misses"]] for r in run["results"]}
        # the graph's value at equal evidence size: 1 search hit + up to 3 graph passages vs 4 search hits
        same = {"hybrid@4": kb.evaluate("multihop", k=4, configs=[{"name": "hybrid", "mode": "hybrid"}]),
                "hybrid@1+graph3": kb.evaluate("multihop", k=1, configs=[
                    {"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1, "graph_k": 3}]),
                "hybrid@2": kb.evaluate("multihop", k=2, configs=[{"name": "hybrid", "mode": "hybrid"}]),
                "hybrid@1+graph1": kb.evaluate("multihop", k=1, configs=[
                    {"name": "hybrid+graph", "mode": "hybrid", "graph_hops": 1, "graph_k": 1}])}
        out["multihop_equal_evidence"] = {name: r["results"][0]["metrics"] for name, r in same.items()}
        out["dataset"] = {"documents": len(data["documents"]), "questions": len(cases), "k": K}
        if path := os.environ.get("SWSDB_QUALITY_REPORT"):
            Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print_table(out)
        yield out
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)


def print_table(out: dict) -> None:
    print("\nmultihop at equal evidence size")
    for name, m in out["multihop_equal_evidence"].items():
        print(f"  {name:<16} recall {m['recall']:.3f}  evidence {m['evidence']}  "
              f"answer {m.get('answer_found', 0):.3f}")
    for name in ("all", "multihop"):
        print(f"\n{name} ({out['dataset']['k']} results)")
        print(f"  {'config':<14} {'hit':>6} {'recall':>7} {'mrr':>6} {'ndcg':>6} {'answer':>7} {'p50 ms':>7}")
        for cfg, m in out[name].items():
            if cfg.startswith("_"):
                continue
            print(f"  {cfg:<14} {m['hit']:>6.3f} {m['recall']:>7.3f} {m['mrr']:>6.3f} {m['ndcg']:>6.3f} "
                  f"{m.get('answer_found', 0):>7.3f} {m['latency_p50_ms']:>7.1f}")


@pytest.mark.parametrize("set_name", ["all", "multihop"])
def test_no_regression_below_the_floors(report, set_name):
    for cfg, floors in FLOORS[set_name].items():
        got = report[set_name][cfg]
        for metric, floor in floors.items():
            assert got[metric] >= floor, (f"{set_name}/{cfg} {metric} {got[metric]:.3f} < {floor} "
                                          f"(misses: {report[set_name]['_misses'][cfg]})")


def test_graph_expansion_helps_where_it_should(report):
    """Multi-hop questions need a second document linked by a name: the graph should find more of them."""
    multi = report["multihop"]
    assert multi["hybrid+graph"]["recall"] > multi["hybrid"]["recall"], multi
    # with the same number of passages, following the graph is never worse than taking more search hits (on 7
    # questions one tie in the fused ranking moves a result by 0.07, so this compares with >=)
    same = report["multihop_equal_evidence"]
    assert same["hybrid@1+graph1"]["recall"] >= same["hybrid@2"]["recall"], same
    assert same["hybrid@1+graph1"]["answer_found"] > same["hybrid@2"]["answer_found"], same
    # and it doesn't hurt the single-document questions' ranking
    assert report["all"]["hybrid+graph"]["mrr"] >= report["all"]["hybrid"]["mrr"] - 0.02
