"""LangChain and LlamaIndex retrievers: they ask swsdb the right question and keep where each passage came from."""

import asyncio
import json

import httpx
from swsdb import AsyncSwsDB, SwsDB

HIT = {"chunk_id": "c1", "score": 0.9, "via": "graph", "text": "Refunds take five business days.",
       "document": "refunds", "title": "Refund policy", "page": 2, "heading": "Refunds", "chunk": 0,
       "metadata": {"team": "support"}, "graph_path": ["mentions Priya Nair"]}


def transport(seen):
    def handle(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"hits": [HIT]})
    return handle


def test_langchain_retriever():
    from swsdb.langchain import SwsDBRetriever
    seen = []
    r = SwsDBRetriever(collection="kb", k=3, graph_hops=1, principals=["user:7"],
                       client=SwsDB("http://x", transport=httpx.MockTransport(transport(seen))),
                       async_client=AsyncSwsDB("http://x", transport=httpx.MockTransport(transport(seen))))
    docs = r.invoke("how long do refunds take")
    assert docs[0].page_content == HIT["text"] and docs[0].id == "c1"
    assert docs[0].metadata["document"] == "refunds" and docs[0].metadata["page"] == 2
    assert docs[0].metadata["graph_path"] == ["mentions Priya Nair"] and docs[0].metadata["meta_team"] == "support"
    assert seen[0]["k"] == 3 and seen[0]["graph_hops"] == 1 and seen[0]["principals"] == ["user:7"]
    assert asyncio.run(r.ainvoke("again"))[0].metadata["via"] == "graph"


def test_llama_index_retriever():
    from swsdb.llama_index import SwsDBRetriever
    seen = []
    r = SwsDBRetriever("kb", k=2, rerank=True,
                       client=SwsDB("http://x", transport=httpx.MockTransport(transport(seen))),
                       async_client=AsyncSwsDB("http://x", transport=httpx.MockTransport(transport(seen))))
    nodes = r.retrieve("refunds")
    assert nodes[0].score == 0.9 and nodes[0].node.get_content() == HIT["text"]
    assert nodes[0].node.metadata["document"] == "refunds" and nodes[0].node.metadata["team"] == "support"
    assert seen[0]["rerank"] is True and seen[0]["k"] == 2
    assert asyncio.run(r.aretrieve("again"))[0].node.node_id == "c1"
