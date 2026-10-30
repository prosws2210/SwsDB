"""Does the knowledge graph help on your questions? Compare plain hybrid search with graph expansion.

    python compare.py                       # the Kestrel handbook and its multi-hop questions
    python compare.py --collection docs --questions my_questions.json

Works with no model (swsdb links documents by the names they mention); with SWSDB_LLM_URL / SWSDB_LLM_MODEL a chat
model extracts typed entities and relations too. Questions file: [{"query": ..., "relevant": [doc ids], "answer": ...}].
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from swsdb import SwsDB, SwsDBError

DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "kestrel.json"
CONFIGS = [
    {"name": "hybrid", "mode": "hybrid", "graph_hops": 0},
    {"name": "hybrid+graph (1 hop)", "mode": "hybrid", "graph_hops": 1, "graph_k": 3},
    {"name": "hybrid+graph (2 hops)", "mode": "hybrid", "graph_hops": 2, "graph_k": 3},
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--collection", default="kestrel")
    p.add_argument("--questions", help="a JSON file of questions (default: the handbook's multi-hop questions)")
    p.add_argument("-k", type=int, default=2, help="search hits per question (graph passages come on top)")
    a = p.parse_args()
    db = SwsDB(timeout=600)
    if not db.status()["engines"].get("graph"):
        print("this swsdb runs without the graph engine", file=sys.stderr)
        return 2
    if a.questions:
        cases = json.loads(Path(a.questions).read_text(encoding="utf-8"))
    else:
        data = json.loads(DATASET.read_text(encoding="utf-8"))
        cases = [{k: q[k] for k in ("query", "relevant", "answer")} for q in data["questions"] if q.get("hops") == 2]
        try:
            db.collection(a.collection).info()
        except SwsDBError:
            col = db.create_collection(a.collection, documents=True)
            for d in data["documents"]:
                col.add_text(d["id"], d["text"], title=d["title"])
            for d in data["documents"]:
                col.wait_ready(d["id"])
    col = db.collection(a.collection)
    entities = col.cypher("MATCH (e:Entity) RETURN count(e)", read_only=True)["rows"][0][0]
    if not entities:
        print("note: the graph has no entities (SWSDB_ENTITIES=off when the documents were added), so only "
              "neighbouring passages can be added.\n")
    col.put_eval_set("graph-compare", cases)
    run = col.evaluate("graph-compare", k=a.k, configs=CONFIGS)
    print(f"{len(cases)} questions, {a.k} search hits each, {entities} entities in the graph\n")
    print(f"{'configuration':<24}{'recall':>8}{'MRR':>8}{'answer found':>14}{'passages':>10}{'p50 ms':>9}")
    for r in run["results"]:
        m = r["metrics"]
        print(f"{r['config']['name']:<24}{m['recall']:>8.3f}{m['mrr']:>8.3f}{m.get('answer_found', 0):>14.3f}"
              f"{m['evidence']:>10.1f}{m['latency_p50_ms']:>9.1f}")
    print(f"\nbest: {run['best']}  (see it in the console: Evaluation)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
