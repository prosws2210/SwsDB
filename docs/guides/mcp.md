# MCP server

swsdb gives AI assistants and agents its search, answers, documents and memory as
[MCP](https://modelcontextprotocol.io) tools. Every tool calls the REST API with the caller's key and tenant, so an
agent gets exactly that key's permissions: tenant, document readers and read-only limits all apply.

## Endpoints

| Transport | Address | Tools |
|---|---|---|
| Streamable HTTP | `http://<host>:6464/mcp` | read-only set; any valid key |
| Streamable HTTP | `http://<host>:6464/mcp/rw` | read-write set; an admin key (403 otherwise) |
| stdio | `swsdb mcp` / `swsdb mcp --read-write` | for desktop clients; `pip install "swsdb[mcp]"` |

`swsdb mcp --http 8765` serves the tools over HTTP from your own machine instead (for example in front of a remote
swsdb). The server-side endpoints check the key before any MCP session starts, and refuse cross-site browser
requests like the rest of the API.

## Tools

| Tool | Set | Does |
|---|---|---|
| `list_collections` | read | collections the key can see, with document counts |
| `search(collection, query, k, mode, graph_hops, where)` | read | numbered passages with document, title, page and how they were found |
| `ask(collection, question, k)` | read | a short answer citing passages as `[n]`, then the sources |
| `get_document(collection, id)` | read | the full text of a document the key may read |
| `list_documents(collection, limit, status)` | read | documents and their status (admin key) |
| `sql_query(query, max_rows)` | read | one read-only SQL statement in the tenant's schema |
| `graph_query(collection, query)` | read | one read-only Cypher query |
| `recall(query, k, tags, collection)` | read | search remembered facts |
| `remember(text, tags, collection)` | write | store a fact; returns once it can be recalled |
| `forget(id, collection)` | write | delete a memory |
| `add_document(collection, id, text, title, metadata, readers, wait)` | write | add or replace a document; creates the collection if needed |
| `delete_document(collection, id)` | write | delete a document from every store |

Errors come back as tool errors with the API's status and message, for example
`swsdb 403 forbidden: this key belongs to tenant 'acme' ...`, so the assistant can explain what went wrong.

## Recipes

- **A support bot that can only read.** Create a read key for its tenant, point it at `/mcp`. It sees documents
  readable by everyone in the tenant, and can't write or act for other users.
- **A personal assistant with memory.** Give it an admin key for a tenant of its own and `/mcp/rw`. Memories go to
  the `memory` collection; see [agent memory](../quickstarts/agent-memory.md).
- **An agent that files notes into your knowledge base.** `/mcp/rw` with `add_document(..., readers=["group:team"])`.
