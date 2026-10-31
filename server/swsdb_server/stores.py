"""Adapters over the engines. The API talks to these interfaces, never to an engine's client directly, so an engine can
be swapped (or moved out of the container) without touching the API."""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import redis.asyncio as aioredis
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .names import parse_engine_name, point_id

# ------------------------------------------------------------------ errors the API maps to status codes


class StoreError(Exception):
    """A request the engine rejected (bad query, wrong dimensions...): the caller's to fix (HTTP 400)."""


class NotFound(StoreError):
    """HTTP 404."""


class Conflict(StoreError):
    """HTTP 409."""


class Forbidden(StoreError):
    """HTTP 403: allowed for nobody through the API, whatever the key."""


class Unavailable(Exception):
    """The engine didn't answer (HTTP 503)."""


# ------------------------------------------------------------------ SQL (PostgreSQL)

# Pass-through SQL connects as this role: a plain login role (not a superuser), so a statement can't escalate back
# to swsdb's own privileges. It owns `public`; swsdb's tables live in schema `swsdb`, which it can't touch.
APP_ROLE = "swsdb_app"

MIGRATIONS: list[str] = [
    # 1: the sandbox role for user SQL (must match APP_ROLE)
    """
    REVOKE ALL ON SCHEMA swsdb FROM PUBLIC;
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'swsdb_app') THEN
        CREATE ROLE swsdb_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
      END IF;
    END $$;
    ALTER SCHEMA public OWNER TO swsdb_app;
    GRANT USAGE, CREATE ON SCHEMA public TO swsdb_app;
    """,
    # 2: the document catalog. Postgres is the source of truth: vectors and graph can be rebuilt from these rows.
    """
    CREATE TABLE swsdb.collections (
      tenant text NOT NULL, name text NOT NULL,
      embedding text NOT NULL, dim int NOT NULL, graph boolean NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant, name));
    CREATE TABLE swsdb.documents (
      id uuid PRIMARY KEY, tenant text NOT NULL, collection text NOT NULL, external_id text NOT NULL,
      title text, filename text, kind text, metadata jsonb NOT NULL DEFAULT '{}', readers text[] NOT NULL,
      version int NOT NULL DEFAULT 1, status text NOT NULL, error text,
      chunks int NOT NULL DEFAULT 0, entities int NOT NULL DEFAULT 0, bytes bigint, sha256 text,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant, collection) REFERENCES swsdb.collections ON DELETE CASCADE,
      UNIQUE (tenant, collection, external_id));
    CREATE INDEX documents_pending ON swsdb.documents (updated_at) WHERE status IN ('queued', 'processing');
    CREATE TABLE swsdb.chunks (
      id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES swsdb.documents ON DELETE CASCADE,
      version int NOT NULL, ordinal int NOT NULL, text text NOT NULL, page int, heading text, hash text NOT NULL,
      UNIQUE (document_id, version, ordinal));
    CREATE TABLE swsdb.chunk_entities (
      chunk_id uuid NOT NULL REFERENCES swsdb.chunks ON DELETE CASCADE,
      entity_key text NOT NULL, name text NOT NULL, type text NOT NULL, PRIMARY KEY (chunk_id, entity_key));
    CREATE TABLE swsdb.chunk_relations (
      chunk_id uuid NOT NULL REFERENCES swsdb.chunks ON DELETE CASCADE,
      source text NOT NULL, target text NOT NULL, type text NOT NULL, PRIMARY KEY (chunk_id, source, target, type));
    """,
    # 3: consistency. Every change is an outbox row written in the same transaction as the catalog change; workers
    # apply it to the derived stores (idempotently, with retries). live_version is the version queries may return.
    """
    ALTER TABLE swsdb.documents ADD COLUMN live_version int;
    UPDATE swsdb.documents SET live_version = version WHERE status = 'ready';
    ALTER TABLE swsdb.documents ADD COLUMN attempts int NOT NULL DEFAULT 0;
    ALTER TABLE swsdb.chunks ADD COLUMN extracted boolean NOT NULL DEFAULT false;
    UPDATE swsdb.chunks c SET extracted = true
      WHERE EXISTS (SELECT 1 FROM swsdb.chunk_entities e WHERE e.chunk_id = c.id);
    CREATE TABLE swsdb.outbox (
      id bigserial PRIMARY KEY,
      document_id uuid NOT NULL REFERENCES swsdb.documents ON DELETE CASCADE,
      op text NOT NULL CHECK (op IN ('index', 'delete', 'graph')),
      version int NOT NULL,
      attempts int NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      locked_until timestamptz,
      last_error text,
      created_at timestamptz NOT NULL DEFAULT now());
    CREATE INDEX outbox_due ON swsdb.outbox (next_attempt_at);
    CREATE INDEX outbox_document ON swsdb.outbox (document_id, id);
    INSERT INTO swsdb.outbox (document_id, op, version)
      SELECT id, 'index', version FROM swsdb.documents WHERE status IN ('queued', 'processing');
    """,
    # 4: retrieval evaluation: golden sets and stored runs
    """
    CREATE TABLE swsdb.eval_sets (
      id bigserial PRIMARY KEY, tenant text NOT NULL, collection text NOT NULL, name text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant, collection) REFERENCES swsdb.collections ON DELETE CASCADE,
      UNIQUE (tenant, collection, name));
    CREATE TABLE swsdb.eval_cases (
      set_id bigint NOT NULL REFERENCES swsdb.eval_sets ON DELETE CASCADE, ordinal int NOT NULL,
      query text NOT NULL, relevant text[] NOT NULL, answer text, PRIMARY KEY (set_id, ordinal));
    CREATE TABLE swsdb.eval_runs (
      id bigserial PRIMARY KEY, set_id bigint NOT NULL REFERENCES swsdb.eval_sets ON DELETE CASCADE,
      report jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
    """,
    # 5: tenant isolation. Managed API keys bound to one tenant (only a hash is kept), and one Postgres role and schema
    # per tenant for pass-through SQL. The default tenant keeps swsdb_app and `public`, which no other role may use.
    """
    CREATE TABLE swsdb.api_keys (
      id text PRIMARY KEY, hash bytea NOT NULL, tenant text NOT NULL,
      role text NOT NULL CHECK (role IN ('admin', 'read')), name text,
      created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz, revoked_at timestamptz);
    CREATE TABLE swsdb.sql_tenants (
      tenant text PRIMARY KEY, role text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now());
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    ALTER DEFAULT PRIVILEGES FOR ROLE swsdb_app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
    """,
    # 6: webhooks. Events are inserted in the transaction of the change they describe and delivered by a worker.
    """
    CREATE TABLE swsdb.webhooks (
      id text PRIMARY KEY, tenant text NOT NULL, url text NOT NULL, events text[] NOT NULL, collections text[],
      secret text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), disabled_at timestamptz);
    CREATE INDEX webhooks_tenant ON swsdb.webhooks (tenant);
    CREATE TABLE swsdb.webhook_events (
      id bigserial PRIMARY KEY, webhook_id text NOT NULL REFERENCES swsdb.webhooks ON DELETE CASCADE,
      event text NOT NULL, payload jsonb NOT NULL, attempts int NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(), locked_until timestamptz, delivered_at timestamptz,
      failed_at timestamptz, last_status int, last_error text, created_at timestamptz NOT NULL DEFAULT now());
    CREATE INDEX webhook_events_due ON swsdb.webhook_events (next_attempt_at)
      WHERE delivered_at IS NULL AND failed_at IS NULL;
    """,
    # 7: with an external Postgres the sandbox roles log in with passwords (no Unix socket to map OS users on)
    """
    CREATE TABLE swsdb.sql_role_secrets (role text PRIMARY KEY, password text NOT NULL);
    """,
    # 8: several servers on one database: who is running, who holds an outbox row, uploads kept in Postgres
    """
    CREATE TABLE swsdb.servers (
      id text PRIMARY KEY, version text NOT NULL, files text NOT NULL, profile text NOT NULL,
      started_at timestamptz NOT NULL DEFAULT now(), seen_at timestamptz NOT NULL DEFAULT now());
    ALTER TABLE swsdb.outbox ADD COLUMN locked_by text;
    CREATE TABLE swsdb.originals (
      document_id uuid NOT NULL REFERENCES swsdb.documents (id) ON DELETE CASCADE,
      version int NOT NULL, data bytea NOT NULL, PRIMARY KEY (document_id, version));
    """,
    # 9: SQL queries saved from the console, per tenant
    """
    CREATE TABLE swsdb.saved_queries (
      tenant text NOT NULL, name text NOT NULL, query text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant, name));
    """,
    # 10: saved queries pinned to the console's Reports page
    """
    ALTER TABLE swsdb.saved_queries ADD COLUMN pinned boolean NOT NULL DEFAULT false;
    """,
    # 11: a tenant's end users (Auth), its files (Storage), and the functions row-level security policies call
    """
    CREATE TABLE swsdb.auth_config (
      tenant text PRIMARY KEY, secret bytea NOT NULL, signup boolean NOT NULL DEFAULT false,
      token_ttl_s int NOT NULL DEFAULT 3600 CHECK (token_ttl_s BETWEEN 60 AND 2592000));
    CREATE TABLE swsdb.end_users (
      tenant text NOT NULL, id uuid NOT NULL, email text NOT NULL, password text NOT NULL,
      groups text[] NOT NULL DEFAULT '{}', metadata jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(), last_sign_in_at timestamptz, disabled_at timestamptz,
      PRIMARY KEY (tenant, id), UNIQUE (tenant, email));
    CREATE TABLE swsdb.storage_buckets (
      tenant text NOT NULL, name text NOT NULL, public boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant, name));
    CREATE TABLE swsdb.storage_objects (
      tenant text NOT NULL, bucket text NOT NULL, path text NOT NULL, content_type text NOT NULL,
      size bigint NOT NULL, sha256 text NOT NULL, data bytea NOT NULL, owner text,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant, bucket, path),
      FOREIGN KEY (tenant, bucket) REFERENCES swsdb.storage_buckets (tenant, name) ON DELETE CASCADE);
    CREATE SCHEMA IF NOT EXISTS swsdb_auth;
    CREATE OR REPLACE FUNCTION swsdb_auth.uid() RETURNS text LANGUAGE sql STABLE
      AS $$ SELECT nullif(current_setting('swsdb.user_id', true), '') $$;
    CREATE OR REPLACE FUNCTION swsdb_auth.email() RETURNS text LANGUAGE sql STABLE
      AS $$ SELECT nullif(current_setting('swsdb.user_email', true), '') $$;
    CREATE OR REPLACE FUNCTION swsdb_auth.groups() RETURNS text[] LANGUAGE sql STABLE
      AS $$ SELECT coalesce(string_to_array(nullif(current_setting('swsdb.user_groups', true), ''), ','), '{}') $$;
    GRANT USAGE ON SCHEMA swsdb_auth TO PUBLIC;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA swsdb_auth TO PUBLIC;
    """,
]

