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
| `GET /v1/sql/saved`, `PUT /v1/sql/saved/{name}`, `DELETE /v1/sql/saved/{name}` | SQL queries saved for the tenant (`{"query": "...", "pinned": true}`; `pinned` shows it on Reports, and leaving it out keeps it); saving and deleting need the admin key |
| `GET /v1/collections/{name}/original/{id}` | the document's file as uploaded (live version), as a download (`application/octet-stream`), admin |
| `GET /v1/logs` | recent API requests on this server, newest first, and time per route (`?status=4xx&route=query&min_ms=100&limit=200`), admin; a tenant's key sees its tenant |
| `GET /v1/advisors` | security and performance findings (`level`, `title`, `detail`, `fix`, `items`), admin; server-wide checks need a key from the environment |
| `GET /v1/activity` | searches and documents indexed per minute on this server over the last hour (`?minutes=`), and search latency; a tenant's key sees its tenant |
| `POST /v1/collections/{name}/points/scroll` | `{"limit": 50, "offset": null, "where": {...}, "with_vector": false}`: a page of points in id order, and `next` for the following page |
| `POST /v1/collections/{name}/points/delete` | `{"ids": [...]}` |
| `POST /v1/collections/{name}/search` | `{"vector", "limit", "where", "with_payload"}` |
| `GET /v1/collections/{name}/inspect` | its configuration: vectors (named, sparse, multivector), payload indexes, HNSW, optimizer, segments, status |
| `GET /v1/collections/{name}/memory` | disk, RAM and cache per vector, sparse vector, payload and payload index (lite: table and index sizes) |
| `GET /v1/collections/{name}/segments` | segments and optimizations (Qdrant) |
| `POST /v1/collections/{name}/similar` | `{"like": <point id> or "vector": [...], "using": "<vector name>", "limit", "where"}`: nearest neighbours by any named vector, including sparse and multivectors; the point itself is left out |
| `POST /v1/collections/{name}/recall` | `{"samples": 50, "k": 10, "using": ""}`: recall@k of the approximate index against an exact scan, on random points (Qdrant) |
| `POST /v1/collections/{name}/map` | `{"limit": 1000, "using": "", "color_by": "field.path", "where"}`: points projected to 2D (PCA), each with the field's value |
| `PUT /v1/collections/{name}/index`, `DELETE /v1/collections/{name}/index/{field}` | `{"field", "type": "keyword"/"integer"/"float"/"bool"/"geo"/"datetime"/"text"/"uuid"}` payload indexes, admin, not on documents collections (Qdrant) |
| `GET /v1/collections/{name}/graph` | what the graph holds: `nodes`, `edges`, `labels` and `relationships` with counts, `property_keys`, `schema` (which labels connect to which, by type, with counts) and `memory_bytes` (FalkorDB); not with Neo4j |
| `POST /v1/collections/{name}/cypher` | `{"query", "params", "read_only", "timeout_s"}` (`LOAD CSV` is refused) |

On documents collections, search, points/get, points/scroll and Cypher need the admin key (they would skip document readers).

## SQL

`POST /v1/sql` with `{"query", "params", "read_only", "timeout_s", "max_rows"}`: one statement, in the tenant's own
schema. `params` are `%s` (a list) or `%(name)s` (an object) placeholders.

`GET /v1/query-stats` (admin) lists the SQL statements that took the most time, from Postgres's `pg_stat_statements`
(values show as `$1`, never the data): `?order=total|mean|calls|rows&limit=50`. A tenant's key sees its own roles; a
key from the environment sees every role, swsdb's own work included, and may `POST /v1/query-stats/reset`.

## Project connections

Use one swsdb in place of a project's own database containers (see [Start a new project](../guides/projects.md)).

| | |
|---|---|
| `GET /v1/postgres` | the project's Postgres login: `user`, `database`, `schema`, `host`, `port`, `issued`, open `sessions`, the `databases` the project created, and the URL with `PASSWORD` in place of the password, admin |
| `POST /v1/postgres/credentials` | a new password and the full connection string (`url`, and `prisma_url` with `?schema=` for Prisma), shown once; creates the login on first use, admin |
| `DELETE /v1/postgres/credentials` | remove the login and end its open sessions (the data stays), admin |
| `/qdrant/...` | Qdrant's REST API for the project's own collections, for the standard Qdrant client (`prefix="qdrant"`, the swsdb key as its API key) |

Through `/qdrant`, collection names are the project's own, and the collection is a swsdb raw vector collection. Only
`/collections` and calls on one collection are offered, without snapshots, shards or aliases. `init_from` is refused,
and `lookup_from` / `with_lookup` resolve inside the project. Writes need the admin key, documents collections are
read-only (and admin only), end-user tokens are refused, and the lite profile answers 409.

## End users (Auth)

