"""The Data API (/v1/rest), database functions over HTTP (/v1/rpc), row-level security management and query
statistics: what Supabase gives with PostgREST, policies and pg_stat_statements.

Every statement here is written by swsdb from validated names: table, column and function names must exist in the
tenant's schema, and every value travels as a parameter. Keys run it as the tenant's role (like Supabase's service
role: row-level security doesn't apply to the table owner). Signed-in end users run it as the tenant's end-users role,
which can touch only what the tenant exposed to it, row by row as its policies allow.
"""

from __future__ import annotations

import json
import re
from typing import Any

from psycopg import sql as pgsql

from .stores import APP_ROLE, Forbidden, NotFound, SqlStore, StoreError, users_role

OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE", "ilike": "ILIKE"}
RESERVED = {"select", "order", "limit", "offset", "count"}
COMMANDS = ("ALL", "SELECT", "INSERT", "UPDATE", "DELETE")
POLICY_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,63}$")


def schema_of(owner: str) -> str:
    return "public" if owner == APP_ROLE else owner


class DataApi:
    def __init__(self, sql: SqlStore):
        self.sql = sql

    # ---------------------------------------------------------------- catalog of the tenant's schema

    async def _q(self, tenant: str, statement: str, params=None, *, read_only=True) -> dict:
        return await self.sql.query(statement, params, read_only=read_only, timeout_s=15, max_rows=5000, tenant=tenant)

    async def columns(self, tenant: str, table: str) -> dict[str, str]:
        r = await self._q(
            tenant,
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
            [table],
        )
        if not r["rows"]:
            raise NotFound(f"no table {table!r} in this tenant")
        if "%" in table or any("%" in c for c, _ in r["rows"]):  # it would read as a parameter placeholder
            raise StoreError("the Data API can't serve tables or columns with % in their names; use SQL")
        return {c: t for c, t in r["rows"]}

    async def target(self, tenant: str, table: str) -> tuple[pgsql.Composable, dict[str, str]]:
        cols = await self.columns(tenant, table)
        owner = await self.sql.tenant_role(tenant)
        return pgsql.Identifier(schema_of(owner), table), cols

    # ---------------------------------------------------------------- running, as a key or as an end user

    async def _run(self, tenant: str, caller, statement: pgsql.Composable, params: list, *, write: bool) -> dict:
        if caller.role == "read" and write:
            raise Forbidden("the read-only key can't change rows")
        text = statement.as_string(None)
        if caller.role == "user":
            return await self.sql.query_as_user(tenant, caller.user, text, params, read_only=not write)
        return await self.sql.query(text, params, read_only=not write, timeout_s=15, max_rows=1000, tenant=tenant)

    @staticmethod
    def _objects(res: dict) -> list[dict]:
        return [dict(zip(res["columns"], row, strict=True)) for row in res["rows"]]

    @staticmethod
    def _value(v: Any) -> Any:
        return json.dumps(v) if isinstance(v, (dict, list)) else v

    def _filters(self, args: dict[str, str], cols: dict[str, str]) -> tuple[list[pgsql.Composable], list]:
        """`column=op.value` pairs (PostgREST style): eq, neq, gt, gte, lt, lte, like, ilike, is (null, true,
        false), in.(a,b,c)."""
        parts, params = [], []
        for col, spec in args.items():
            if col in RESERVED:
                continue
            if col not in cols:
                raise StoreError(f"unknown column {col!r}")
            op, _, value = spec.partition(".")
            c = pgsql.Identifier(col)
            if op in OPS:
                parts.append(pgsql.SQL("{} {} %s").format(c, pgsql.SQL(OPS[op])))
                params.append(value)
            elif op == "is" and value in ("null", "true", "false"):
                parts.append(pgsql.SQL("{} IS {}").format(c, pgsql.SQL(value.upper())))
            elif op == "not" and value.startswith("is."):
                v = value[3:]
                if v not in ("null", "true", "false"):
                    raise StoreError(f"{col}: not.is takes null, true or false")
                parts.append(pgsql.SQL("{} IS NOT {}").format(c, pgsql.SQL(v.upper())))
            elif op == "in" and value.startswith("(") and value.endswith(")"):
                parts.append(pgsql.SQL("{}::text = ANY(%s::text[])").format(c))
                params.append([x.strip() for x in value[1:-1].split(",") if x.strip()])
            else:
                raise StoreError(
                    f"{col}: unknown filter {spec!r} (use eq., neq., gt., gte., lt., lte., like., "
                    "ilike., is., not.is., in.(...))"
                )
        return parts, params

    # ---------------------------------------------------------------- REST: select, insert, update, delete

    async def select(self, tenant: str, caller, table: str, args: dict[str, str]) -> dict:
        target, cols = await self.target(tenant, table)
        want = [c.strip() for c in args.get("select", "*").split(",") if c.strip()]
        if want != ["*"]:
            for c in want:
                if c not in cols:
                    raise StoreError(f"unknown column {c!r}")
        sel = pgsql.SQL("*") if want == ["*"] else pgsql.SQL(", ").join(map(pgsql.Identifier, want))
        parts, params = self._filters(args, cols)
        where = pgsql.SQL(" WHERE ") + pgsql.SQL(" AND ").join(parts) if parts else pgsql.SQL("")
        order = []
        for item in [x for x in args.get("order", "").split(",") if x]:
            col, _, direction = item.partition(".")
            if col not in cols or direction not in ("", "asc", "desc"):
                raise StoreError(f"order: use column.asc or column.desc (got {item!r})")
            order.append(
                pgsql.SQL("{} {} NULLS LAST").format(pgsql.Identifier(col), pgsql.SQL(direction.upper() or "ASC"))
            )
        by = pgsql.SQL(" ORDER BY ") + pgsql.SQL(", ").join(order) if order else pgsql.SQL("")
        try:
            limit = min(int(args.get("limit", 100)), 1000)
            offset = max(int(args.get("offset", 0)), 0)
        except ValueError:
            raise StoreError("limit and offset are numbers") from None
        stmt = (
            pgsql.SQL("SELECT {} FROM {}").format(sel, target)
            + where
            + by
            + pgsql.SQL(f" LIMIT {limit} OFFSET {offset}")
        )
        res = await self._run(tenant, caller, stmt, params, write=False)
        count = None
        if args.get("count") == "exact":
            c = await self._run(
                tenant, caller, pgsql.SQL("SELECT count(*) FROM {}").format(target) + where, params, write=False
            )
            count = c["rows"][0][0]
        return {"data": self._objects(res), "count": count}

    async def insert(self, tenant: str, caller, table: str, body: Any) -> dict:
        target, cols = await self.target(tenant, table)
        rows = body if isinstance(body, list) else [body]
        if not rows or not all(isinstance(r, dict) for r in rows):
            raise StoreError("send an object, or a list of objects, of column values")
        if len(rows) > 1000:
            raise StoreError("at most 1000 rows at a time")
        names = sorted({k for r in rows for k in r})
        for n in names:
            if n not in cols:
                raise StoreError(f"unknown column {n!r}")
        params: list = []
        values = []
        for r in rows:
            cells = []
            for n in names:
                if n in r:
                    cells.append(pgsql.SQL("%s"))
                    params.append(self._value(r[n]))
                else:
                    cells.append(pgsql.SQL("DEFAULT"))
            values.append(pgsql.SQL("(") + pgsql.SQL(", ").join(cells) + pgsql.SQL(")"))
        if names:
            stmt = (
                pgsql.SQL("INSERT INTO {} ({}) VALUES ").format(
                    target, pgsql.SQL(", ").join(map(pgsql.Identifier, names))
                )
                + pgsql.SQL(", ").join(values)
                + pgsql.SQL(" RETURNING *")
            )
        else:
            stmt = pgsql.SQL("INSERT INTO {} DEFAULT VALUES RETURNING *").format(target)
        res = await self._run(tenant, caller, stmt, params, write=True)
        return {"data": self._objects(res)}

    async def update(self, tenant: str, caller, table: str, args: dict[str, str], body: Any) -> dict:
        target, cols = await self.target(tenant, table)
        if not isinstance(body, dict) or not body:
            raise StoreError("send an object of the columns to change")
        parts, fparams = self._filters(args, cols)
        if not parts:
            raise StoreError("an update needs at least one filter (e.g. ?id=eq.5): it would change every row")
        sets, params = [], []
        for k, v in body.items():
            if k not in cols:
                raise StoreError(f"unknown column {k!r}")
            sets.append(pgsql.SQL("{} = %s").format(pgsql.Identifier(k)))
            params.append(self._value(v))
        stmt = (
            pgsql.SQL("UPDATE {} SET ").format(target)
            + pgsql.SQL(", ").join(sets)
            + pgsql.SQL(" WHERE ")
            + pgsql.SQL(" AND ").join(parts)
            + pgsql.SQL(" RETURNING *")
        )
        return {"data": self._objects(await self._run(tenant, caller, stmt, params + fparams, write=True))}

    async def delete(self, tenant: str, caller, table: str, args: dict[str, str]) -> dict:
        target, cols = await self.target(tenant, table)
        parts, params = self._filters(args, cols)
        if not parts:
            raise StoreError("a delete needs at least one filter (e.g. ?id=eq.5): it would remove every row")
        stmt = (
            pgsql.SQL("DELETE FROM {} WHERE ").format(target)
            + pgsql.SQL(" AND ").join(parts)
            + pgsql.SQL(" RETURNING *")
        )
        return {"data": self._objects(await self._run(tenant, caller, stmt, params, write=True))}

    # ---------------------------------------------------------------- RPC: the tenant's functions over HTTP

    async def functions(self, tenant: str) -> list[dict]:
        users = await self.sql.users_role(tenant)
        r = await self._q(
            tenant,
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
            "pg_get_function_result(p.oid), coalesce(p.proargnames, '{}'), p.provolatile, "
            "has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = current_schema() "
            "AND p.prokind = 'f' ORDER BY 1",
            [users],
        )
        return [
            {
                "name": n,
                "arguments": a,
                "returns": ret,
                "argnames": list(names),
                "volatile": v == "v",
                "exposed": bool(ex),
            }
            for n, a, ret, names, v, ex in r["rows"]
        ]

    async def rpc(self, tenant: str, caller, name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise StoreError("send the arguments as an object of name: value")
        fns = [f for f in await self.functions(tenant) if f["name"] == name]
        if not fns:
            raise NotFound(f"no function {name!r} in this tenant")
        fn = next((f for f in fns if set(args) <= set(f["argnames"])), None)
        if fn is None:
            raise StoreError(f"{name} takes {', '.join(fns[0]['argnames']) or 'no arguments'}")
        owner = await self.sql.tenant_role(tenant)
        call = pgsql.SQL(", ").join(pgsql.SQL("{} => %s").format(pgsql.Identifier(k)) for k in args)
        stmt = pgsql.SQL("SELECT * FROM {}({})").format(pgsql.Identifier(schema_of(owner), name), call)
        # a function that doesn't change anything may run in a read-only transaction (and for the read key)
        res = await self._run(tenant, caller, stmt, [self._value(v) for v in args.values()], write=fn["volatile"])
        return {"data": self._objects(res)}

    # ---------------------------------------------------------------- row-level security

    async def policies(self, tenant: str) -> dict:
        users = await self.sql.users_role(tenant)
        tables = await self._q(
            tenant,
            (
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, has_table_privilege(%s, c.oid, 'SELECT'), "
                "has_table_privilege(%s, c.oid, 'INSERT'), has_table_privilege(%s, c.oid, 'UPDATE'), "
                "has_table_privilege(%s, c.oid, 'DELETE') FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') ORDER BY 1"
            ),
            [users] * 4,
        )
        pols = await self._q(
            tenant,
            "SELECT tablename, policyname, cmd, permissive, roles::text[], qual, with_check "
            "FROM pg_policies WHERE schemaname = current_schema() ORDER BY 1, 2",
        )
        by: dict[str, list] = {}
        for t, name, cmd, permissive, roles, qual, check in pols["rows"]:
            by.setdefault(t, []).append(
                {
                    "name": name,
                    "command": cmd,
                    "permissive": permissive == "PERMISSIVE",
                    "roles": roles,
                    "using": qual,
                    "check": check,
                }
            )
        return {
            "users_role": users,
            "tables": [
                {
                    "table": t,
                    "rls": rls,
                    "forced": forced,
                    "exposed": sel or ins or upd or dele,
                    "grants": [
                        g for g, ok in (("select", sel), ("insert", ins), ("update", upd), ("delete", dele)) if ok
                    ],
                    "policies": by.get(t, []),
                }
                for t, rls, forced, sel, ins, upd, dele in tables["rows"]
            ],
            "functions": await self.functions(tenant),
        }

    async def expose_table(self, tenant: str, table: str, expose: bool) -> None:
        """Expose: row-level security on, and the end-users role may select, insert, update and delete (only the
        rows policies allow; with no policy, none). Hide: the end-users role loses every right on it."""
        target, _ = await self.target(tenant, table)
        users = pgsql.Identifier(await self.sql.users_role(tenant))
        owner = await self.sql.tenant_role(tenant)
        stmts = (
            [
                pgsql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(target),
                pgsql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(target, users),
                pgsql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                    pgsql.Identifier(schema_of(owner)), users
                ),
            ]
            if expose
            else [pgsql.SQL("REVOKE ALL ON {} FROM {}").format(target, users)]
        )
        for s in stmts:
            await self._q(tenant, s.as_string(None), read_only=False)

    async def create_policy(
        self, tenant: str, table: str, name: str, command: str, using: str | None, check: str | None
    ) -> None:
        target, _ = await self.target(tenant, table)
        if not POLICY_NAME.match(name):
            raise StoreError("a policy name is 1-63 letters, digits, spaces, '_' or '-'")
        command = command.upper()
        if command not in COMMANDS:
            raise StoreError(f"command must be one of {', '.join(COMMANDS)}")
        if command == "INSERT" and using:
            raise StoreError("an INSERT policy takes only a check expression")
        if command in ("SELECT", "DELETE") and check:
            raise StoreError(f"a {command} policy takes only a using expression")
        if not using and not check:
            raise StoreError("give a using or a check expression")
        users = pgsql.Identifier(await self.sql.users_role(tenant))
        # the expressions are the tenant admin's SQL; they run as the tenant's own role, as one statement
        stmt = pgsql.SQL("CREATE POLICY {} ON {} AS PERMISSIVE FOR {} TO {}").format(
            pgsql.Identifier(name), target, pgsql.SQL(command), users
        )
        if using:
            stmt += pgsql.SQL(" USING (") + pgsql.SQL(using) + pgsql.SQL(")")
        if check:
            stmt += pgsql.SQL(" WITH CHECK (") + pgsql.SQL(check) + pgsql.SQL(")")
        await self._q(tenant, stmt.as_string(None), read_only=False)

    async def drop_policy(self, tenant: str, table: str, name: str) -> None:
        target, _ = await self.target(tenant, table)
        await self._q(
            tenant,
            pgsql.SQL("DROP POLICY {} ON {}").format(pgsql.Identifier(name), target).as_string(None),
            read_only=False,
        )

    async def expose_function(self, tenant: str, name: str, expose: bool) -> None:
        fns = [f for f in await self.functions(tenant) if f["name"] == name]
        if not fns:
            raise NotFound(f"no function {name!r} in this tenant")
        owner = await self.sql.tenant_role(tenant)
        users = pgsql.Identifier(await self.sql.users_role(tenant))
        for f in fns:
            sig = pgsql.SQL("{}({})").format(pgsql.Identifier(schema_of(owner), name), pgsql.SQL(f["arguments"]))
            stmt = (
                pgsql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}")
                if expose
                else pgsql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}")
            ).format(sig, users)
            await self._q(tenant, stmt.as_string(None), read_only=False)