TENANT_ROLE_PREFIX = "swsdb_t_"  # bootstrap's pg_ident map lets the server log in as any role with this prefix


def users_role(tenant: str) -> str:
    """The role a tenant's signed-in end users act as (Data API, RPC): no rights except what the tenant grants it,
    and row-level security applies to it ("__" can't occur in a tenant name, so it never clashes)."""
    name = f"{TENANT_ROLE_PREFIX}{tenant}__u"
    if len(name) > 63:
        name = f"{TENANT_ROLE_PREFIX}{hashlib.sha256(tenant.encode()).hexdigest()[:40]}__u"
    return name


def tenant_role(tenant: str) -> str:
    """The Postgres role (and schema) behind a tenant's pass-through SQL. Role names are limited to 63 bytes."""
    if tenant == "default":
        return APP_ROLE
    name = TENANT_ROLE_PREFIX + tenant
    if len(name) > 63:
        name = f"{name[:54]}_{hashlib.sha256(tenant.encode()).hexdigest()[:8]}"
    return name


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


class NewerSchema(RuntimeError):
    """The database was migrated by a newer swsdb: this one must not write to it."""


def pg_bin(name: str) -> str:
    return f"/usr/lib/postgresql/{os.environ.get('SWSDB_PG_MAJOR', '17')}/bin/{name}"