Your app's users sign in with email and password and send the access token as `Authorization: Bearer <token>`. A
request with a token acts as that user: search and answers see the documents the user may read (readers
`user:<id>`, `group:<name>` and `*`; passing `principals` is refused), tables and functions are reached only through
the Data API under the table's policies, and files only in the user's own Storage folder. Users never send SQL, so
they can't change who they are. Key-only routes (collections admin, SQL, keys, backups...) answer 403 to a token.

| | |
|---|---|
| `POST /v1/auth/signup` | `{"email", "password", "tenant"}` → an access token; only when sign-up is open; no key needed |
| `POST /v1/auth/token` | `{"email", "password", "tenant"}` → `{"access_token", "token_type", "expires_in", "user"}`; no key needed; 10 failures in 5 minutes give 429 |
| `GET /v1/auth/user` | who the token belongs to: id, email, groups and principals |
| `GET /v1/auth/users` | the tenant's users (`?q=` part of an email, `limit`, `offset`), admin |
| `POST /v1/auth/users` | `{"email", "password", "groups", "metadata"}` create a user, admin |
| `PATCH /v1/auth/users/{id}` | `{"groups", "disabled", "password"}`, admin; a disabled or deleted user's tokens stop working at once |
| `DELETE /v1/auth/users/{id}` | admin |
| `GET`, `PUT /v1/auth/settings` | `{"signup": false, "token_ttl_s": 3600}` (60 seconds to 30 days), admin |
| `POST /v1/auth/settings/rotate` | a new signing secret: every token issued so far stops working, on every server, admin |

Passwords are hashed with scrypt; tokens are HS256 JWTs signed with a secret kept per tenant. `tenant` in the body (or
the `swsdb-tenant` header) picks the tenant; it is `default` otherwise.

## Data API and functions

| | |
|---|---|
| `GET /v1/rest/{table}` | rows: `?select=a,b&col=eq.value&order=col.desc&limit=100&offset=0&count=exact` |
| `POST /v1/rest/{table}` | insert an object or a list (up to 1000); returns the rows as stored |
| `PATCH /v1/rest/{table}?col=eq.value` | change the matching rows (a filter is required) |
| `DELETE /v1/rest/{table}?col=eq.value` | delete the matching rows (a filter is required) |
| `POST /v1/rpc/{function}` | call a SQL function with named arguments `{"arg": value}` |

Filters are `eq.`, `neq.`, `gt.`, `gte.`, `lt.`, `lte.`, `like.`, `ilike.`, `is.null` (or `true`, `false`),
`not.is.null` and `in.(a,b,c)`; several filters combine with AND. `limit` is at most 1000.

A key sees every row of the tenant's tables. A signed-in user reaches only tables and functions **exposed** to users,
as the tenant's users role, so Postgres row-level security decides which rows; policies read who is asking with
`swsdb_auth.uid()`, `swsdb_auth.email()` and `swsdb_auth.groups()`.

| | |
|---|---|
| `GET /v1/policies` | tables (row-level security on, exposed, policies) and functions users may call, admin |
| `PUT /v1/policies/tables/{table}` | `{"exposed": true}` expose a table to users (turns row-level security on), admin |
| `POST /v1/policies/tables/{table}` | `{"name", "command": "ALL"/"SELECT"/"INSERT"/"UPDATE"/"DELETE", "using", "check"}` add a policy, admin |
| `DELETE /v1/policies/tables/{table}/{policy}` | drop a policy, admin |
| `PUT /v1/policies/functions/{function}` | `{"exposed": true}` let users call a function, admin |

A table exposed with no policy shows users nothing.

## Storage

Files in buckets, per tenant, kept in Postgres (so snapshots, restores and several servers include them).

| | |
|---|---|
| `GET /v1/storage/buckets` | buckets with their file count and size |
| `POST /v1/storage/buckets` | `{"name", "public": false}`, admin |
| `PATCH /v1/storage/buckets/{bucket}` | `{"public": true}`, admin |
| `DELETE /v1/storage/buckets/{bucket}` | admin; a bucket holding files needs `?force=true` |
| `GET /v1/storage/{bucket}` | files (`?prefix=`) |
| `PUT /v1/storage/{bucket}/{path}` | upload the request body as the file (its Content-Type is kept; at most `SWSDB_MAX_UPLOAD_MB`) |
| `GET /v1/storage/{bucket}/{path}` | download |
| `DELETE /v1/storage/{bucket}/{path}` | delete |

The admin key does everything in its tenant, the read key lists and downloads. A signed-in user lists, uploads,
downloads and deletes in their own folder (`<user id>/...`) of any bucket, and lists and downloads everything in a
**public** bucket. Public means every key and user of the tenant, never anonymous visitors. Downloads come as
attachments with `nosniff` and a sandboxing Content-Security-Policy (common images are shown inline), so an uploaded
HTML or SVG file never runs as a page.

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
