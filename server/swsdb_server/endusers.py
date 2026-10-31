"""End users of a tenant (Supabase's Auth): sign-up and sign-in with email and password, and signed access tokens.

An app's users sign in once and send the token (`Authorization: Bearer <token>`). A request with it acts as that user:
- documents: search and answers see what the user may read (readers `user:<id>`, the user's groups, and `*`);
- tables: only through the Data API (/v1/rest), as the tenant's users role, so Postgres row-level security policies
  decide which rows; users never send SQL, so they can't change who they are;
- functions: /v1/rpc, as the same role; files: their own folder in Storage.

Passwords are hashed with scrypt. Tokens are HS256 JWTs signed with a per-tenant secret kept in swsdb's catalog
(so every server verifies them, and rotating the secret signs everyone out).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field

from .stores import Conflict, Forbidden, NotFound, SqlStore, StoreError

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{1,63}$")
GROUP = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,62}$")
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


@dataclass(frozen=True)
class EndUser:
    id: str
    email: str
    groups: tuple[str, ...] = field(default_factory=tuple)

    @property
    def principals(self) -> list[str]:
        return [f"user:{self.id}", *(f"group:{g}" for g in self.groups)]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(claims: dict, secret: bytes) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def peek(token: str) -> dict | None:
    """The claims of a token-shaped string, unverified (only to know which tenant's secret checks it)."""
    try:
        head, body, _ = token.split(".")
        if json.loads(_unb64(head)).get("alg") != "HS256":
            return None
        return json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def verify(token: str, secret: bytes) -> dict | None:
    try:
        head, body, sig = token.split(".")
    except ValueError:
        return None
    want = _b64(hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        return None
    claims = json.loads(_unb64(body))
    if claims.get("exp", 0) < time.time():
        return None
    return claims


class EndUsers:
    def __init__(self, sql: SqlStore):
        self.sql = sql
        self._config: dict[str, dict] = {}

    def forget(self) -> None:
        self._config.clear()

    async def config(self, tenant: str) -> dict:
        if tenant not in self._config:
            rows = await self.sql.rows(
                "SELECT secret, signup, token_ttl_s FROM swsdb.auth_config WHERE tenant = %s", [tenant]
            )
            if not rows:
                await self.sql.execute(
                    "INSERT INTO swsdb.auth_config (tenant, secret) VALUES (%s, %s) ON CONFLICT (tenant) DO NOTHING",
                    [tenant, secrets.token_bytes(32)],
                )
                rows = await self.sql.rows(
                    "SELECT secret, signup, token_ttl_s FROM swsdb.auth_config WHERE tenant = %s", [tenant]
                )
            r = rows[0]
            self._config[tenant] = {
                "secret": bytes(r["secret"]),
                "signup": r["signup"],
                "token_ttl_s": r["token_ttl_s"],
            }
        return self._config[tenant]

    async def settings(self, tenant: str, *, signup: bool | None, token_ttl_s: int | None) -> dict:
        await self.config(tenant)
        await self.sql.execute(
            "UPDATE swsdb.auth_config SET signup = coalesce(%s, signup), "
            "token_ttl_s = coalesce(%s, token_ttl_s) WHERE tenant = %s",
            [signup, token_ttl_s, tenant],
        )
        self._config.pop(tenant, None)
        c = await self.config(tenant)
        return {"signup": c["signup"], "token_ttl_s": c["token_ttl_s"]}

    async def rotate(self, tenant: str) -> None:
        """A new signing secret: every token issued so far stops working."""
        await self.config(tenant)
        await self.sql.execute(
            "UPDATE swsdb.auth_config SET secret = %s WHERE tenant = %s", [secrets.token_bytes(32), tenant]
        )
        self._config.pop(tenant, None)

    async def create(
        self, tenant: str, email: str, password: str, groups: list[str] | None = None, metadata: dict | None = None
    ) -> dict:
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise StoreError("that doesn't look like an email address")
        if len(password) < 8:
            raise StoreError("passwords need at least 8 characters")
        for g in groups or []:
            if not GROUP.match(g):
                raise StoreError(f"invalid group {g!r}: lowercase letters, digits, '_', '.', ':' or '-'")
        uid = str(uuid.uuid4())
        rows = await self.sql.rows(
            "INSERT INTO swsdb.end_users (tenant, id, email, password, groups, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant, email) DO NOTHING RETURNING id, email, groups, metadata, created_at",
            [tenant, uid, email, hash_password(password), list(groups or []), json.dumps(metadata or {})],
        )
        if not rows:
            raise Conflict(f"a user with {email} exists")
        return self._public(rows[0])

    @staticmethod
    def _public(r: dict) -> dict:
        out = {
            "id": str(r["id"]),
            "email": r["email"],
            "groups": list(r.get("groups") or []),
            "metadata": r.get("metadata") or {},
        }
        for k in ("created_at", "last_sign_in_at"):
            if k in r:
                out[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else r[k]
        return out

    async def sign_in(self, tenant: str, email: str, password: str) -> dict:
        rows = await self.sql.rows(
            "SELECT id, email, password, groups, metadata FROM swsdb.end_users "
            "WHERE tenant = %s AND email = %s AND disabled_at IS NULL",
            [tenant, email.strip().lower()],
        )
        if not rows or not check_password(password, rows[0]["password"]):
            hash_password(password)  # same time whether or not the user exists
            raise Forbidden("wrong email or password")
        u = rows[0]
        await self.sql.execute(
            "UPDATE swsdb.end_users SET last_sign_in_at = now() WHERE tenant = %s AND id = %s", [tenant, u["id"]]
        )
        return await self.token(tenant, u)

    async def token(self, tenant: str, u: dict) -> dict:
        c = await self.config(tenant)
        now = int(time.time())
        claims = {
            "sub": str(u["id"]),
            "email": u["email"],
            "groups": list(u["groups"] or []),
            "tenant": tenant,
            "role": "user",
            "iat": now,
            "exp": now + c["token_ttl_s"],
            "iss": "swsdb",
        }
        return {
            "access_token": sign(claims, c["secret"]),
            "token_type": "bearer",
            "expires_in": c["token_ttl_s"],
            "user": self._public(u),
        }

    async def resolve(self, token: str) -> tuple[str, EndUser] | None:
        """(tenant, user) for a valid token of an existing, enabled user."""
        claims = peek(token)
        if not claims or claims.get("iss") != "swsdb" or not isinstance(claims.get("tenant"), str):
            return None
        tenant = claims["tenant"]
        if not await self._has_config(tenant):
            return None
        good = verify(token, (await self.config(tenant))["secret"])
        if not good:
            return None
        rows = await self.sql.rows(
            "SELECT groups FROM swsdb.end_users WHERE tenant = %s AND id = %s AND disabled_at IS NULL",
            [tenant, good["sub"]],
        )
        if not rows:
            return None  # deleted or disabled since the token was issued
        return tenant, EndUser(good["sub"], good.get("email", ""), tuple(rows[0]["groups"] or []))

    async def _has_config(self, tenant: str) -> bool:
        if tenant in self._config:
            return True
        return bool(await self.sql.rows("SELECT 1 FROM swsdb.auth_config WHERE tenant = %s", [tenant]))

    async def list(self, tenant: str, limit: int = 200, offset: int = 0, q: str | None = None) -> dict:
        where, params = "tenant = %s", [tenant]
        if q:
            where += " AND email ILIKE %s"
            params.append("%" + q.replace("%", "\\%").replace("_", "\\_") + "%")
        rows = await self.sql.rows(
            f"SELECT id, email, groups, metadata, created_at, last_sign_in_at, disabled_at "  # noqa: S608
            f"FROM swsdb.end_users WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        total = (await self.sql.rows(f"SELECT count(*) AS n FROM swsdb.end_users WHERE {where}", params))[0]["n"]  # noqa: S608
        return {"users": [{**self._public(r), "disabled": r["disabled_at"] is not None} for r in rows], "total": total}

    async def update(
        self, tenant: str, uid: str, *, groups: list[str] | None, disabled: bool | None, password: str | None
    ) -> dict:
        for g in groups or []:
            if not GROUP.match(g):
                raise StoreError(f"invalid group {g!r}")
        if password is not None and len(password) < 8:
            raise StoreError("passwords need at least 8 characters")
        rows = await self.sql.rows(
            "UPDATE swsdb.end_users SET groups = coalesce(%s, groups), "
            "disabled_at = CASE WHEN %s IS NULL THEN disabled_at "
            "WHEN %s THEN coalesce(disabled_at, now()) ELSE NULL END, "
            "password = coalesce(%s, password) WHERE tenant = %s AND id = %s::uuid "
            "RETURNING id, email, groups, metadata, created_at, last_sign_in_at, disabled_at",
            [groups, disabled, disabled, hash_password(password) if password else None, tenant, uid],
        )
        if not rows:
            raise NotFound(f"no user {uid}")
        return {**self._public(rows[0]), "disabled": rows[0]["disabled_at"] is not None}

    async def delete(self, tenant: str, uid: str) -> None:
        if not await self.sql.rows(
            "DELETE FROM swsdb.end_users WHERE tenant = %s AND id = %s::uuid RETURNING id", [tenant, uid]
        ):
            raise NotFound(f"no user {uid}")
