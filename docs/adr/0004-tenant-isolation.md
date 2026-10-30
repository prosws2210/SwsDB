# ADR 0004: Tenants: keys bound to a tenant, and a Postgres role per tenant

Status: accepted · Date: 2026-10-31

## Context

Until v0.1 a tenant was a namespace chosen by the caller (the `swsdb-tenant` header) and every API key reached every
tenant. Two consequences surfaced in the security review:

- Isolation held only between data, not between callers. Anyone with a key could read any tenant by changing a header.
- Pass-through SQL ran as one sandbox role (`swsdb_app`) owning `public`, so every tenant's tables were shared.

Multi-tenant applications (a SaaS putting each customer in a tenant, or an agent per user) need a key that reaches one
tenant, and stores that enforce it.

## Decision

**Keys.** Keys from the environment stay global (admin or read-only). Managed keys are bound to one tenant and a role,
created through the API by an admin, shown once, and stored as SHA-256 (they are 256-bit random strings, so a slow hash
would add nothing). A request's tenant is the header, else the key's tenant, else `default`; a tenant key asking for
another tenant is refused before any handler runs. The first managed key needs `SWSDB_API_KEY`, so someone always holds
a key to every tenant and an open server can't be locked by creating a key.

**Vectors and graphs** were already one Qdrant collection and one FalkorDB graph per swsdb collection, named
`sw_<tenant>__<collection>`. Names can't contain `__`, so no tenant's names can reach another's.

**SQL.** Each tenant gets a login role `swsdb_t_<tenant>` (hashed when too long) and a schema of the same name. The role
is `NOINHERIT` and not a member of anything, so it can't `SET ROLE` elsewhere. Its search path is its schema, its
default function privileges are revoked from `PUBLIC`, and `public` is closed to every role but `swsdb_app` (the default
tenant's, kept for compatibility). The server logs in as these roles over the Unix socket through one `pg_ident`
regex rule (`/^swsdb_t_[a-z0-9_-]+$`), so no passwords exist for them. Each tenant has its own small connection pool;
the pool resets settings and temporary tables between statements.

Considered and rejected:

- *`SET ROLE` per transaction from one login role.* The statement could `RESET ROLE` or switch to another tenant's role,
  because the login role must be a member of all of them.
- *Row-level security in one schema.* Pass-through SQL lets tenants create their own tables, and RLS would have to be
  enforced on objects swsdb doesn't create.
- *A database per tenant.* Stronger catalog isolation, but connections are per database: one pool per tenant per
  database, and a snapshot of many databases.

## Consequences

- Postgres still shows every role the names of other schemas and tables in `pg_catalog` (not their contents).
  Documented as a known limit.
- Snapshots record the tenant roles and recreate them on restore, since roles live outside the database.
- Read-only keys can't use raw vector reads or Cypher on documents collections. Those skip document readers, so they
  need the admin key.