async def run_pg(*args: str, env: dict[str, str] | None = None) -> None:
    """Run a PostgreSQL client tool (pg_dump, pg_restore) and turn a failure into a clear error. Passwords go in the
    environment (`env`), never on the command line."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env={**os.environ, **(env or {})} if env else None)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        tool = os.path.basename(args[0])
        raise StoreError(f"{tool} failed ({proc.returncode}): {(err or out).decode(errors='replace').strip()[-2000:]}")


async def _reset_session(conn) -> None:
    """Back to the pool: drop what a pass-through statement may have left behind in the session."""
    await conn.execute("RESET ALL")
    await conn.execute("DISCARD TEMP")


class SqlStore:
    MAX_TENANT_POOLS = 32

    def __init__(self, socket_dir: str | None, dbname: str = "swsdb", max_size: int = 10, dsn: str | None = None,
                 own_pool: int = 4):
        """The bundled Postgres over its Unix socket (`socket_dir`), or an external one (`dsn`, whose user owns the
        database and may create roles)."""
        self.dsn = dsn
        self.socket_dir = socket_dir
        self.dbname = (psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") or dbname) if dsn else dbname
        self.pool = AsyncConnectionPool(conninfo=self._conninfo(None), min_size=0, max_size=own_pool, open=False,
                                        kwargs={"autocommit": True})  # swsdb's own work: migrations, catalog
        self.setup: list[str] = []  # DDL the lite stores need, applied after the migrations (idempotent)
        self._passwords: dict[str, str] = {}
        self._max_size = max_size
        self._tenant_pools: dict[str, AsyncConnectionPool] = {}  # role -> pool, most recently used last
        self._roles: dict[str, str] = {"default": APP_ROLE}
        self._migrated = False
        self.backup_dir: Path | None = None  # where a dump goes before migrating existing data (set by the app)
        self.last_backup: Path | None = None

    def _conninfo(self, role: str | None, password: str | None = None, dbname: str | None = None) -> str:
        """`role` None: swsdb's own login (the DSN's user, or the swsdb superuser over the socket)."""
        if self.dsn:
            extra = {"user": role, "password": password} if role else {}
            return psycopg.conninfo.make_conninfo(self.dsn, connect_timeout=5, **extra,
                                                  **({"dbname": dbname} if dbname else {}))
        return psycopg.conninfo.make_conninfo(host=self.socket_dir, dbname=dbname or self.dbname,
                                              user=role or "swsdb", connect_timeout=5)

    def tool_args(self, dbname: str | None = None) -> tuple[list[str], dict[str, str]]:
        """Connection arguments and environment for pg_dump / pg_restore."""
        if not self.dsn:
            return ["-h", self.socket_dir, "-U", "swsdb", "-d", dbname or self.dbname], {}
        p = psycopg.conninfo.conninfo_to_dict(self.dsn)
        args = ["-h", str(p.get("host") or "localhost"), "-p", str(p.get("port") or 5432), "-U", str(p.get("user")),
                "-d", dbname or self.dbname]
        env = {"PGPASSWORD": str(p.get("password") or "")}
        if p.get("sslmode"):
            env["PGSSLMODE"] = str(p["sslmode"])
        return args, env

    def admin_conninfo(self, dbname: str) -> str:
        return self._conninfo(None, dbname=dbname)

    async def _role_password(self, role: str) -> str | None:
        """External Postgres only: the sandbox role's password, set (and remembered) on first use."""
        if not self.dsn:
            return None
        if role in self._passwords:
            return self._passwords[role]
        rows = await self.rows("SELECT password FROM swsdb.sql_role_secrets WHERE role = %s", [role])
        if rows:
            pw = rows[0]["password"]
        else:
            pw = secrets.token_urlsafe(32)
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(pgsql.SQL("ALTER ROLE {} PASSWORD {}").format(pgsql.Identifier(role),
                                                                                 pgsql.Literal(pw)))
                await conn.execute("INSERT INTO swsdb.sql_role_secrets (role, password) VALUES (%s, %s) "
                                   "ON CONFLICT (role) DO UPDATE SET password = EXCLUDED.password", [role, pw])
        self._passwords[role] = pw
        return pw

    async def sync_role_passwords(self) -> None:
        """After a restore onto an external Postgres: give the roles the passwords the restored catalog holds."""
        if not self.dsn:
            return
        self._passwords.clear()
        for row in await self.rows("SELECT s.role, s.password FROM swsdb.sql_role_secrets s "
                                   "JOIN pg_roles r ON r.rolname = s.role"):
            await self.execute(pgsql.SQL("ALTER ROLE {} PASSWORD {}").format(pgsql.Identifier(row["role"]),
                                                                             pgsql.Literal(row["password"])))

    def _sandbox_pool(self, role: str, size: int, password: str | None = None) -> AsyncConnectionPool:
        return AsyncConnectionPool(conninfo=self._conninfo(role, password), min_size=0, max_size=size, open=False,
                                   kwargs={"autocommit": True}, reset=_reset_session)

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        for p in self._tenant_pools.values():
            await p.close()
        self._tenant_pools.clear()
        await self.pool.close()

    async def rows(self, statement: str, params: Any = None) -> list[dict]:
        """swsdb's own statements (catalog, keys), as the swsdb role."""
        await self.migrate()
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(statement, params)
            return await cur.fetchall() if cur.description else []

    async def execute(self, statement: str, params: Any = None) -> None:
        await self.rows(statement, params)

    async def tenant_role(self, tenant: str) -> str:
        """The tenant's sandbox role, created with its own schema on first use. It can't read other tenants' schemas,
        swsdb's catalog, or `public` (the default tenant's), and can't switch to another role."""
        if tenant in self._roles:
            return self._roles[tenant]
        role = tenant_role(tenant)
        await self.migrate()
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7367736463)")
            exists = await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])).fetchone()
            ident = pgsql.Identifier(role)
            if not exists:
                for stmt in (
                    "CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT "
                    "CONNECTION LIMIT 20",
                    "CREATE SCHEMA IF NOT EXISTS {r} AUTHORIZATION {r}",
                    "REVOKE ALL ON SCHEMA {r} FROM PUBLIC",
                    "ALTER ROLE {r} SET search_path = {r}",
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {r} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
                ):
                    await conn.execute(pgsql.SQL(stmt).format(r=ident))
            await conn.execute("INSERT INTO swsdb.sql_tenants (tenant, role) VALUES (%s, %s) "
                               "ON CONFLICT (tenant) DO NOTHING", [tenant, role])
        self._roles[tenant] = role
        return role

    async def users_role(self, tenant: str) -> str:
        """The tenant's end-users role, created on first use: it may log in and use the tenant's schema, nothing else
        until the tenant grants it a table (see the Policies page)."""
        owner = await self.tenant_role(tenant)
        role = users_role(tenant)
        if role in self._roles.values():
            return role
        schema = "public" if owner == APP_ROLE else owner
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7367736463)")
            exists = await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])).fetchone()
            r = pgsql.Identifier(role)
            if not exists:
                await conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                             "NOBYPASSRLS NOINHERIT CONNECTION LIMIT 20").format(r))
            await conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(pgsql.Identifier(schema), r))
            await conn.execute(pgsql.SQL("ALTER ROLE {} SET search_path = {}").format(r, pgsql.Identifier(schema)))
            await conn.execute("INSERT INTO swsdb.sql_tenants (tenant, role) VALUES (%s, %s) "
                               "ON CONFLICT (tenant) DO NOTHING", [f"{tenant}__u", role])
        self._roles[f"{tenant}__u"] = role
        return role

    async def query_as_user(self, tenant: str, user, statement, params, *, read_only: bool, timeout_s: float = 15,
                            max_rows: int = 1000) -> dict:
        """One statement swsdb wrote (the Data API, RPC), as the tenant's end-users role, with who the user is set
        for row-level security policies (swsdb_auth.uid(), .email(), .groups())."""
        await self.migrate()
        role = await self.users_role(tenant)
        try:
            pool = self._tenant_pools.pop(role, None)
            if pool is None:
                pool = self._sandbox_pool(role, 4, await self._role_password(role))
                await pool.open(wait=False)
                while len(self._tenant_pools) >= self.MAX_TENANT_POOLS:
                    old = self._tenant_pools.pop(next(iter(self._tenant_pools)))
                    await old.close()
            self._tenant_pools[role] = pool
            async with pool.connection(timeout=10) as conn, conn.transaction():
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true), set_config('swsdb.user_id', %s, true), "
                    "set_config('swsdb.user_email', %s, true), set_config('swsdb.user_groups', %s, true)",
                    [f"{int(timeout_s * 1000)}", user.id if user else "", user.email if user else "",
                     ",".join(user.groups) if user else ""])
                cur = await conn.execute(statement, params or None)
                if cur.description is None:
                    return {"columns": [], "rows": [], "row_count": cur.rowcount, "truncated": False}
                rows = await cur.fetchmany(max_rows + 1)
                return {"columns": [d.name for d in cur.description],
                        "rows": [[_json_safe(v) for v in r] for r in rows[:max_rows]],
                        "row_count": min(len(rows), max_rows), "truncated": len(rows) > max_rows}
        except psycopg.errors.InsufficientPrivilege as e:
            raise Forbidden(f"not allowed for signed-in users: {str(e).splitlines()[0]}") from e
        except psycopg.errors.QueryCanceled as e:
            raise StoreError(f"statement timed out after {timeout_s:g}s") from e
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available") from e
        except psycopg.Error as e:
            raise StoreError(f"{type(e).__name__}: {str(e).splitlines()[0]}") from e

    async def _pool_for(self, tenant: str) -> AsyncConnectionPool:
        role = await self.tenant_role(tenant)
        pool = self._tenant_pools.pop(role, None)
        if pool is None:
            pool = self._sandbox_pool(role, self._max_size if role == APP_ROLE else 4,
                                      await self._role_password(role))
            await pool.open(wait=False)
            while len(self._tenant_pools) >= self.MAX_TENANT_POOLS:  # close the least recently used
                old = self._tenant_pools.pop(next(iter(self._tenant_pools)))
                await old.close()
        self._tenant_pools[role] = pool
        return pool

    async def migrate(self) -> None:
        """Apply swsdb's own schema migrations once per process (idempotent and recorded)."""
        if self._migrated:
            return
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7367736462)")  # one migrator at a time
            await conn.execute("CREATE SCHEMA IF NOT EXISTS swsdb")
            await conn.execute("CREATE TABLE IF NOT EXISTS swsdb.migrations (version int PRIMARY KEY, "
                               "applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r[0] for r in await (await conn.execute("SELECT version FROM swsdb.migrations")).fetchall()}
            if done and max(done) > len(MIGRATIONS):
                raise NewerSchema(f"the database was upgraded by a newer swsdb (schema {max(done)}; this image knows "
                                  f"up to {len(MIGRATIONS)}). Run that newer image, or restore a snapshot taken with "
                                  "this version.")
            pending = [v for v in range(1, len(MIGRATIONS) + 1) if v not in done]
            if done and pending and self.backup_dir is not None:
                # an upgrade: keep the data as it was, so a rollback is a pg_restore away
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                path = self.backup_dir / f"before-schema-{max(pending)}-{time.strftime('%Y%m%d-%H%M%S')}.dump"
                args, env = self.tool_args()
                await run_pg(pg_bin("pg_dump"), *args, "-Fc", "-f", str(path), env=env)
                self.last_backup = path
            for version, ddl in enumerate(MIGRATIONS, start=1):
                if version not in done:
                    await conn.execute(ddl)
                    await conn.execute("INSERT INTO swsdb.migrations (version) VALUES (%s)", [version])
            for ddl in self.setup:
                try:
                    await conn.execute(ddl)
                except psycopg.errors.InsufficientPrivilege as e:
                    raise StoreError(f"the lite profile needs the vector and age extensions in this database, and "
                                     f"creating them needs a superuser: {e}") from e
        self._migrated = True

    def add_setup(self, ddl: str) -> None:
        if ddl not in self.setup:
            self.setup.append(ddl)

    def forget(self) -> None:
        """The database was replaced (restore): migrate again and recreate tenant roles and pools on next use."""
        self._migrated = False
        self._roles = {"default": APP_ROLE}
        self._passwords.clear()

    async def drop_connections(self) -> None:
        """After the database was dropped and recreated: discard pooled connections to the old one."""
        for p in list(self._tenant_pools.values()):
            await p.close()
        self._tenant_pools.clear()
        await self.pool.check()

    async def query(self, statement: str, params: list | dict | None, *, read_only: bool, timeout_s: float,
                    max_rows: int, tenant: str = "default") -> dict:
        """One statement as the tenant's sandbox role, in its own transaction. Server-side prepared, so a string
        holding several statements is rejected by Postgres (no `COMMIT; ...` escaping read-only mode or the
        timeout)."""
        await self.migrate()
        try:
            pool = await self._pool_for(tenant)
            async with pool.connection(timeout=10) as conn, conn.transaction():
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SELECT set_config('statement_timeout', %s, true)", [f"{int(timeout_s * 1000)}"])
                cur = await conn.execute(statement, params or None, prepare=True)
                if cur.description is None:
                    return {"columns": [], "rows": [], "row_count": cur.rowcount, "truncated": False}
                rows = await cur.fetchmany(max_rows + 1)
                return {
                    "columns": [d.name for d in cur.description],
                    "rows": [[_json_safe(v) for v in r] for r in rows[:max_rows]],
                    "row_count": min(len(rows), max_rows),
                    "truncated": len(rows) > max_rows,
                }
        except psycopg.errors.QueryCanceled as e:  # statement_timeout: the query's fault, not an outage
            raise StoreError(f"statement timed out after {timeout_s:g}s") from e
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available (pool exhausted or server unreachable)") from e
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            if isinstance(e, psycopg.errors.DatabaseError) and e.sqlstate and not e.sqlstate.startswith("08"):
                raise StoreError(f"{type(e).__name__}: {e}".strip()) from e  # e.g. out of memory for this query
            raise Unavailable(f"PostgreSQL: {e}") from e
        except psycopg.Error as e:
            raise StoreError(f"{type(e).__name__}: {e}".strip()) from e


# ------------------------------------------------------------------ vectors (Qdrant)

DISTANCES = {"cosine": "Cosine", "dot": "Dot", "euclid": "Euclid", "manhattan": "Manhattan"}


@dataclass
class VectorSpec:
    size: int
    distance: str = "cosine"


def _qdrant_filter(where: dict[str, Any] | None) -> dict | None:
    """`{"team": "support", "year": [2024, 2025]}` -> a Qdrant filter: every key must match (lists mean any of)."""
    if not where:
        return None
    must = []
    for key, value in where.items():
        if not isinstance(key, str) or not key or key.startswith("_"):
            raise StoreError(f"invalid filter field {key!r}")
        if isinstance(value, list):
            must.append({"key": key, "match": {"any": value}})
        elif isinstance(value, (str, int, bool)):
            must.append({"key": key, "match": {"value": value}})
        else:
            raise StoreError(f"filter on {key!r}: use a string, integer, boolean or a list of them")
    return {"must": must}


def _browsed(p: dict) -> dict:
    payload = dict(p.get("payload") or {})
    out = {"id": payload.pop("_id", p["id"]), "point_id": p["id"], "payload": payload}
    if p.get("vector") is not None:
        out["vector"] = p["vector"]
    return out


class VectorStore:
    in_postgres = False  # the lite profile's pgvector store says True: it is copied with the Postgres dump
    engine = "qdrant"

    def __init__(self, url: str, api_key: str):
        # identity: Qdrant otherwise gzips every reply httpx asks it to, which costs ~40 ms per search on loopback
        self.http = httpx.AsyncClient(base_url=url, timeout=30.0,
                                      headers={"api-key": api_key, "accept-encoding": "identity"})

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise Unavailable(f"Qdrant: {type(e).__name__}") from e
        if r.status_code == 404:
            raise NotFound(r.json().get("status", {}).get("error", "not found") if r.content else "not found")
        if r.status_code == 409:
            raise Conflict(r.json().get("status", {}).get("error", "already exists"))
        if r.status_code >= 500:
            raise Unavailable(f"Qdrant answered {r.status_code}")
        body = r.json()
        if r.status_code >= 400:
            raise StoreError(body.get("status", {}).get("error", f"Qdrant answered {r.status_code}"))
        return body

    async def all_names(self) -> list[str]:
        """Every collection in the engine, swsdb's or not."""
        return sorted(c["name"] for c in (await self._call("GET", "/collections"))["result"]["collections"])

    async def list(self, tenant: str) -> list[str]:
        return sorted(n for c in await self.all_names() if (n := parse_engine_name(c, tenant)))

    async def create(self, name: str, spec: VectorSpec) -> None:
        if spec.distance not in DISTANCES:
            raise StoreError(f"distance must be one of {', '.join(DISTANCES)}")
        if not 1 <= spec.size <= 65536:
            raise StoreError("vector size must be between 1 and 65536")
        await self._call("PUT", f"/collections/{name}",
                         json={"vectors": {"size": spec.size, "distance": DISTANCES[spec.distance]}})

    async def info(self, name: str) -> dict:
        r = (await self._call("GET", f"/collections/{name}"))["result"]
        params = r["config"]["params"]
        v = params["vectors"]
        if "size" not in v and "dense" in v:  # a documents collection: named dense + sparse vectors
            v = {**v["dense"], "sparse": list(params.get("sparse_vectors") or {})}
        dist = {q: k for k, q in DISTANCES.items()}.get(v.get("distance"), v.get("distance"))
        out = {"size": v.get("size"), "distance": dist, "points": r.get("points_count"), "status": r.get("status")}
        if v.get("sparse"):
            out["sparse"] = v["sparse"]
        return out

    async def drop(self, name: str) -> bool:
        return bool((await self._call("DELETE", f"/collections/{name}"))["result"])

    async def upsert(self, name: str, points: list[dict]) -> int:
        out = []
        for p in points:
            pid = point_id(p["id"])
            payload = dict(p.get("payload") or {})
            if any(k.startswith("_") for k in payload):
                raise StoreError("payload keys starting with '_' are reserved for swsdb")
            payload["_id"] = p["id"]
            out.append({"id": pid, "vector": p["vector"], "payload": payload})
        await self._call("PUT", f"/collections/{name}/points", params={"wait": "true"}, json={"points": out})
        return len(out)

    async def search(self, name: str, vector: list[float], limit: int, where: dict | None,
                     with_payload: bool = True) -> list[dict]:
        body: dict[str, Any] = {"query": vector, "limit": limit, "with_payload": True}
        if (f := _qdrant_filter(where)) is not None:
            body["filter"] = f
        hits = (await self._call("POST", f"/collections/{name}/points/query", json=body))["result"]["points"]
        out = []
        for h in hits:
            payload = dict(h.get("payload") or {})
            original = payload.pop("_id", h["id"])
            out.append({"id": original, "score": h["score"], **({"payload": payload} if with_payload else {})})
        return out

    async def get(self, name: str, ids: list[str | int]) -> list[dict]:
        r = await self._call("POST", f"/collections/{name}/points",
                             json={"ids": [point_id(i) for i in ids], "with_payload": True, "with_vector": True})
        out = []
        for p in r["result"]:
            payload = dict(p.get("payload") or {})
            out.append({"id": payload.pop("_id", p["id"]), "vector": p.get("vector"), "payload": payload})
        return out

    async def delete(self, name: str, ids: list[str | int]) -> None:
        await self._call("POST", f"/collections/{name}/points/delete", params={"wait": "true"},
                         json={"points": [point_id(i) for i in ids]})

    # -- document collections: named dense + sparse (BM25) vectors, chunks invisible until their document is ready

    async def create_documents(self, name: str, dim: int) -> None:
        await self._call("PUT", f"/collections/{name}", json={
            "vectors": {"dense": {"size": dim, "distance": "Cosine"}},
            "sparse_vectors": {"sparse": {"modifier": "idf"}},
            "on_disk_payload": True})
        for field, schema in (("document_id", "keyword"), ("readers", "keyword"), ("live", "bool"),
                              ("version", "integer")):
            await self._call("PUT", f"/collections/{name}/index", params={"wait": "true"},
                             json={"field_name": field, "field_schema": schema})

    async def is_documents(self, name: str) -> bool:
        r = (await self._call("GET", f"/collections/{name}"))["result"]["config"]["params"]
        return isinstance(r.get("vectors"), dict) and "dense" in r["vectors"] and "sparse" in (r.get("sparse_vectors")
                                                                                             or {})

    async def upsert_chunks(self, name: str, points: list[dict]) -> None:
        for i in range(0, len(points), 256):
            await self._call("PUT", f"/collections/{name}/points", params={"wait": "true"},
                             json={"points": points[i:i + 256]})

    async def set_payload(self, name: str, payload: dict, flt: dict) -> None:
        await self._call("POST", f"/collections/{name}/points/payload", params={"wait": "true"},
                         json={"payload": payload, "filter": flt})

    async def delete_where(self, name: str, flt: dict) -> None:
        await self._call("POST", f"/collections/{name}/points/delete", params={"wait": "true"}, json={"filter": flt})

    async def count(self, name: str, flt: dict) -> int:
        body = {"filter": flt, "exact": True}
        return int((await self._call("POST", f"/collections/{name}/points/count", json=body))["result"]["count"])

    async def scan(self, name: str, fields: list[str], page: int = 1000):
        """Every point's id and the given payload fields, page by page (for reconciliation)."""
        offset = None
        while True:
            body: dict[str, Any] = {"limit": page, "with_payload": {"include": fields}, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = (await self._call("POST", f"/collections/{name}/points/scroll", json=body))["result"]
            for pt in r["points"]:
                yield pt
            offset = r.get("next_page_offset")
            if offset is None:
                return

    async def query(self, name: str, body: dict) -> list[dict]:
        return (await self._call("POST", f"/collections/{name}/points/query", json=body))["result"]["points"]

    async def browse(self, name: str, limit: int, offset: str | int | None, where: dict | None,
                     with_vector: bool = False) -> dict:
        """A page of points in id order, for looking at the data: {"points", "next"} (next: the offset to pass for
        the following page, None at the end)."""
        body: dict[str, Any] = {"limit": limit, "with_payload": True, "with_vector": with_vector}
        if offset is not None:
            body["offset"] = offset  # Qdrant's own next_page_offset: an integer or a UUID
        if (f := _qdrant_filter(where)) is not None:
            body["filter"] = f
        r = (await self._call("POST", f"/collections/{name}/points/scroll", json=body))["result"]
        return {"points": [_browsed(p) for p in r["points"]], "next": r.get("next_page_offset")}

    async def scroll(self, name: str, flt: dict, limit: int) -> list[dict]:
        body = {"filter": flt, "limit": limit, "with_payload": True, "with_vector": False}
        return (await self._call("POST", f"/collections/{name}/points/scroll", json=body))["result"]["points"]


# ------------------------------------------------------------------ graph (FalkorDB)


def cypher_literal(v: Any) -> str:
    """A Python value as a Cypher literal, for FalkorDB's `CYPHER name=value` parameter header."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise StoreError("graph parameters can't be NaN or infinite")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # double-quoted with backslash escapes, which Cypher accepts
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(cypher_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if not isinstance(k, str) or not k.replace("_", "a").isalnum():
                raise StoreError(f"graph map keys must be identifiers, got {k!r}")
            parts.append(f"{k}: {cypher_literal(x)}")
        return "{" + ", ".join(parts) + "}"
    raise StoreError(f"unsupported graph parameter type {type(v).__name__}")


def _cypher_code(query: str) -> str:
    """The query with comments, string literals and `quoted` names blanked out: what's left is the actual code."""
    out, i, n = [], 0, len(query)
    while i < n:
        c = query[i]
        if query.startswith("//", i):
            j = query.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c in "'\"`":
            j = i + 1
            while j < n and query[j] != c:
                j += 2 if query[j] == "\\" and c != "`" else 1
            i = j + 1
            out.append(" '' ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


_LOAD_CSV = re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE)


def check_pass_through_cypher(query: str) -> None:
    """Refuse what a pass-through query must not do. LOAD CSV makes the graph engine fetch any URL it can reach (inside
    the container that includes the other engines and cloud metadata endpoints), so it is off for API callers."""
    if _LOAD_CSV.search(_cypher_code(query)):
        raise Forbidden("LOAD CSV is disabled in swsdb: it would let a query make the server fetch arbitrary URLs. "
                        "Load data through the API or with parameters instead.")


def _decode(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, list):
        return [_decode(x) for x in v]
    return v


class _StaleSchema(Exception):
    pass


def _typed(cell: list, schema: tuple[list[str], list[str], list[str]]) -> Any:
    """One value of FalkorDB's compact reply ([type, value]) as plain JSON: lists, maps, nodes, edges and paths keep
    their structure, numbers and booleans their type."""
    kind, v = cell
    labels, types, keys = schema

    def name(table: list[str], i: int) -> str:
        if i >= len(table):
            raise _StaleSchema
        return table[i]

    def props(raw: list) -> dict:
        return {name(keys, k): _typed([t, x], schema) for k, t, x in raw}

    if kind == 1:
        return None
    if kind == 2:
        return _decode(v)
    if kind == 3:
        return int(v)
    if kind == 4:
        return _decode(v) == "true"
    if kind == 5:
        return float(_decode(v))
    if kind == 6:
        return [_typed(x, schema) for x in v]
    if kind == 7:
        eid, rel, src, dst, raw = v
        return {"id": eid, "type": name(types, rel), "src": src, "dst": dst, "properties": props(raw)}
    if kind == 8:
        nid, lab, raw = v
        return {"id": nid, "labels": [name(labels, i) for i in lab], "properties": props(raw)}
    if kind == 9:
        nodes, edges = (_typed(x, schema) for x in v)
        return {"nodes": nodes, "edges": edges}
    if kind == 10:
        return {_decode(v[i]): _typed(v[i + 1], schema) for i in range(0, len(v), 2)}
    if kind == 11:
        return {"latitude": float(_decode(v[0])), "longitude": float(_decode(v[1]))}
    if kind == 12:
        return [float(_decode(x)) for x in v]
    return _decode(v)  # dates, times, durations: as FalkorDB gives them


class GraphStore:
    in_postgres = False
    engine = "falkordb"
    pass_through = True  # Cypher from API callers runs (each collection is its own graph)

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, password: str | None = None,
                 url: str | None = None):
        """The bundled FalkorDB, or an external one by `url` (redis://:password@host:6379, rediss:// for TLS)."""
        self.r = (aioredis.Redis.from_url(url, socket_timeout=60, socket_connect_timeout=5) if url else
                  aioredis.Redis(host=host, port=port, password=password, socket_timeout=60, socket_connect_timeout=5))
        self._schemas: dict[str, tuple[list[str], list[str], list[str]]] = {}

    async def close(self) -> None:
        await self.r.aclose()

    async def all_names(self) -> list[str]:
        return sorted(_decode(g) for g in await self._cmd("GRAPH.LIST"))

    def forget(self) -> None:
        self._schemas.clear()

    async def count_nodes(self, name: str) -> int:
        return (await self.query(name, "MATCH (n) RETURN count(n)", None, read_only=True, timeout_s=10))["rows"][0][0]

    async def dump(self, name: str) -> bytes | None:
        """The graph as one value (Redis DUMP), for snapshots."""
        return await self.r.dump(name)

    async def restore(self, name: str, data: bytes) -> None:
        await self.r.restore(name, 0, data, replace=True)
        self._schemas.pop(name, None)

    async def list(self, tenant: str) -> list[str]:
        return sorted(n for g in await self.all_names() if (n := parse_engine_name(g, tenant)))

    async def index(self, name: str, label: str, prop: str) -> None:
        try:
            await self.query(name, f"CREATE INDEX FOR (x:{label}) ON (x.{prop})", None, read_only=False,
                             timeout_s=30)
        except StoreError as e:
            if "already indexed" not in str(e).lower():
                raise

    async def delete_orphan_entities(self, name: str) -> None:
        await self.query(name, "MATCH (e:Entity) WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e", None,
                         read_only=False, timeout_s=120)

    async def _cmd(self, *args):
        try:
            return await self.r.execute_command(*args)
        except aioredis.ResponseError as e:
            msg = str(e)
            if "empty key" in msg.lower() or "invalid graph operation on empty key" in msg.lower():
                raise NotFound("graph not found") from e
            raise StoreError(msg) from e
        except (aioredis.ConnectionError, aioredis.TimeoutError) as e:
            raise Unavailable(f"FalkorDB: {type(e).__name__}") from e

    async def query(self, name: str, query: str, params: dict | None, *, read_only: bool, timeout_s: float) -> dict:
        header = ""
        if params:
            for k in params:
                if not isinstance(k, str) or not k.replace("_", "a").isalnum() or k[0].isdigit():
                    raise StoreError(f"invalid parameter name {k!r}")
            header = "CYPHER " + " ".join(f"{k}={cypher_literal(v)}" for k, v in params.items()) + " "
        res = await self._cmd("GRAPH.RO_QUERY" if read_only else "GRAPH.QUERY", name, header + query,
                              "--timeout", int(timeout_s * 1000), "--compact")
        if len(res) == 1:  # write-only query: just statistics
            return {"columns": [], "rows": [], "stats": [_decode(s) for s in res[0]]}
        columns = [_decode(c[1]) for c in res[0]]
        for attempt in range(2):
            try:
                schema = await self._schema(name, refresh=attempt > 0)
                rows = [[_typed(cell, schema) for cell in row] for row in res[1]]
                break
            except _StaleSchema:
                if attempt:
                    raise StoreError("the graph's labels or property keys changed while reading the result") from None
        return {"columns": columns, "rows": rows, "stats": [_decode(s) for s in res[2]]}

    async def _schema(self, name: str, refresh: bool = False) -> tuple[list[str], list[str], list[str]]:
        """Labels, relationship types and property keys by id: compact replies refer to them by number."""
        if refresh or name not in self._schemas:
            out = []
            for proc in ("db.labels()", "db.relationshipTypes()", "db.propertyKeys()"):
                r = await self._cmd("GRAPH.RO_QUERY", name, f"CALL {proc}")
                out.append([_decode(row[0]) for row in r[1]] if len(r) > 1 else [])
            self._schemas[name] = (out[0], out[1], out[2])
        return self._schemas[name]

    async def drop(self, name: str) -> bool:
        try:
            await self._cmd("GRAPH.DELETE", name)
            return True
        except NotFound:
            return False
        except StoreError as e:
            if "no such" in str(e).lower() or "empty key" in str(e).lower():
                return False
            raise

    async def exists(self, name: str) -> bool:
        return bool(await self.r.exists(name))
