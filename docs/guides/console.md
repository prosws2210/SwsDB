# Web console

swsdb serves a console at <http://localhost:6464/console>. It talks only to its own server, renders every value as text,
and runs under a strict Content-Security-Policy (no inline code, nothing loaded from elsewhere).

| Page | What you do there |
|---|---|
| **Overview** | Engine health and latency, documents, vectors and graph size, the sync backlog, collections by status. |
| **Collections** | Documents with their status, version, passages and entities. Upload files or paste text. Click a document to see why it is (or isn't) searchable: catalog, vectors and graph, with pending changes. |
| **Search** | **Ask** for a cited answer (click a citation to jump to its passage) or **Search** for passages alone. Pick hybrid, meaning or keywords, the graph depth, and who to ask as. |
| **Graph** | Entities by mentions, their relations and co-mentions as a radial map, and the documents that mention them. |
| **Evaluation** | Golden sets of questions, runs comparing configurations, recall over time. |
| **Doctor** | Compare Qdrant and the graph with the catalog, and queue repairs. |
| **Backups** | Take a snapshot or a backup now; download, restore or delete snapshots; restore copies from S3. Needs the admin key from the environment. |

![Overview](../img/console-overview.jpg)

## Keys and tenants

**API key** in the sidebar stores a key for this browser tab only (session storage). A tenant's key switches the
console to its tenant and locks the tenant field; a key from the environment can switch tenants freely. The theme
(dark or light) is remembered per browser.

If the server stops answering, the console shows an offline page and reconnects by itself when it's back.
