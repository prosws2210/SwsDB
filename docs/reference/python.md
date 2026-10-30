# Python SDK

```bash
pip install swsdb            # the client and CLI (only needs httpx)
pip install "swsdb[mcp]"     # plus the MCP server
```

```python
from swsdb import SwsDB, SwsDBError   # or AsyncSwsDB: the same methods, awaitable

db = SwsDB(url="http://localhost:6464", api_key="...", tenant="acme", timeout=60)  # all optional (env defaults)
```

Every method raises `SwsDBError(status, code, message)` on failure (`status` 0 when the server is unreachable).

## Server

| Method | Returns |
|---|---|
| `db.ready()` | `True` when `/readyz` is 200 |
| `db.status()` | engines, sync backlog, role, tenant |
| `db.collections()` | the tenant's collections |
| `db.create_collection(name, documents=True)` | a `Collection`; or `vector_size=384, distance="cosine"` for raw vectors; `graph=True/False` |
| `db.collection(name)` | a handle (no request) |
| `db.delete_collection(name)` | |
| `db.sql(query, params=None, read_only=False, timeout_s=30, max_rows=1000)` | `{"columns", "rows", "row_count", "truncated"}` |
| `db.reconcile(repair=False, all_tenants=False)` | the consistency report |
| `db.create_webhook(url, events=None, collections=None, secret=None)` / `db.webhooks()` / `db.webhook_deliveries(id)` / `db.ping_webhook(id)` / `db.delete_webhook(id)` | webhooks |
| `db.tenants()` | tenants with their collections and counts |
| `db.create_key(role="read", name=None, tenant=None)` / `db.keys()` / `db.revoke_key(id)` | managed keys |
| `db.create_snapshot(name=None)` / `db.snapshots()` / `db.download_snapshot(snapshot_id, path)` | snapshots |
| `db.upload_snapshot(path, snapshot_id=None)` / `db.restore_snapshot(snapshot_id, backup_first=True)` / `db.delete_snapshot(snapshot_id)` | |

## Documents collections

| Method | Returns |
|---|---|
| `col.add_text(id, text, title=None, metadata=None, readers=None, format="markdown")` | the catalog entry (`status` `queued`, or `unchanged: true`) |
| `col.upload(path, id=None, title=None, metadata=None, readers=None)` | the catalog entry |
| `col.wait_ready(id, timeout=600)` | the entry once searchable; raises if processing failed |
| `col.query(query, k=10, mode="hybrid", where=None, principals=None, graph_hops=None, graph_k=5)` | hits |
| `col.answer(question, k=5, mode="hybrid", where=None, principals=None, graph_hops=None, generate=True)` | `{"answer", "cited", "passages", "mode", "model"}` |
| `col.text(id, principals=None)` | `{"text", "chunks", ...}` |
| `col.document(id)` / `col.documents(limit, offset, status)` / `col.delete_document(id)` | |
| `col.diagnose(id)` | verdict and reasons |
| `col.put_eval_set(name, cases)` / `col.evaluate(name, k=5, configs=None, principals=None)` | evaluation |
| `col.info()` / `col.drop()` | |

## Raw vectors and graphs

| Method | |
|---|---|
| `col.upsert(points, batch_size=500)` | `[{"id", "vector", "payload"}]`, batched |
| `col.search(vector, limit=10, where=None, with_payload=True)` | hits with your ids and payloads |
| `col.get(ids)` / `col.delete(ids)` | |
| `col.cypher(query, params=None, read_only=False, timeout_s=30)` | `{"columns", "rows", "stats"}` |
