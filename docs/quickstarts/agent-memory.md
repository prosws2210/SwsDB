# Agent memory over MCP

swsdb is an [MCP](https://modelcontextprotocol.io) server. Connect Claude, Cursor or any MCP client, and the
assistant can search your documents, answer with citations, and remember things across conversations. It acts with
the permissions of the key you give it, and nothing more.

## Connect an assistant

The server speaks MCP over HTTP:

| URL | Tools | Key |
|---|---|---|
| `http://localhost:6464/mcp` | read-only: `search`, `ask`, `get_document`, `list_collections`, `list_documents`, `sql_query`, `graph_query`, `recall` | any key |
| `http://localhost:6464/mcp/rw` | also `remember`, `forget`, `add_document`, `delete_document` | an admin key |

=== "Claude Code"

    ```bash
    claude mcp add --transport http swsdb http://localhost:6464/mcp/rw --header "api-key: $SWSDB_API_KEY"
    ```

=== "Claude Desktop / Cursor (stdio)"

    ```bash
    pip install "swsdb[mcp]"
    ```
    ```json
    {
      "mcpServers": {
        "swsdb": {
          "command": "swsdb",
          "args": ["mcp", "--read-write"],
          "env": {"SWSDB_URL": "http://localhost:6464", "SWSDB_API_KEY": "..."}
        }
      }
    }
    ```

=== "Any HTTP client"

    Send the key as an `api-key` header (or `Authorization: Bearer ...`). Add `swsdb-tenant` to pick a tenant with a
    key from the environment; a tenant's own key always acts for its tenant.

Then ask the assistant something like *"remember that I prefer invoices in euros"*, and in a new conversation,
*"what do you know about my billing preferences?"*.

## How memory works

`remember` stores the text as a document in the `memory` collection (created on first use) and returns once it is
searchable, so a `recall` straight after finds it. Tags go into the document's metadata and filter `recall`:

```text
remember(text="The user prefers invoices in euros, sent on the first.", tags=["billing"])
recall(query="when should invoices go out?")           -> - (m-20261031-...) The user prefers invoices in euros...
recall(query="deploy schedule", tags=["billing"])      -> only billing memories are searched
forget(id="m-20261031-...")
```

Memories are ordinary documents. They appear in the console, can be restricted with `readers`, backed up with
snapshots, and isolated per tenant: give each user or agent its own tenant key, and their memories stay apart.

[`examples/agent-memory/memory_demo.py`](https://github.com/prosws2210/SwsDB/blob/main/examples/agent-memory/memory_demo.py)
does the same through an MCP client session:

```bash
pip install "swsdb[mcp]"
python examples/agent-memory/memory_demo.py
```

## One key per agent

```bash
swsdb --tenant support-bot keys create --role admin --name "support assistant"
```

The key reaches only the `support-bot` tenant: its collections, memories and SQL schema. Use a `read` key with `/mcp`
for an assistant that should only look things up. See [Security and tenants](../guides/security.md).
