"""A small S3 client (AWS Signature Version 4) for backups: list, upload (multipart), download, delete.

Works with AWS S3 and S3-compatible stores (MinIO, Cloudflare R2, Backblaze B2, Wasabi, Ceph), using path-style URLs
(`<endpoint>/<bucket>/<key>`), which all of them accept. Payloads are sent unsigned (`UNSIGNED-PAYLOAD`), so large
files stream without being read twice; use an https endpoint.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

PART = 64 * 1024 * 1024  # multipart chunk: S3 allows 5 MiB to 5 GiB per part, up to 10,000 parts
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(RuntimeError):
    pass


def _xml(text: str) -> ET.Element:
    # Replies come from the operator's own storage endpoint; ElementTree doesn't resolve external entities and the
    # bundled expat limits entity expansion.
    return ET.fromstring(text)  # noqa: S314


def _h(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _read(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(size)


class S3:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1",
                 prefix: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.host = urlsplit(self.endpoint).netloc
        self.bucket, self.region = bucket, region
        self.access_key, self.secret_key = access_key, secret_key
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, read=600))

    async def close(self) -> None:
        await self.http.aclose()

    def _sign(self, method: str, path: str, query: dict[str, str], headers: dict[str, str]) -> dict[str, str]:
        now = dt.datetime.now(dt.UTC)
        amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        headers = {**headers, "host": self.host, "x-amz-date": amz_date, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
        signed = sorted(headers)
        canonical = "\n".join([
            method, quote(path, safe="/-_.~"),
            "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items())),
            "".join(f"{k}:{headers[k].strip()}\n" for k in signed), ";".join(signed), "UNSIGNED-PAYLOAD"])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        key = _h(_h(_h(_h(("AWS4" + self.secret_key).encode(), day), self.region), "s3"), "aws4_request")
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                                    f"SignedHeaders={';'.join(signed)}, Signature={signature}")
        return headers

    def _url(self, key: str | None) -> tuple[str, str]:
        path = f"/{self.bucket}" + (f"/{key}" if key is not None else "")
        return path, self.endpoint + quote(path, safe="/-_.~")

    async def _req(self, method: str, key: str | None, query: dict[str, str] | None = None, content=None,
                   headers: dict[str, str] | None = None, ok: tuple[int, ...] = (200,)) -> httpx.Response:
        path, url = self._url(key)
        query = query or {}
        h = self._sign(method, path, query, {k.lower(): v for k, v in (headers or {}).items()})
        try:
            r = await self.http.request(method, url, params=query or None, content=content, headers=h)
        except httpx.HTTPError as e:
            raise S3Error(f"S3 endpoint {self.endpoint} unreachable: {type(e).__name__}") from e
        if r.status_code not in ok:
            msg = r.text[:400]
            try:
                msg = _xml(r.text).findtext("Message") or msg
            except ET.ParseError:
                pass
            raise S3Error(f"S3 {method} {key or self.bucket} answered {r.status_code}: {msg}")
        return r

    async def check(self) -> None:
        """The bucket exists and these credentials can list it."""
        await self._req("GET", None, {"list-type": "2", "max-keys": "1"})

    async def list(self) -> list[dict]:
        out, token = [], None
        while True:
            q = {"list-type": "2", "prefix": self.prefix}
            if token:
                q["continuation-token"] = token
            root = _xml((await self._req("GET", None, q)).text)
            for c in root.iter(f"{NS}Contents"):
                out.append({"key": c.findtext(f"{NS}Key"), "bytes": int(c.findtext(f"{NS}Size") or 0),
                            "modified": c.findtext(f"{NS}LastModified")})
            token = root.findtext(f"{NS}NextContinuationToken")
            if not token:
                return out

    async def upload(self, path: Path, name: str) -> str:
        key = self.prefix + name
        size = (await asyncio.to_thread(path.stat)).st_size
        if size <= PART:
            data = await asyncio.to_thread(path.read_bytes)
            await self._req("PUT", key, content=data, headers={"content-type": "application/x-tar"})
            return key
        root = _xml((await self._req("POST", key, {"uploads": ""}, headers={"content-type": "application/x-tar"})).text)
        upload_id = root.findtext(f"{NS}UploadId") or root.findtext("UploadId")
        parts = []
        try:
            for n, offset in enumerate(range(0, size, PART), start=1):
                chunk = await asyncio.to_thread(_read, path, offset, PART)
                r = await self._req("PUT", key, {"partNumber": str(n), "uploadId": upload_id}, content=chunk)
                parts.append(f"<Part><PartNumber>{n}</PartNumber><ETag>{r.headers['etag']}</ETag></Part>")
            body = f"<CompleteMultipartUpload>{''.join(parts)}</CompleteMultipartUpload>".encode()
            await self._req("POST", key, {"uploadId": upload_id}, content=body)
        except BaseException:
            try:
                await self._req("DELETE", key, {"uploadId": upload_id}, ok=(200, 204))
            except S3Error:
                pass
            raise
        return key

    async def download(self, key: str, dest: Path) -> int:
        path, url = self._url(key)
        tmp = dest.with_name(dest.name + ".part")
        n = 0
        try:
            async with self.http.stream("GET", url, headers=self._sign("GET", path, {}, {})) as r:
                if r.status_code != 200:
                    raise S3Error(f"S3 GET {key} answered {r.status_code}")
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        n += len(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return n

    async def delete(self, key: str) -> None:
        await self._req("DELETE", key, ok=(200, 204))
