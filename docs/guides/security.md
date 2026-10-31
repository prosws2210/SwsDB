# Security and tenants

## API keys

| Key | Set by | Reaches | Can |
|---|---|---|---|
| admin | `SWSDB_API_KEY` | every tenant | everything, including snapshots, `/metrics` and managing keys |
| read-only | `SWSDB_READ_ONLY_API_KEY` | every tenant | read and search; documents readable by everyone only |
| tenant admin | `swsdb keys create --role admin` | one tenant | everything inside its tenant, including its keys |
| tenant read | `swsdb keys create --role read` | one tenant | read and search inside its tenant |

Send a key as the `api-key` header or `Authorization: Bearer <key>`. Keys from the environment can also come from files,
which keeps them out of `docker inspect`:

```bash
docker run ... -e SWSDB_API_KEY_FILE=/run/secrets/admin -v ./secrets:/run/secrets:ro ghcr.io/prosws2210/swsdb
```

The same `_FILE` form works for `SWSDB_READ_ONLY_API_KEY`, `SWSDB_LLM_API_KEY` and `SWSDB_EMBEDDINGS_API_KEY`.

### Managed keys

```bash
export SWSDB_API_KEY=...                                              # an environment admin key
swsdb --tenant acme keys create --role read --name "acme support bot"  # prints the key once
swsdb keys list --all-tenants
swsdb keys revoke 3f9a1c0b2e
```

Managed keys look like `swsdb_<id>_<secret>`. swsdb stores only a SHA-256 of the key and compares in constant time;
revoking takes effect on the next request. Creating the first managed key needs `SWSDB_API_KEY` to be set, so that
someone always holds a key that reaches every tenant.

### No keys at all

With no key configured, every request is a global admin. This is meant for a laptop. Bootstrap prints a warning, and
two checks protect a local swsdb from web pages you visit:

- **DNS rebinding.** The `Host` header must be `localhost`, an IP address, the container's hostname, or a name in
  `SWSDB_ALLOWED_HOSTS`.
- **Cross-site requests**, with or without keys. A request whose `Origin` is another site is refused. Only swsdb's own
  console (same origin) and non-browser clients get through. List other sites in `SWSDB_ALLOWED_HOSTS` if a web app
  must call swsdb from the browser.

## Tenants

A tenant is a namespace for collections, documents, graphs, evaluation sets, keys and SQL. Every request acts for one
tenant: the `swsdb-tenant` header, else the key's own tenant, else `default`. A tenant's key asking for another
tenant gets 403.

