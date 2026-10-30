"""LangChain retriever for swsdb: hybrid and graph retrieval, with document permissions, as LangChain Documents.

    pip install "swsdb[langchain]"

    from swsdb.langchain import SwsDBRetriever
    retriever = SwsDBRetriever(collection="handbook", k=5, graph_hops=1)     # SWSDB_URL / SWSDB_API_KEY from env
    docs = retriever.invoke("How long do refunds take?")
    chain = {"context": retriever, "question": RunnablePassthrough()} | prompt | llm

Each Document's metadata says where the passage came from (document, title, page, heading), its score and how it was
found (`via`: hybrid, dense, sparse or graph, with `graph_path`), so answers can cite their sources.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForRetrieverRun, CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field, PrivateAttr

from .aio import AsyncSwsDB
from .client import SwsDB


def to_document(hit: dict) -> Document:
    meta = {k: hit.get(k) for k in ("document", "title", "page", "heading", "chunk", "chunk_id", "score", "via")}
    if hit.get("graph_path"):
        meta["graph_path"] = hit["graph_path"]
    meta.update({f"meta_{k}": v for k, v in (hit.get("metadata") or {}).items()})
    return Document(page_content=hit.get("text") or "", metadata={k: v for k, v in meta.items() if v is not None},
                    id=hit.get("chunk_id"))


class SwsDBRetriever(BaseRetriever):
    """Retrieve passages from a swsdb documents collection."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    collection: str
    k: int = 5
    mode: str = "hybrid"
    graph_hops: int | None = None
    graph_k: int = 3
    rerank: bool | None = None
    where: dict[str, Any] | None = None
    principals: list[str] | None = Field(None, description="who is asking (needs the admin key)")
    url: str | None = None
    api_key: str | None = None
    tenant: str | None = None
    client: SwsDB | None = None
    async_client: AsyncSwsDB | None = None
    _aclient: AsyncSwsDB | None = PrivateAttr(None)

    def _kwargs(self) -> dict:
        return {"k": self.k, "mode": self.mode, "graph_hops": self.graph_hops, "graph_k": self.graph_k,
                "where": self.where, "principals": self.principals, "rerank": self.rerank}

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        if self.client is None:
            self.client = SwsDB(self.url, self.api_key, self.tenant)
        return [to_document(h) for h in self.client.collection(self.collection).query(query, **self._kwargs())]

    async def _aget_relevant_documents(self, query: str, *,
                                       run_manager: AsyncCallbackManagerForRetrieverRun) -> list[Document]:
        if self._aclient is None:
            self._aclient = self.async_client or (self.client.async_twin() if self.client
                                                  else AsyncSwsDB(self.url, self.api_key, self.tenant))
        hits = await self._aclient.collection(self.collection).query(query, **self._kwargs())
        return [to_document(h) for h in hits]
