"""Who is calling: API keys from the environment (every tenant) and managed keys bound to one tenant.

Managed keys look like `swsdb_<id>_<secret>`. Only a SHA-256 of the whole key is stored; the id finds the row, and
the comparison is constant-time. Keys are long random strings, so a slow password hash would add nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass

from .stores import SqlStore, Unavailable

log = logging.getLogger("swsdb.auth")

KEY = re.compile(r"^swsdb_([0-9a-f]{10})_[A-Za-z0-9_-]{30,}$")
ROLES = ("admin", "read")


@dataclass(frozen=True)
class Caller:
    role: str  # "admin" (read and write) or "read" (read and search only)
    tenant: str | None  # None: every tenant (keys from the environment, or no keys configured)
    key_id: str | None = None  # a managed key's id

    @property
    def is_global(self) -> bool:
        return self.tenant is None


def new_key() -> tuple[str, str]:
    """(id, full key)."""
    kid = secrets.token_hex(5)
    return kid, f"swsdb_{kid}_{secrets.token_urlsafe(32)}"


def digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


class KeyStore:
    """Managed keys, cached in memory. Other servers on the same database are told about every change
    (`changed`, a cluster notification), and drop their cache when they hear it."""

    TOUCH_EVERY_S = 60.0

    def __init__(self, sql: SqlStore | None):
        self.sql = sql
        self._rows: dict[str, dict] | None = None
        self._lock = asyncio.Lock()
        self._touched: dict[str, float] = {}
        self.changed = None  # async callback after a create or revoke

    def forget(self) -> None:
        self._rows = None

    async def _load(self) -> dict[str, dict]:
        if self._rows is not None:
            return self._rows
        if self.sql is None:
            self._rows = {}
            return self._rows
        async with self._lock:
            if self._rows is None:
                await self.sql.migrate()
                rows = await self.sql.rows("SELECT id, hash, tenant, role FROM swsdb.api_keys "
                                           "WHERE revoked_at IS NULL")
                self._rows = {r["id"]: r for r in rows}
        return self._rows

    async def any_active(self) -> bool:
        return bool(await self._load())

    def has_any_cached(self) -> bool:
        """Managed keys known without asking Postgres (unknown yet counts as none: the stricter answer here)."""
        return bool(self._rows)

    async def resolve(self, key: str) -> Caller | None:
        m = KEY.match(key)
        if not m:
            return None
        row = (await self._load()).get(m.group(1))
        if row is None or not hmac.compare_digest(bytes(row["hash"]), digest(key)):
            return None
        now = time.monotonic()
        if now - self._touched.get(row["id"], 0) > self.TOUCH_EVERY_S:  # last use, at most once a minute
            self._touched[row["id"]] = now
            asyncio.get_running_loop().create_task(self._touch(row["id"]))
        return Caller(row["role"], row["tenant"], row["id"])

    async def _touch(self, kid: str) -> None:
        try:
            await self.sql.execute("UPDATE swsdb.api_keys SET last_used_at = now() WHERE id = %s", [kid])
        except Exception as e:  # bookkeeping only
            log.debug("could not record use of key %s: %s", kid, e)

    async def create(self, tenant: str, role: str, name: str | None) -> dict:
        if self.sql is None:
            raise Unavailable("managed API keys are stored in PostgreSQL, which is not enabled")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        kid, key = new_key()
        row = (await self.sql.rows(
            "INSERT INTO swsdb.api_keys (id, hash, tenant, role, name) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, tenant, role, name, created_at", [kid, digest(key), tenant, role, name]))[0]
        (await self._load())[kid] = {"id": kid, "hash": digest(key), "tenant": tenant, "role": role}
        if self.changed:
            await self.changed()
        return {**public(row), "key": key}

    async def list(self, tenant: str | None) -> list[dict]:
        if self.sql is None:
            return []
        where, params = ("", []) if tenant is None else ("WHERE tenant = %s", [tenant])
        rows = await self.sql.rows("SELECT id, tenant, role, name, created_at, last_used_at, revoked_at "  # noqa: S608
                                   f"FROM swsdb.api_keys {where} ORDER BY created_at", params)
        return [public(r) for r in rows]

    async def revoke(self, kid: str, tenant: str | None) -> bool:
        if self.sql is None:
            return False
        where, params = ("id = %s", [kid]) if tenant is None else ("id = %s AND tenant = %s", [kid, tenant])
        rows = await self.sql.rows(f"UPDATE swsdb.api_keys SET revoked_at = now() WHERE {where} "  # noqa: S608
                                   "AND revoked_at IS NULL RETURNING id", params)
        (await self._load()).pop(kid, None)
        if rows and self.changed:
            await self.changed()
        return bool(rows)


def public(row: dict) -> dict:
    out = {k: row.get(k) for k in ("id", "tenant", "role", "name")}
    for k in ("created_at", "last_used_at", "revoked_at"):
        if k in row:
            out[k] = row[k].isoformat() if row[k] else None
    return out
