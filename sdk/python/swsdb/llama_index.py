"""LlamaIndex retriever for swsdb: hybrid and graph retrieval, with document permissions, as scored nodes.

    pip install "swsdb[llama-index]"

    from llama_index.core.query_engine import RetrieverQueryEngine
    from swsdb.llama_index import SwsDBRetriever

    retriever = SwsDBRetriever(collection="handbook", k=5, graph_hops=1)     # SWSDB_URL / SWSDB_API_KEY from env
    nodes = retriever.retrieve("How long do refunds take?")
    engine = RetrieverQueryEngine.from_args(retriever, llm=llm)

Node metadata says where each passage came from (document, title, page, heading) and how it was found (`via`, and
`graph_path` for passages reached through the knowledge graph).
"""

from __future__ import annotations

from typing import Any

from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from .aio import AsyncSwsDB
from .client import SwsDB


def to_node(hit: dict) -> NodeWithScore:
    meta = {k: hit.get(k) for k in ("document", "title", "page", "heading", "chunk", "via")}
    if hit.get("graph_path"):
        meta["graph_path"] = hit["graph_path"]
    meta.update(hit.get("metadata") or {})
    node = TextNode(id_=hit.get("chunk_id") or "", text=hit.get("text") or "",
                    metadata={k: v for k, v in meta.items() if v is not None},
                    excluded_embed_metadata_keys=["via", "graph_path", "chunk"],
                    excluded_llm_metadata_keys=["via", "graph_path", "chunk"])
    return NodeWithScore(node=node, score=hit.get("score"))


class SwsDBRetriever(BaseRetriever):
    """Retrieve passages from a swsdb documents collection."""

    def __init__(self, collection: str, k: int = 5, *, mode: str = "hybrid", graph_hops: int | None = None,
                 graph_k: int = 3, rerank: bool | None = None, where: dict[str, Any] | None = None,
                 principals: list[str] | None = None, url: str | None = None, api_key: str | None = None,
                 tenant: str | None = None, client: SwsDB | None = None, async_client: AsyncSwsDB | None = None,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.collection = collection
        self.query_kwargs = {"k": k, "mode": mode, "graph_hops": graph_hops, "graph_k": graph_k, "rerank": rerank,
                             "where": where, "principals": principals}
        self.client = client or SwsDB(url, api_key, tenant)
        self._aclient: AsyncSwsDB | None = async_client

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        hits = self.client.collection(self.collection).query(query_bundle.query_str, **self.query_kwargs)
        return [to_node(h) for h in hits]

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        if self._aclient is None:
            self._aclient = self.client.async_twin()
        hits = await self._aclient.collection(self.collection).query(query_bundle.query_str, **self.query_kwargs)
        return [to_node(h) for h in hits]
