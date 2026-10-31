# REST API

Every server documents its own API interactively at `/docs` (OpenAPI at `/openapi.json`). This page is the map.

**Conventions.** Send the key as `api-key: <key>` (or `Authorization: Bearer <key>`) and the tenant as
`swsdb-tenant: <tenant>` (optional: the key's tenant, else `default`). Errors are always
`{"error": "<code>", "message": "<what to do>"}` with codes `bad_request` (400), `unauthorized` (401), `forbidden` (403),
`not_found` (404), `conflict` (409), `invalid` (422) and `unavailable` (503). Names of tenants, collections and
evaluation sets are 1 to 63 lowercase letters, digits, `-` or `_`.

## Health

| | |
|---|---|
| `GET /healthz` | the process is alive |
| `GET /readyz` | 200 when ready to serve and accept documents, else 503 with the reason |
| `GET /v1/status` | engines (state, version, latency), sync backlog, caller's role and tenant |
| `GET /metrics` | Prometheus (environment admin key when keys are set) |

## Collections

| | |
|---|---|
| `GET /v1/collections` | the tenant's collections |
| `PUT /v1/collections/{name}` | create: `{"documents": true}`, `{"vector": {"size": 384, "distance": "cosine"}}` and/or `{"graph": true}` |
| `GET /v1/collections/{name}` | vector, graph and document statistics (documents by status) |
| `DELETE /v1/collections/{name}` | drop it from every store |

## Documents (documents collections)

| | |
|---|---|
| `POST /v1/collections/{name}/documents` | upload a file (multipart: `file`, optional `id`, `title`, `metadata` and `readers` as JSON) |
| `POST /v1/collections/{name}/documents/text` | `{"id", "text", "format": "markdown"/"text"/"html", "title", "metadata", "readers"}` |
| `GET /v1/collections/{name}/documents` | a page of documents and the total, admin: `limit`, `offset`, `status`, `q` (part of the id or title), `meta` (a JSON object of metadata fields), `sort` (created, updated, id or chunks) |
| `GET /v1/collections/{name}/documents/{id}` | one document's catalog entry, admin |
| `DELETE /v1/collections/{name}/documents/{id}` | delete everywhere (queries stop returning it at once) |
| `POST /v1/collections/{name}/query` | `{"query", "k", "mode": "hybrid"/"dense"/"sparse", "where", "principals", "graph_hops", "graph_k"}` → `{"hits", "graph", "took_ms"}` |
| `POST /v1/collections/{name}/answer` | `{"question", "k", "mode", "where", "principals", "graph_hops", "graph_k", "generate"}` → `{"answer", "cited", "passages", "mode", "model"}` |
| `GET /v1/collections/{name}/text/{id}` | the searched text of a document the caller may read (`?principals=a,b` with the admin key) |
| `GET /v1/collections/{name}/diagnose/{id}` | why it is or isn't searchable, admin |

Document ids may contain `/`; encode them in the path (`a%2Fb`).

## Evaluation

| | |
|---|---|
| `PUT /v1/collections/{name}/eval/sets/{set}` | `{"cases": [{"query", "relevant": [ids], "answer"}]}` |
| `GET /v1/collections/{name}/eval/sets` | sets |
| `POST /v1/collections/{name}/eval/runs` | `{"set", "k", "configs": [{"name", "mode", "graph_hops", "graph_k", "where"}], "principals"}` |
| `GET /v1/collections/{name}/eval/runs` | stored runs, newest first |

## Raw vectors and graphs

| | |
|---|---|
| `PUT /v1/collections/{name}/points` | `{"points": [{"id", "vector", "payload"}]}` (up to 1000) |
| `POST /v1/collections/{name}/points/get` | `{"ids": [...]}` |
| `GET /v1/collections/{name}/facets` | documents by status, by metadata field and value, and by day added; `?meta=` narrows the counts |
| `GET /v1/activity` | searches and documents indexed per minute on this server over the last hour (`?minutes=`), and search latency; a tenant's key sees its tenant |
| `POST /v1/collections/{name}/points/scroll` | `{"limit": 50, "offset": null, "where": {...}, "with_vector": false}`: a page of points in id order, and `next` for the following page |
| `POST /v1/collections/{name}/points/delete` | `{"ids": [...]}` |
| `POST /v1/collections/{name}/search` | `{"vector", "limit", "where", "with_payload"}` |
| `POST /v1/collections/{name}/cypher` | `{"query", "params", "read_only", "timeout_s"}` (`LOAD CSV` is refused) |

On documents collections, search, points/get, points/scroll and Cypher need the admin key (they would skip document readers).

## SQL

`POST /v1/sql` with `{"query", "params", "read_only", "timeout_s", "max_rows"}`: one statement, in the tenant's own
schema. `params` are `%s` (a list) or `%(name)s` (an object) placeholders.

## Administration

| | |
|---|---|
| `POST /v1/admin/reconcile` | `{"dry_run": true, "all_tenants": false}` compare stores with the catalog; repairs with `dry_run: false` |
| `POST /v1/keys` | `{"role": "read"/"admin", "name", "tenant"}` → the key, once |
| `GET /v1/keys` | keys of the tenant (`?all_tenants=true` with an environment key) |
| `DELETE /v1/keys/{id}` | revoke |
| `POST /v1/admin/snapshots` | `{"name"}` take a snapshot (environment admin key) |
| `GET /v1/admin/snapshots` | list |
| `GET /v1/admin/snapshots/{id}` | its manifest |
| `GET /v1/admin/snapshots/{id}/download` | the tar file |
| `PUT /v1/admin/snapshots/{id}` | upload a tar file (the request body) |
| `POST /v1/admin/snapshots/{id}/restore` | `{"backup_first": true}` replace all data with it |
| `DELETE /v1/admin/snapshots/{id}` | delete |

## Webhooks

| | |
|---|---|
| `POST /v1/webhooks` | `{"url", "events", "collections", "secret"}` → the webhook and its secret, once |
| `GET /v1/webhooks` | the tenant's webhooks with delivered, pending and failed counts |
| `GET /v1/webhooks/{id}/deliveries` | recent deliveries |
| `POST /v1/webhooks/{id}/ping` | send a `ping` event |
| `DELETE /v1/webhooks/{id}` | remove |

See [Webhooks](../guides/webhooks.md). `GET /v1/admin/tenants` (environment admin key) lists every tenant with its
collections, documents, keys and SQL role.

## MCP

`/mcp` (read-only tools) and `/mcp/rw` (read-write, admin key) speak MCP over streamable HTTP. See [MCP server](../guides/mcp.md).
