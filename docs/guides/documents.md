# Documents and retrieval

A **documents collection** is one you give text to: swsdb parses, chunks, embeds and graphs it, and keeps every store
in step. (A collection can also hold raw vectors or a raw graph you manage yourself; see the [API](../reference/api.md).)

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", api_key="...", tenant="acme")
kb = db.create_collection("kb", documents=True)            # graph too, when the graph engine runs
kb.upload("handbook.pdf")                                  # id defaults to the file name
kb.add_text("refunds", "# Refunds\n\nRefunds take five business days.", title="Refund policy",
            metadata={"team": "support", "year": 2026}, readers=["*"])
kb.wait_ready("refunds")
```

## What happens to a document

1. **Accepted.** The catalog row, the original file and an outbox entry are written in one Postgres transaction. Once
   the call returns, the change can't be lost. Status `queued`.
2. **Parsed and chunked.** PDF (by page), Markdown and HTML (by heading) and plain text are split into passages of up
   to about 220 words. Passages never cross a heading or a page, keep their paragraphs, and overlap a little.
3. **Embedded.** Dense vectors (`BAAI/bge-small-en-v1.5`, local and offline by default, or any OpenAI-compatible
   endpoint) and BM25 keyword vectors go into Qdrant, invisible until step 5.
4. **Graphed.** Document, passages in order, and the entities they mention: names found in the text, or typed
   entities and relations from a chat model when one is configured.
5. **Switched on.** The new version becomes the one queries return, and the old version's passages are removed. Status
   `ready`.

Each step is idempotent and retried with backoff if an engine is down. Re-adding identical content is a no-op
(`unchanged: true`); changed content creates version 2, and queries keep returning version 1 until version 2 is
ready. Deleting is immediate for queries and finished in the background.

## Querying

```python
hits = kb.query("how long do refunds take", k=5)                          # hybrid: meaning + keywords, fused
hits = kb.query("refund", mode="sparse")                                  # keywords only
hits = kb.query("money back", mode="dense")                               # meaning only
hits = kb.query("refunds", where={"team": "support", "year": [2025, 2026]})  # metadata: exact, lists mean "any of"
hits = kb.query("who leads Orion", graph_hops=1, graph_k=3)               # + passages connected in the graph
hits = kb.query("how long do refunds take", rerank=True)                  # re-ordered by a cross-encoder
r = kb.answer("How long do refunds take?")                                # a short answer citing passages as [n]
```

`rerank=True` re-reads the top candidates together with the question using a small cross-encoder that is bundled in
the image (`ms-marco-MiniLM-L-6-v2`). It is more precise and costs about 0.2 s per query on a laptop CPU. It's off by
default; `SWSDB_RERANK=true` turns it on for every query, and evaluation can compare it (`{"rerank": true}` in a
config). `where` filters on metadata fields; `{"document": [...]}` restricts to document ids. Every hit is checked against the
catalog, so only a document's live version is ever returned, and nothing being deleted.

## Permissions

Each document has **readers**: principals allowed to find it. The default `["*"]` means everyone in the tenant.

```python
kb.add_text("salaries", "...", readers=["group:hr", "user:7"])
kb.query("salary bands", principals=["user:42", "group:hr"])   # admin key, acting for a user
```

| Caller | Sees |
|---|---|
| admin key, no `principals` | everything in the tenant |
| admin key with `principals` | documents whose readers include `*` or any of the principals |
| read-only key | documents readable by `*` only (it can't act for principals) |

The filter runs inside Qdrant for search and again for every graph passage, cited answer, `text` call and MCP tool.
Raw access that would skip it (vector search, point reads and Cypher on a documents collection) needs the admin key.

## Why isn't it searchable?

```python
kb.diagnose("refunds")
```
```json
{"verdict": "searchable", "reasons": ["every store holds the live version (version 1, 1 passages)"], ...}
```

`diagnose` checks the catalog, pending changes, Qdrant and the graph, and says in plain words what is missing or still
running. The console's Collections page shows the same thing in a drawer.

## Reading a document

```python
kb.text("refunds")["text"]       # the searched text of the live version, if the caller may read it
```

Documents a caller may not read answer 404, exactly like documents that don't exist.
