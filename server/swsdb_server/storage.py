"""Storage: files in buckets, per tenant (Supabase's Storage), kept in Postgres so snapshots, restores and several
servers include them with everything else.

Who may do what:
- the admin key: everything in its tenant; the read key: list and download;
- a signed-in end user: list, upload, download and delete in their own folder (`<user id>/...`) of any bucket, and
  list and download everything in a public bucket. "Public" means every key and user of the tenant, never anonymous.

Downloads are served as attachments, with nosniff and a sandboxing Content-Security-Policy, except common images, so an
uploaded HTML or SVG file can never run as a page of the console's origin.
"""

from __future__ import annotations

import hashlib
import re

from .names import NAME
from .stores import Conflict, Forbidden, NotFound, SqlStore, StoreError

PATH = re.compile(r"^(?!/)(?!.*//)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._ ()@+=,!~-][A-Za-z0-9._ ()@+=,!~/-]{0,511}$")
INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}


class Storage:
    def __init__(self, sql: SqlStore, max_bytes: int):
        self.sql = sql
        self.max_bytes = max_bytes

    @staticmethod
    def _bucket_name(name: str) -> str:
        if not NAME.match(name) or name == "buckets":
            raise StoreError(f"invalid bucket name {name!r}: lowercase letters, digits, '-' or '_'")
        return name

    @staticmethod
    def _path(path: str) -> str:
        if not PATH.match(path or ""):
            raise StoreError(f"invalid path {path!r}: up to 512 characters, folders separated by '/', no '..'")
        return path

    @staticmethod
    def _own(caller, path: str) -> bool:
        return caller.role != "user" or path.startswith(f"{caller.user.id}/")

    async def _bucket(self, tenant: str, name: str) -> dict:
        rows = await self.sql.rows(
            "SELECT name, public, created_at FROM swsdb.storage_buckets WHERE tenant = %s AND name = %s",
            [tenant, self._bucket_name(name)],
        )
        if not rows:
            raise NotFound(f"no bucket {name!r}")
        return rows[0]

    async def buckets(self, tenant: str) -> list[dict]:
        rows = await self.sql.rows(
            "SELECT b.name, b.public, b.created_at, count(o.path) AS objects, coalesce(sum(o.size), 0) AS bytes "
            "FROM swsdb.storage_buckets b LEFT JOIN swsdb.storage_objects o "
            "ON o.tenant = b.tenant AND o.bucket = b.name "
            "WHERE b.tenant = %s GROUP BY b.name, b.public, b.created_at ORDER BY b.name",
            [tenant],
        )
        return [{**r, "created_at": r["created_at"].isoformat(), "bytes": int(r["bytes"])} for r in rows]

    async def create_bucket(self, tenant: str, name: str, public: bool) -> dict:
        rows = await self.sql.rows(
            "INSERT INTO swsdb.storage_buckets (tenant, name, public) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING name",
            [tenant, self._bucket_name(name), public],
        )
        if not rows:
            raise Conflict(f"bucket {name!r} exists")
        return {"name": name, "public": public}

    async def update_bucket(self, tenant: str, name: str, public: bool) -> dict:
        await self._bucket(tenant, name)
        await self.sql.execute(
            "UPDATE swsdb.storage_buckets SET public = %s WHERE tenant = %s AND name = %s", [public, tenant, name]
        )
        return {"name": name, "public": public}

    async def delete_bucket(self, tenant: str, name: str, force: bool) -> dict:
        b = await self._bucket(tenant, name)
        n = (
            await self.sql.rows(
                "SELECT count(*) AS n FROM swsdb.storage_objects WHERE tenant = %s AND bucket = %s", [tenant, b["name"]]
            )
        )[0]["n"]
        if n and not force:
            raise Conflict(f"bucket {name!r} holds {n} files: delete them first, or pass force=true")
        await self.sql.execute("DELETE FROM swsdb.storage_buckets WHERE tenant = %s AND name = %s", [tenant, name])
        return {"deleted": name, "files": n}

    async def list(self, tenant: str, caller, bucket: str, prefix: str = "", limit: int = 500) -> dict:
        b = await self._bucket(tenant, bucket)
        if caller.role == "user" and not b["public"]:
            prefix = prefix if prefix.startswith(f"{caller.user.id}/") else f"{caller.user.id}/"
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await self.sql.rows(
            "SELECT path, content_type, size, sha256, owner, created_at, updated_at FROM swsdb.storage_objects "
            "WHERE tenant = %s AND bucket = %s AND path LIKE %s ORDER BY path LIMIT %s",
            [tenant, bucket, like, limit],
        )
        return {
            "bucket": bucket,
            "public": b["public"],
            "prefix": prefix,
            "objects": [
                {**r, "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}
                for r in rows
            ],
        }

    async def put(self, tenant: str, caller, bucket: str, path: str, data: bytes, content_type: str | None) -> dict:
        if caller.role == "read":
            raise Forbidden("the read-only key can't upload")
        await self._bucket(tenant, bucket)
        self._path(path)
        if not self._own(caller, path):
            raise Forbidden(f"signed-in users upload into their own folder: {caller.user.id}/...")
        if len(data) > self.max_bytes:
            raise StoreError(f"the file is larger than {self.max_bytes // (1024 * 1024)} MB (SWSDB_MAX_UPLOAD_MB)")
        ctype = (content_type or "application/octet-stream").split(";")[0].strip().lower()[
            :100
        ] or "application/octet-stream"
        owner = caller.user.id if caller.role == "user" else None
        sha = hashlib.sha256(data).hexdigest()
        await self.sql.execute(
            "INSERT INTO swsdb.storage_objects (tenant, bucket, path, content_type, size, sha256, data, owner) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant, bucket, path) DO UPDATE SET "
            "content_type = EXCLUDED.content_type, size = EXCLUDED.size, sha256 = EXCLUDED.sha256, "
            "data = EXCLUDED.data, "
            "owner = coalesce(swsdb.storage_objects.owner, EXCLUDED.owner), updated_at = now()",
            [tenant, bucket, path, ctype, len(data), sha, data, owner],
        )
        return {"bucket": bucket, "path": path, "size": len(data), "content_type": ctype, "sha256": sha}

    async def get(self, tenant: str, caller, bucket: str, path: str) -> tuple[bytes, str, dict]:
        b = await self._bucket(tenant, bucket)
        self._path(path)
        if not b["public"] and not self._own(caller, path):
            raise NotFound(f"no file {path!r} in {bucket!r}")  # not "forbidden": don't say it exists
        rows = await self.sql.rows(
            "SELECT data, content_type, sha256 FROM swsdb.storage_objects WHERE tenant = %s "
            "AND bucket = %s AND path = %s",
            [tenant, bucket, path],
        )
        if not rows:
            raise NotFound(f"no file {path!r} in {bucket!r}")
        r = rows[0]
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.rsplit("/", 1)[-1])[:120] or "file"
        inline = r["content_type"] in INLINE
        headers = {
            "x-content-type-options": "nosniff",
            "etag": f'"{r["sha256"]}"',
            "content-security-policy": "default-src 'none'; sandbox",
            "content-disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
        }
        return bytes(r["data"]), r["content_type"] if inline else "application/octet-stream", headers

    async def delete(self, tenant: str, caller, bucket: str, path: str) -> dict:
        if caller.role == "read":
            raise Forbidden("the read-only key can't delete")
        await self._bucket(tenant, bucket)
        self._path(path)
        if not self._own(caller, path):
            raise Forbidden("signed-in users delete in their own folder only")
        rows = await self.sql.rows(
            "DELETE FROM swsdb.storage_objects WHERE tenant = %s AND bucket = %s AND path = %s RETURNING path",
            [tenant, bucket, path],
        )
        if not rows:
            raise NotFound(f"no file {path!r} in {bucket!r}")
        return {"deleted": path}
