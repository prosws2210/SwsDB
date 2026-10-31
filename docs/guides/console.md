# Web console

swsdb serves a console at <http://localhost:6464/console>. It talks only to its own server, renders every value as text,
and runs under a strict Content-Security-Policy (no inline code, nothing loaded from elsewhere).

The sidebar groups the pages. **Databases** has a page for each kind of data, named with the engine that holds it
(a page whose engine isn't running is hidden); everything that belongs to one engine is a tab of its page, so Reports
(saved SQL as charts) and Policies (row-level security) are tabs of Tables. Then **Documents** (collections, search,
evaluation), **App** (your app's users and files), **Operations** (doctor, advisors, logs) and **Admin** (keys,
backups). The cards on the Overview open their pages, and Ctrl+K reaches every page and tab.

| Page | What you do there |
|---|---|
| **Overview** | Engine health and latency; documents, vectors and graph size across every collection (document collections and the ones you fill yourself); the sync backlog; **activity** on this server over the last hour: searches and documents indexed per minute, and median and p95 search time. |
| **Tables** (PostgreSQL) | Tabular data, in four tabs. **Tables** opens on every table and view in the tenant's schema, with its columns, estimated rows and size, and a search. Click one for its own page, with **Rows**, **Structure** and **API** tabs: page through it as a grid (column types and keys in the header, NULLs marked), sort by a column, filter (equals, greater or less than, contains, is empty, combined), see a whole row, read the column definitions (with indexes and constraints), see ready code for the table (**API**: Python, TypeScript, curl and the Data API), or download the page as CSV. With an admin key: **New table** (a designer that shows the exact SQL before it runs: columns, types, defaults, keys, unique, references), insert, edit and delete rows (tables with a primary key; a delete asks twice), add a column, and delete a table (type its name). Read-only, and filter values are sent as query parameters. **Schema**: the tenant's tables as a diagram, foreign keys drawn to the columns they reference. **Database**: functions (with **Call**, which runs one with JSON arguments through `/v1/rpc`), triggers, enumerated types, views, indexes and the server's extensions. **SQL**: the tenant's tables (click one to look inside) and an editor, read-only unless you untick it; a result with a label (or a date) and a number is also drawn as a chart (pick the number column), and any result downloads as CSV; save queries for the tenant by name, and rerun recent ones from this browser's history.
| **Tables > Reports** | Saved SQL queries pinned (☆ in the SQL tab) as live charts, run fresh each time the page opens. |
| **Tables > Policies** | Which tables and functions signed-in users reach. Expose a table, then add policies from templates (users see and change their own rows, everyone signed in reads, users add rows as themselves, a group reads) or write your own `USING` and `WITH CHECK`; a table exposed with no policy shows users nothing. |
| **Vectors** (Qdrant; pgvector in the lite profile) | Vector data, laid out like Qdrant's own dashboard. It opens on every collection, with its status, points, segments and vectors (named, sparse, multivectors). Click one to see its points, with these tabs:<br>- **Points**: the points as cards, each with its payload as a JSON tree and its vectors, with "Find similar" by any of them. Narrow them with **Similar to (id)** and **Filter by payload** (`category:kitchen in_stock:true`, or `id:123`).<br>- **Info**: the vectors' configuration, the HNSW index, the optimizer and the raw configuration.<br>- **Indexes**: payload indexes, which you can create and drop with the admin key.<br>- **Memory**: disk, RAM and cache for each vector, payload and payload index.<br>- **Segments**: the collection's segments and any optimizations running.<br>- **Index quality**: recall@k of the approximate index against an exact scan, on random points.<br>- **Map**: up to 3,000 points projected to 2D (PCA) and colored by a payload field.<br>- **Qdrant API**: Qdrant's own REST requests through `/qdrant`, limited to the project.<br>The lite profile (pgvector) has Points, Info, Memory (table and index sizes) and Map; the other tabs are Qdrant's. |
| **Graph** (FalkorDB; Apache AGE in the lite profile) | Graph data, laid out like FalkorDB's own browser. **Browser**: on the left, the graph's memory, its node labels and relationship types with counts (click one to draw those nodes or relationships) and its property keys. At the top, a Cypher bar with recent queries (read-only unless you untick it, with the admin key). Below, three views: **Graph** draws the result; click a node or a relationship to light up its neighbours and see its labels and properties, double-click a node to add its neighbours, search for an element, and wheel to zoom or drag to pan. **Table** shows the rows and downloads them as CSV. **Schema** shows which labels connect to which, with counts. **Entities**: what documents mention: Entities by mentions (with how many there are in all), their relations and co-mentions as a radial map, and the documents that mention them. |
| **Collections** | Documents with their status, version, passages, entities and their most common metadata fields, 100 per page, searchable by id or title and sortable. **What's inside** counts the documents by metadata value (and by day added); click a value to filter the list, and the counts narrow to the filtered documents. Upload files or paste text. Click a document to see why it is (or isn't) searchable: catalog, vectors and graph, with pending changes. |
| **Search** | **Ask** for a cited answer (click a citation to jump to its passage) or **Search** for passages alone. Pick hybrid, meaning or keywords, the graph depth, who to ask as, and **filters** on document metadata (picked from the values the collection holds). Every passage shows its document's metadata. |
| **Evaluation** | Golden sets of questions, runs comparing configurations, recall over time. |
| **Users** | Your app's end users: create them (with groups), disable, set a new password or delete; open or close sign-up, set how long a sign-in lasts, and sign everyone out by rotating the signing secret. See [Auth](../reference/api.md#end-users-auth). |
| **Storage** | Buckets (private or public to the tenant) with their files and size; upload into a folder, filter by folder, download and delete files. |
| **Doctor** | Compare the vector store and the graph with the catalog, and queue repairs. |
| **Advisors** | Security and performance checks, each with what to do: an open API, exposed engine ports, plain HTTP on a named host, no scheduled backups, keys unused for 30 days, documents everyone may read, failed documents, changes being retried, drift, tables without a primary key, foreign keys without an index, large tables scanned without one. A tenant's key gets the tenant's checks; a key from the environment also gets the server's. |
| **Logs** | The last 5,000 API requests on this server (time, status, route, duration, tenant, key id), filterable by status, route and duration, with a live mode; and **query performance**: calls, errors, mean, p95, max and total time per route. Only routes and timings are kept, never bodies, parameters or key values. **Database queries**: the SQL statements that took the most time, from Postgres's own statistics (values show as `$1`, so no data appears), with calls, mean, max, rows, cache hits and share of the time. |
| **API keys** | Make a key for a tenant (shown once), see when each was last used, revoke it in two clicks (every server refuses it at once). |
| **Backups** | Take a snapshot or a backup now; download, restore or delete snapshots; restore copies from S3. Needs the admin key from the environment. |

![Overview](../img/console-overview.jpg)

**Search** (Ctrl+K) jumps to any page, collection, table, saved query or document by name.

In Collections, a document's panel downloads its **original file** as it was uploaded (always as a download, never shown as a page).

**Connect** (in the sidebar) shows the server's address and ready-to-paste code for Python, TypeScript, curl, an MCP
client, PostgreSQL tools (SQLAlchemy, Django, Prisma, psql), the Qdrant client and LangChain, for the current tenant.
With the project's admin key it also makes the project's **Postgres connection string** (the password is shown once;
**New password** replaces it, **Revoke** removes the login). It never shows a stored key.

The Tables, Vectors and Graph pages go through the API like everything else: your key's tenant and role apply, SQL runs as the tenant's
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
