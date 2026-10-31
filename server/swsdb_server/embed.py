"""Embeddings: dense (meaning) and sparse BM25 (keywords).

Dense vectors come from a local ONNX model built into the image (works offline, no key) or from any OpenAI-compatible
embeddings endpoint (OpenAI, Ollama, vLLM, ...). Sparse vectors are always local BM25.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from . import timing

log = logging.getLogger("swsdb.embed")

LOCAL_DENSE = "BAAI/bge-small-en-v1.5"
LOCAL_DENSE_DIM = 384
LOCAL_SPARSE = "Qdrant/bm25"
LOCAL_RERANK = "Xenova/ms-marco-MiniLM-L-6-v2"  # cross-encoder, Apache-2.0, baked into the image


@dataclass
class Sparse:
    indices: list[int]
    values: list[float]


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """`provider` is "local" or an OpenAI-compatible base URL's provider label; `model` names the dense model."""

    def __init__(self, provider: str = "local", model: str | None = None, url: str | None = None,
                 api_key: str | None = None, cache_dir: str | None = None, batch: int = 32,
                 query_cache: int = 10_000, threads: int | None = None):
        self.provider = provider
        self.model = model or (LOCAL_DENSE if provider == "local" else "text-embedding-3-small")
        self.url = (url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.batch = batch
        self.threads = threads  # the model's CPU threads (None: every core)
        self._dense = None
        self._sparse = None
        self._reranker = None
        self.rerank_model = LOCAL_RERANK
        self._dim: int | None = LOCAL_DENSE_DIM if provider == "local" and self.model == LOCAL_DENSE else None
        self._lock = threading.Lock()  # one inference at a time: onnxruntime already uses every core
        self._queries_waiting = 0  # documents give way to queries between their small batches
        self._count = threading.Lock()
        self._http = httpx.AsyncClient(timeout=120) if provider != "local" else None
        # questions' vectors, most recently used last: a question's vector never changes for a model, so a repeated
        # question skips the model entirely. Bounded (10,000 questions is ~15 MB); documents never go through it
        self._qcache: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._qcache_max = query_cache

    def _cached(self, kind: str, text: str):
        hit = self._qcache.get((kind, text))
        if hit is not None:
            self._qcache.move_to_end((kind, text))
        return hit

    def _remember(self, kind: str, text: str, value) -> None:
        if self._qcache_max <= 0:
            return
        self._qcache[(kind, text)] = value
        if len(self._qcache) > self._qcache_max:
            self._qcache.popitem(last=False)

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    # -- local models load lazily: a server that never ingests never pays their memory

    def _load_dense(self):
        if self._dense is None:
            from fastembed import TextEmbedding
            log.info("loading embedding model %s", self.model)
            self._dense = TextEmbedding(self.model, cache_dir=self.cache_dir, threads=self.threads)
        return self._dense

    def _load_reranker(self):
        if self._reranker is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            log.info("loading re-ranking model %s", self.rerank_model)
            self._reranker = TextCrossEncoder(self.rerank_model, cache_dir=self.cache_dir, threads=self.threads)
        return self._reranker

    def _load_sparse(self):
        if self._sparse is None:
            from fastembed import SparseTextEmbedding
            self._sparse = SparseTextEmbedding(LOCAL_SPARSE, cache_dir=self.cache_dir, threads=self.threads)
        return self._sparse

    DOC_BATCH = 8  # documents are embedded a few passages at a time, so a search never waits long behind them

    def _run(self, fn, texts: list[str], query: bool) -> list:
        """One inference at a time. A search goes first: documents wait between their small batches while one is
        queued, so search latency stays low during bulk ingestion."""
        if query:
            with self._count:
                self._queries_waiting += 1
            try:
                with self._lock:
                    return fn(texts, True)
            finally:
                with self._count:
                    self._queries_waiting -= 1
        out: list = []
        for i in range(0, len(texts), self.DOC_BATCH):
            while self._queries_waiting:
                time.sleep(0.002)
            with self._lock:
                out += fn(texts[i:i + self.DOC_BATCH], False)
        return out

    def _dense_batch(self, texts: list[str], query: bool) -> list[list[float]]:
        m = self._load_dense()
        it = m.query_embed(texts) if query else m.embed(texts, batch_size=self.batch)
        return [v.tolist() for v in it]

    def _sparse_batch(self, texts: list[str], query: bool) -> list[Sparse]:
        m = self._load_sparse()
        it = m.query_embed(texts) if query else m.embed(texts, batch_size=self.batch)
        return [Sparse(e.indices.tolist(), e.values.tolist()) for e in it]

    def _local_dense(self, texts: list[str], query: bool) -> list[list[float]]:
        return self._run(self._dense_batch, texts, query)

    def _local_sparse(self, texts: list[str], query: bool) -> list[Sparse]:
        if query:  # BM25 is tokenizing and counting, not the neural model: a question needn't wait for the lock
            return self._sparse_batch(texts, True)
        return self._run(self._sparse_batch, texts, query)

    @timing.timed("embed")
    async def dense(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        if query and len(texts) == 1:
            if (hit := self._cached("dense", texts[0])) is not None:
                return [hit]
            out = await self._embed_dense(texts, query)
            self._remember("dense", texts[0], out[0])
            return out
        return await self._embed_dense(texts, query)

    async def _embed_dense(self, texts: list[str], query: bool) -> list[list[float]]:
        if self.provider == "local":
            return await asyncio.to_thread(self._local_dense, texts, query)
        out: list[list[float]] = []
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for i in range(0, len(texts), self.batch):
            try:
                r = await self._http.post(f"{self.url}/embeddings", headers=headers,
                                          json={"model": self.model, "input": texts[i:i + self.batch]})
            except httpx.HTTPError as e:
                raise EmbeddingError(f"embeddings endpoint {self.url} unreachable: {type(e).__name__}") from e
            if r.status_code >= 400:
                raise EmbeddingError(f"embeddings endpoint answered {r.status_code}: {r.text[:300]}")
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out += [d["embedding"] for d in data]
        self._dim = len(out[0])
        return out

    @timing.timed("embed")
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Cross-encoder relevance of each text to the query (a logit: higher is more relevant). Runs with a search's
        priority, so document embedding gives way to it."""
        if not texts:
            return []

        def score(batch: list[str], _query: bool) -> list[float]:
            return [float(s) for s in self._load_reranker().rerank(query, batch, batch_size=32)]
        return await asyncio.to_thread(self._run, score, texts, True)

    @timing.timed("embed")
    async def sparse(self, texts: list[str], query: bool = False) -> list[Sparse]:
        if not texts:
            return []
        if query and len(texts) == 1:
            if (hit := self._cached("sparse", texts[0])) is not None:
                return [hit]
            out = await asyncio.to_thread(self._local_sparse, texts, query)
            self._remember("sparse", texts[0], out[0])
            return out
        return await asyncio.to_thread(self._local_sparse, texts, query)

    async def dim(self) -> int:
        if self._dim is None:
            self._dim = len((await self.dense(["dimension probe"]))[0])
        return self._dim

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
