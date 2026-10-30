# Evaluation

Retrieval settings are choices: hybrid or meaning-only, with the graph or without. A **golden set** settles them with
numbers: questions, and the documents that should answer them.

```python
kb.put_eval_set("support-faq", [
    {"query": "How long do refunds take?", "relevant": ["refund-policy"], "answer": "five business days"},
    {"query": "Which office does the Orion lead work from?", "relevant": ["project-orion", "person-priya-nair"]},
])
run = kb.evaluate("support-faq", k=5)
print(run["best"])
for r in run["results"]:
    print(r["config"]["name"], r["metrics"])
```

By default each question is asked four ways: `hybrid`, `dense`, `keyword` and `hybrid+graph`. Pass `configs=[...]` to
compare your own (`mode`, `graph_hops`, `graph_k`, `where`).

| Metric | Meaning |
|---|---|
| `hit` | share of questions where at least one relevant document came back |
| `recall` | share of the relevant documents that came back |
| `mrr` | how high the first relevant document ranks (1 = first) |
| `ndcg` | ranking quality over all relevant documents |
| `answer_found` | share of questions whose `answer` phrase appears in the returned text |
| `evidence` | passages returned on average (graph passages come on top of `k`) |
| `latency_p50_ms`, `latency_p95_ms` | time per question |

Runs are stored. The console's Evaluation page shows the winner, the metrics side by side, what the winner missed,
and the best recall over time, so a regression after a change is visible. Golden sets are checked against
permissions too: pass `principals` to evaluate as a user.

swsdb's own CI runs the same evaluation on a fixed dataset ([`benchmarks/datasets/kestrel.json`](https://github.com/prosws2210/SwsDB/blob/main/benchmarks/datasets/kestrel.json))
and fails if any metric drops below its floor. See [Benchmarks](../benchmarks.md#retrieval-quality).
