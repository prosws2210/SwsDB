# Web console

swsdb serves a console at <http://localhost:6464/console>. It talks only to its own server, renders every value as text,
and runs under a strict Content-Security-Policy (no inline code, nothing loaded from elsewhere).

| Page | What you do there |
|---|---|
| **Overview** | Engine health and latency, documents, vectors and graph size, the sync backlog, collections by status. |
| **Collections** | Documents with their status, version, passages and entities. Upload files or paste text. Click a document to see why it is (or isn't) searchable: catalog, vectors and graph, with pending changes. |
| **Search** | **Ask** for a cited answer (click a citation to jump to its passage) or **Search** for passages alone. Pick hybrid, meaning or keywords, the graph depth, and who to ask as. |
| **Graph** | Entities by mentions, their relations and co-mentions as a radial map, and the documents that mention them. |
| **Data** | The raw data. **SQL**: the tenant's tables (click one to look inside) and an editor, read-only unless you untick it. **Cypher**: queries on a collection's graph, with nodes and relationships drawn as a graph (click a node for its properties) and the rows below. **Vectors**: every point of a collection with its payload, its vector and its nearest neighbours. |
| **Evaluation** | Golden sets of questions, runs comparing configurations, recall over time. |
| **Doctor** | Compare the vector store and the graph with the catalog, and queue repairs. |
| **Backups** | Take a snapshot or a backup now; download, restore or delete snapshots; restore copies from S3. Needs the admin key from the environment. |

![Overview](../img/console-overview.jpg)

The Data page goes through the API like everything else: your key's tenant and role apply, SQL runs as the tenant's
sandbox role, and nothing reaches swsdb's own catalog. With Neo4j as the graph engine the Cypher tab is off (see
[deployment](deployment.md#neo4j)).

## The engines' own tools

To look at the engines directly (every tenant, no swsdb permissions in between), start the container with
`SWSDB_EXPOSE_NATIVE=true`, publish the ports you need, and get the passwords with
`docker exec swsdb swsdb-admin credentials`:

| Engine | Tool |
|---|---|
| Qdrant | its dashboard, bundled: `http://localhost:6333/dashboard` (it asks for the Qdrant API key; `SWSDB_QDRANT_DASHBOARD=false` turns it off) |
| PostgreSQL | any client: psql, pgAdmin, DBeaver, TablePlus (port 5432, user `swsdb`) |
| FalkorDB | FalkorDB Browser, run next to swsdb and pointed at port 6379 |

Look, don't edit: a change made in Qdrant or FalkorDB directly makes them drift from the catalog (the Doctor repairs
it), and editing swsdb's own tables in Postgres can break it.

## Keys and tenants

**API key** in the sidebar stores a key for this browser tab only (session storage). A tenant's key switches the
console to its tenant and locks the tenant field; a key from the environment can switch tenants freely. The theme
(dark or light) is remembered per browser.

If the server stops answering, the console shows an offline page and reconnects by itself when it's back.
