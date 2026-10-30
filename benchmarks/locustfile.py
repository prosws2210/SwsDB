"""Load test with Locust: mixed searches, cited answers and writes against one documents collection.

    pip install locust
    SWSDB_API_KEY=... locust -f benchmarks/locustfile.py --host http://localhost:6464 \
        --users 20 --spawn-rate 5 --run-time 5m --headless --csv results/load

Seed the collection first (benchmarks/rag_bench.py leaves none behind; `swsdb ingest bench <folder>` works). Results
are tracked over releases in benchmarks/results/.
"""

from __future__ import annotations

import os
import random
import uuid

from locust import HttpUser, between, task

COLLECTION = os.environ.get("SWSDB_COLLECTION", "bench")
QUESTIONS = ["How long do refunds take?", "Who leads the search project?", "What is the release process?",
             "Where are backups stored?", "How often are access keys rotated?", "What does the platform group run?",
             "When are invoices sent?", "How many vacation days are there?"]


class SwsdbUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self) -> None:
        if key := os.environ.get("SWSDB_API_KEY"):
            self.client.headers["api-key"] = key

    @task(10)
    def search(self) -> None:
        self.client.post(f"/v1/collections/{COLLECTION}/query", name="query hybrid",
                         json={"query": random.choice(QUESTIONS), "k": 10, "graph_hops": 0})

    @task(3)
    def search_with_graph(self) -> None:
        self.client.post(f"/v1/collections/{COLLECTION}/query", name="query hybrid+graph",
                         json={"query": random.choice(QUESTIONS), "k": 10, "graph_hops": 1})

    @task(2)
    def answer(self) -> None:
        self.client.post(f"/v1/collections/{COLLECTION}/answer", name="answer",
                         json={"question": random.choice(QUESTIONS), "k": 5})

    @task(1)
    def write(self) -> None:
        doc = f"load-{uuid.uuid4().hex[:12]}"
        self.client.post(f"/v1/collections/{COLLECTION}/documents/text", name="add document",
                         json={"id": doc, "text": f"Load test note {doc}: {random.choice(QUESTIONS)}"})
