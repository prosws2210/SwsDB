# Web console

swsdb serves a console at <http://localhost:6464/console>. It talks only to its own server, renders every value as text,
and runs under a strict Content-Security-Policy (no inline code, nothing loaded from elsewhere).

| Page | What you do there |
|---|---|
| **Overview** | Engine health and latency; documents, vectors and graph size across every collection (document collections and the ones you fill yourself); the sync backlog; **activity** on this server over the last hour: searches and documents indexed per minute, and median and p95 search time. |
| **Collections** | Documents with their status, version, passages, entities and their most common metadata fields, 100 per page, searchable by id or title and sortable. **What's inside** counts the documents by metadata value (and by day added); click a value to filter the list, and the counts narrow to the filtered documents. Upload files or paste text. Click a document to see why it is (or isn't) searchable: catalog, vectors and graph, with pending changes. |
| **Search** | **Ask** for a cited answer (click a citation to jump to its passage) or **Search** for passages alone. Pick hybrid, meaning or keywords, the graph depth, who to ask as, and **filters** on document metadata (picked from the values the collection holds). Every passage shows its document's metadata. |
| **Graph** | Entities by mentions (with how many there are in all), their relations and co-mentions as a radial map, and the documents that mention them. |
| **Storage** | Buckets (private or public to the tenant) with their files and size; upload into a folder, filter by folder, download and delete files. |
| **Reports** | Saved SQL queries pinned (☆ in the SQL editor) as live charts, run fresh each time the page opens. |
| **Data** | The raw data. **Database**: functions (with **Call**, which runs one with JSON arguments through `/v1/rpc`), triggers, enumerated types, views, indexes and the server's extensions. **Schema**: the tenant's tables as a diagram, foreign keys drawn to the columns they reference. **Tables**: every table and view in the tenant's schema with its columns, estimated rows and size; open one to page through it as a grid (column types and keys in the header, NULLs marked), sort by a column, filter (equals, greater or less than, contains, is empty, combined), see a whole row, read the column definitions (with indexes and constraints), see ready code for the table (**API**: Python, TypeScript, curl and the Data API), or download the page as CSV. With an admin key: **New table** (a designer that shows the exact SQL before it runs: columns, types, defaults, keys, unique, references), insert, edit and delete rows (tables with a primary key; a delete asks twice), add a column, and delete a table (type its name). Read-only, and filter values are sent as query parameters. **SQL**: the tenant's tables (click one to look inside) and an editor, read-only unless you untick it; a result with a label (or a date) and a number is also drawn as a chart (pick the number column), and any result downloads as CSV; save queries for the tenant by name, and rerun recent ones from this browser's history. **Cypher**: queries on a collection's graph, with nodes and relationships drawn as a graph (click a node for its properties) and the rows below. **Vectors**: every point of a collection with its payload, its vector and its nearest neighbours; filter the points by payload fields (fields and values are suggested from a sample; document metadata is under `meta.`), and "Find similar" then searches within the filters. |
| **Evaluation** | Golden sets of questions, runs comparing configurations, recall over time. |
| **Doctor** | Compare the vector store and the graph with the catalog, and queue repairs. |
| **Advisors** | Security and performance checks, each with what to do: an open API, exposed engine ports, plain HTTP on a named host, no scheduled backups, keys unused for 30 days, documents everyone may read, failed documents, changes being retried, drift, tables without a primary key, foreign keys without an index, large tables scanned without one. A tenant's key gets the tenant's checks; a key from the environment also gets the server's. |
| **Logs** | The last 5,000 API requests on this server (time, status, route, duration, tenant, key id), filterable by status, route and duration, with a live mode; and **query performance**: calls, errors, mean, p95, max and total time per route. Only routes and timings are kept, never bodies, parameters or key values. **Database queries**: the SQL statements that took the most time, from Postgres's own statistics (values show as `$1`, so no data appears), with calls, mean, max, rows, cache hits and share of the time. |
| **Users** | Your app's end users: create them (with groups), disable, set a new password or delete; open or close sign-up, set how long a sign-in lasts, and sign everyone out by rotating the signing secret. See [Auth](../reference/api.md#end-users-auth). |
| **Policies** | Which tables and functions signed-in users reach. Expose a table, then add policies from templates (users see and change their own rows, everyone signed in reads, users add rows as themselves, a group reads) or write your own `USING` and `WITH CHECK`; a table exposed with no policy shows users nothing. |
| **API keys** | Make a key for a tenant (shown once), see when each was last used, revoke it in two clicks (every server refuses it at once). |
| **Backups** | Take a snapshot or a backup now; download, restore or delete snapshots; restore copies from S3. Needs the admin key from the environment. |

![Overview](../img/console-overview.jpg)

**Search** (Ctrl+K) jumps to any page, collection, table, saved query or document by name.

In Collections, a document's panel downloads its **original file** as it was uploaded (always as a download, never shown as a page).

**Connect** (in the sidebar) shows the server's address and ready-to-paste code for Python, TypeScript, curl, an MCP
client and LangChain, for the current tenant. It never shows a stored key.

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