| Store | Isolation |
|---|---|
| Qdrant | one collection per swsdb collection, named `sw_<tenant>__<name>` |
| FalkorDB | one graph per swsdb collection (a Cypher query can't address another graph) |
| Catalog | every query filtered by tenant |
| SQL pass-through | **one Postgres role and schema per tenant** (`swsdb_t_<tenant>`). The default tenant keeps `swsdb_app` and `public`. |

### The SQL sandbox

`POST /v1/sql` runs one statement as the tenant's role, in its own transaction with a timeout:

- It is not a superuser and can't create roles, databases or extensions, or switch to another role.
- It can't read swsdb's catalog (schema `swsdb`), another tenant's schema, or `public`. Its own schema is not readable
  by other roles.
- Several statements in one call are rejected, so `COMMIT; ...` can't escape read-only mode or the timeout.
- Settings and temporary tables don't survive into the next call.
- `pg_read_file`, `COPY ... PROGRAM` and friends need privileges it doesn't have.

!!! warning "Known limit: names, not contents"
    Postgres shows every role the *names* of tables and schemas in `pg_catalog`. A tenant can't read another tenant's
    tables, but could list their names. Don't put secrets in table names. (`information_schema` hides objects a role
    can't access.)

### Cypher pass-through

Queries reach only the collection's own graph, and `read_only` queries run as `GRAPH.RO_QUERY`. **`LOAD CSV` is
refused**: it would make the graph engine fetch any URL it can reach, including cloud metadata endpoints. Parameters
are sent as typed values, never spliced into the query text.

## Document permissions

Readers are enforced inside Qdrant's search, again for every graph passage, and by `answer`, `text` and the MCP tools.
Raw access to a documents collection (vector search, point reads, Cypher) would skip that filter, so it needs an admin
key. See [Documents and retrieval](documents.md#permissions).

## Project connections

A project's own code can connect to Postgres with a connection string and use the Qdrant client
([Start a new project](projects.md)). Neither opens anything beyond the project:

- **Postgres.** By default Postgres listens beyond localhost inside the container, but `pg_hba.conf` admits only
  members of `swsdb_projects` from the network, and only with SCRAM passwords. Members are the per-project logins
  `swsdb_t_<tenant>__c`. The superuser, `swsdb_app` and tenants' own roles can't log in from outside.
- **What a login is.** A login has no rights of its own (`NOINHERIT`). Every session starts as the project's role, so
  it has exactly the rights the project's SQL has, in its schema, and whatever it creates belongs to the project.
  `SET ROLE NONE` leaves a role that can do nothing.
- **A project's own databases.** Once a project has a login, its role may create databases (`CREATEDB`), for Django's
  test database and Prisma's shadow database. They belong to the project. Another project's login can connect to one,
  since Postgres grants `CONNECT` to everyone, but it can't read its tables, create anything in it, drop it, rename it
  or copy it as a template. The HTTP SQL API can't create databases, because every `/v1/sql` statement runs in a
  transaction. Revoking the login takes `CREATEDB` away again.
- **Passwords.** A password is generated, shown once, and stored by Postgres only as a SCRAM verifier. swsdb sends the
  verifier, never the password, so it can't show up in statement statistics or logs. A new password works for new
  connections only; revoking drops the login and ends its sessions. Each login allows 20 connections. swsdb's catalog
  keeps the verifier, so a snapshot restored on another server brings the login back with the same password.
- **Qdrant.** `/qdrant` is a gate in front of Qdrant, not an open port. It checks the swsdb key and rewrites
  collection names, both in the path and in bodies that name a second collection. It refuses `init_from`, snapshots
  (a snapshot recovery would make Qdrant fetch a URL), shards, aliases and cluster calls. Qdrant's own port and key
  stay inside the container.
- **Turning it off.** `SWSDB_POSTGRES_ACCESS=false` keeps Postgres on localhost.

Directly connected code skips the per-call timeout, the one-statement rule and the read-only option of `/v1/sql`.
Those limits protect the HTTP API, while a project connection is the project's own database. It is still confined to
the project's schema and to Postgres's privileges, as listed above.

## End users

An app's users sign in with email and password ([Auth](../reference/api.md#end-users-auth)) and send an access token
instead of a key. What a token can do:

- **Search and answers** use the user's own principals (`user:<id>` and their groups). A token can't pass other
  principals, so it can't ask as someone else.
- **Tables** only through the Data API, as the tenant's users role (`swsdb_t_<tenant>__u`, no inherited rights), and
  only tables an admin exposed. Postgres row-level security decides the rows. Users never send SQL; their id, email
  and groups reach the policies as settings swsdb sets itself, from the verified token.
- **Functions** only those an admin let users call. Edge-style code isn't run: a function is plain SQL or PL/pgSQL
  in the tenant's schema, under the same role.
- **Files** only in their own folder of each bucket, plus reading public buckets. Public is every key and user of the
  tenant, never anonymous visitors.
- **Nothing else.** Routes for keys (collection admin, SQL, Cypher, keys, backups, logs) answer 403 to a token.

Passwords are hashed with scrypt, and failed sign-ins are rate-limited. Tokens are HS256 JWTs signed with a secret
per tenant. Disabling or deleting a user stops their tokens at once, and rotating the secret signs everyone out. Sign-up
is closed until an admin opens it.

## Network and TLS

The engines' own ports (5432, 6333, 6379) listen on localhost inside the container unless you set
`SWSDB_EXPOSE_NATIVE=true`. Each volume gets its own random engine passwords (`swsdb-admin credentials` shows them).

To serve HTTPS directly, mount a certificate and its key:

```bash
docker run -d -p 6464:6464 -v ./tls:/tls:ro \
  -e SWSDB_TLS_CERT_FILE=/tls/cert.pem -e SWSDB_TLS_KEY_FILE=/tls/key.pem ghcr.io/prosws2210/swsdb
```

With TLS on, plain HTTP isn't served, and the health check and `swsdb-admin` follow automatically. Terminating TLS at a
reverse proxy works too; keep port 6464 private to the proxy.

## What the security review covered (v0.1)

Each item below is a test in `tests/image/test_security.py` or `tests/unit/test_hardening.py`, run in CI.

| Finding | Fix |
|---|---|
| Pass-through SQL shared one schema (`public`) across tenants | one role and schema per tenant; `public` closed to other roles |
| A read-only key could read restricted documents through raw vector search, point reads and Cypher | admin-only on documents collections |
| Cypher `LOAD CSV` could fetch arbitrary URLs (SSRF), even in read-only queries | refused, however it's written (comments, case, line breaks) |
| With no keys, a web page could reach a local swsdb (DNS rebinding, cross-site form posts) | `Host` and `Origin` checks |
| Keys reached every tenant | managed keys bound to one tenant, stored hashed |
| Secrets only through environment variables (visible in `docker inspect`) | `*_FILE` variants |
| No TLS without a proxy | `SWSDB_TLS_CERT_FILE` / `SWSDB_TLS_KEY_FILE` |
| The Python SDK didn't URL-encode document ids (`a/b`, `..`, `?`) | encoded; ids never become file paths on the server |

The tests check every read path for leaks between two tenants with look-alike data: search in all modes with graph
expansion, answers, document text, listings, diagnosis, evaluation, raw vectors, Cypher, SQL, reconciliation and all
MCP tools. They also check that no secret appears in logs, responses or `docker inspect`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on the repository (Security → Report a vulnerability). See
[SECURITY.md](https://github.com/prosws2210/SwsDB/blob/main/SECURITY.md).
