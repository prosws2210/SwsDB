# LangChain, LlamaIndex and other clients

swsdb does the retrieval (hybrid search, graph expansion, re-ranking, document permissions); these integrations hand
the passages to the framework you already use.

## LangChain

```bash
pip install "swsdb[langchain]"
```

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from swsdb.langchain import SwsDBRetriever

retriever = SwsDBRetriever(collection="handbook", k=5, graph_hops=1)   # SWSDB_URL / SWSDB_API_KEY from the environment

def cite(docs):
    return "\n\n".join(f"[{i}] ({d.metadata['document']}) {d.page_content}" for i, d in enumerate(docs, 1))

prompt = ChatPromptTemplate.from_template("Answer from these passages, citing [n]:\n{context}\n\nQuestion: {question}")
chain = {"context": retriever | cite, "question": RunnablePassthrough()} | prompt | llm
chain.invoke("Which office does the lead of Project Orion work from?")
```

Each `Document` carries `document`, `title`, `page`, `heading`, `score`, `via` (and `graph_path` for passages
found through the graph) in its metadata, plus your document metadata prefixed `meta_`. Options: `k`, `mode`,
`graph_hops`, `graph_k`, `rerank`, `where`, `principals` (who is asking; admin key), `tenant`. `ainvoke` uses the
async client.

## LlamaIndex

```bash
pip install "swsdb[llama-index]"
```

```python
from llama_index.core.query_engine import RetrieverQueryEngine
from swsdb.llama_index import SwsDBRetriever

retriever = SwsDBRetriever("handbook", k=5, graph_hops=1, rerank=True)
engine = RetrieverQueryEngine.from_args(retriever, llm=llm)
print(engine.query("Who owns the product roadmap?"))
```

Nodes keep swsdb's chunk ids and scores; their metadata is the passage's source and your document metadata.

## Async Python

```python
from swsdb import AsyncSwsDB

async with AsyncSwsDB() as db:
    hits = await db.collection("handbook").query("refunds", k=5)
```

`AsyncSwsDB` has the same methods as `SwsDB`, awaitable.

## TypeScript and JavaScript

```bash
npm install swsdb
```

See the [TypeScript SDK](../reference/typescript.md).

## Anything else

Agents and assistants can use the [MCP server](mcp.md). Anything that speaks HTTP can use the
[REST API](../reference/api.md) (OpenAPI at `/openapi.json`).