# ---------------------------------------------------------------- query statistics (pg_stat_statements)

STATS_SETUP = """
DO $stats$ BEGIN
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EXCEPTION WHEN OTHERS THEN NULL;  -- not installed, or not allowed here: the console says so
END $stats$;
"""


async def query_stats(sql: SqlStore, roles: list[str] | None, limit: int = 50, order: str = "total") -> dict:
    """The statements that took the most time, as Postgres normalizes them ($1 for values: no literals are shown).
    `roles` None: every role (swsdb's own work included)."""
    by = {"total": "s.total_exec_time", "mean": "s.mean_exec_time", "calls": "s.calls", "rows": "s.rows"}.get(order)
    if by is None:
        raise StoreError("order is total, mean, calls or rows")
    try:
        rows = await sql.rows(
            f"SELECT r.rolname AS role, s.query, s.calls, s.total_exec_time AS total_ms, s.mean_exec_time AS mean_ms, "  # noqa: S608
            "s.max_exec_time AS max_ms, s.rows, s.shared_blks_hit AS hit, s.shared_blks_read AS read "
            "FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid "
            "WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND (%s::text[] IS NULL OR r.rolname = ANY(%s::text[])) "
            f"ORDER BY {by} DESC LIMIT %s",
            [roles, roles, limit],
        )
    except StoreError:
        raise
    except Exception as e:  # the extension missing, or not preloaded
        return {"available": False, "reason": f"{type(e).__name__}: {str(e).splitlines()[0]}"[:300], "statements": []}
    out = []
    for r in rows:
        blocks = (r["hit"] or 0) + (r["read"] or 0)
        out.append(
            {
                "role": r["role"],
                "query": r["query"],
                "calls": r["calls"],
                "total_ms": round(r["total_ms"], 2),
                "mean_ms": round(r["mean_ms"], 3),
                "max_ms": round(r["max_ms"], 2),
                "rows": r["rows"],
                "cache_hit": round(r["hit"] / blocks, 4) if blocks else None,
            }
        )
    return {"available": True, "statements": out}


def roles_of(tenant: str, owner: str) -> list[str]:
    return [owner, users_role(tenant)]
