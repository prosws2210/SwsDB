"""Webhooks: tell other systems when a document becomes searchable, fails, or is deleted.

Events are written in the same Postgres transaction as the change they describe, so an event is never lost and never
sent for a change that didn't happen. A worker delivers them (at least once) with retries, and signs each request:

    POST <url>
    swsdb-event: document.ready
    swsdb-delivery: 1234                 (the same on a retry: use it to ignore duplicates)
    swsdb-signature: sha256=<hex HMAC-SHA256 of the body with the webhook's secret>
    {"event": "document.ready", "tenant": ..., "collection": ..., "document": ..., "version": ..., "at": ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from urllib.parse import urlsplit

import httpx
from psycopg.types.json import Jsonb

from .stores import StoreError

log = logging.getLogger("swsdb.webhooks")

EVENTS = ("document.ready", "document.failed", "document.deleted")
MAX_ATTEMPTS = 12  # about 2 hours of retries with exponential backoff
LEASE_S = 60


def backoff(attempts: int) -> float:
    return min(2 ** attempts * 2, 1800)


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def check_url(url: str) -> str:
    """http(s) only, and never this machine or link-local addresses: a webhook must not become a way to reach the
    engines inside the container or a cloud metadata endpoint. Private networks (other services) are allowed."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise StoreError("a webhook URL must be http:// or https:// with a host")
    if parts.username or parts.password:
        raise StoreError("put credentials in the webhook's secret, not in its URL")
    for addr in _addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)):
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
            raise StoreError(f"webhook host {parts.hostname} resolves to {addr}, which webhooks may not reach")
    return url


def _addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise StoreError(f"webhook host {host} doesn't resolve: {e.strerror}") from None
    return [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]


async def emit(conn, tenant: str, collection: str, event: str, payload: dict) -> None:
    """Queue `event` for every matching webhook, on the caller's connection (inside its transaction)."""
    body = {"event": event, "tenant": tenant, "collection": collection, **payload,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    await conn.execute(
        "INSERT INTO swsdb.webhook_events (webhook_id, event, payload) SELECT id, %s, %s FROM swsdb.webhooks "
        "WHERE tenant = %s AND disabled_at IS NULL AND %s = ANY(events) "
        "AND (collections IS NULL OR %s = ANY(collections))",
        [event, Jsonb(body), tenant, event, collection])


class Webhooks:
    """Webhook registrations and the delivery worker."""

    def __init__(self, docs):
        self.d = docs
        self.http = httpx.AsyncClient(timeout=10, follow_redirects=False,
                                      headers={"user-agent": "swsdb-webhooks", "content-type": "application/json"})
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ registrations

    async def create(self, tenant: str, url: str, events: list[str], collections: list[str] | None,
                     secret: str | None) -> dict:
        unknown = sorted(set(events) - set(EVENTS))
        if not events or unknown:
            raise StoreError(f"events must be some of {', '.join(EVENTS)}" + (f" (not {unknown})" if unknown else ""))
        await asyncio.to_thread(check_url, url)
        wid = "wh_" + secrets.token_hex(6)
        secret = secret or secrets.token_urlsafe(32)
        row = await self.d.one("INSERT INTO swsdb.webhooks (id, tenant, url, events, collections, secret) "
                               "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                               [wid, tenant, url, sorted(set(events)), collections, secret])
        return {**public(row), "secret": secret}

    async def list(self, tenant: str) -> list[dict]:
        rows = await self.d.rows("""
            SELECT w.*, count(e.*) FILTER (WHERE e.delivered_at IS NOT NULL) AS delivered,
                   count(e.*) FILTER (WHERE e.failed_at IS NOT NULL) AS failed,
                   count(e.*) FILTER (WHERE e.delivered_at IS NULL AND e.failed_at IS NULL) AS pending
            FROM swsdb.webhooks w LEFT JOIN swsdb.webhook_events e ON e.webhook_id = w.id
            WHERE w.tenant = %s AND w.disabled_at IS NULL GROUP BY w.id ORDER BY w.created_at""", [tenant])
        return [{**public(r), "delivered": r["delivered"], "failed": r["failed"], "pending": r["pending"]}
                for r in rows]

    async def delete(self, tenant: str, wid: str) -> bool:
        return bool(await self.d.rows("DELETE FROM swsdb.webhooks WHERE id = %s AND tenant = %s RETURNING id",
                                      [wid, tenant]))

    async def deliveries(self, tenant: str, wid: str, limit: int = 50) -> list[dict]:
        rows = await self.d.rows("""
            SELECT e.id, e.event, e.payload, e.attempts, e.last_status, e.last_error, e.created_at, e.delivered_at,
                   e.failed_at, e.next_attempt_at
            FROM swsdb.webhook_events e JOIN swsdb.webhooks w ON w.id = e.webhook_id
            WHERE w.id = %s AND w.tenant = %s ORDER BY e.id DESC LIMIT %s""", [wid, tenant, limit])
        out = []
        for r in rows:
            state = "delivered" if r["delivered_at"] else "failed" if r["failed_at"] else "pending"
            out.append({"id": r["id"], "event": r["event"], "state": state, "attempts": r["attempts"],
                        "last_status": r["last_status"], "last_error": r["last_error"],
                        "payload": r["payload"], "created_at": r["created_at"].isoformat()})
        return out

    async def ping(self, tenant: str, wid: str) -> dict:
        """Queue a `ping` event to one webhook, to check the receiving end."""
        async with self.d.sql.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO swsdb.webhook_events (webhook_id, event, payload) SELECT id, 'ping', %s "
                "FROM swsdb.webhooks WHERE id = %s AND tenant = %s RETURNING id",
                [Jsonb({"event": "ping", "tenant": tenant, "webhook": wid,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), wid, tenant])
            row = await cur.fetchone()
        if row is None:
            from .stores import NotFound
            raise NotFound(f"webhook {wid!r} not found")
        self.wake()
        return {"queued": row[0]}

    # ------------------------------------------------------------------ delivery

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="swsdb-webhooks")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self.http.aclose()

    async def _loop(self) -> None:
        last_cleanup = 0.0
        while True:
            try:
                batch = await self.d.rows(f"""
                    WITH c AS (SELECT id FROM swsdb.webhook_events
                               WHERE delivered_at IS NULL AND failed_at IS NULL AND next_attempt_at <= now()
                                 AND (locked_until IS NULL OR locked_until < now())
                               ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED)
                    UPDATE swsdb.webhook_events e SET locked_until = now() + interval '{LEASE_S} seconds',
                           attempts = e.attempts + 1
                    FROM c, swsdb.webhooks w WHERE e.id = c.id AND w.id = e.webhook_id
                    RETURNING e.id, e.event, e.payload, e.attempts, w.url, w.secret""")  # noqa: S608 - a constant
                if batch:
                    await asyncio.gather(*(self._deliver(ev) for ev in batch))
                    continue
                if time.monotonic() - last_cleanup > 3600:  # keep a week of history
                    last_cleanup = time.monotonic()
                    await self.d.rows("DELETE FROM swsdb.webhook_events WHERE created_at < now() - interval '7 days' "
                                      "AND (delivered_at IS NOT NULL OR failed_at IS NOT NULL)")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Postgres restarting: try again shortly
                log.debug("webhook loop: %s", e)
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)

    async def _deliver(self, ev: dict) -> None:
        body = json.dumps(ev["payload"], separators=(",", ":"), sort_keys=True).encode()
        headers = {"swsdb-event": ev["event"], "swsdb-delivery": str(ev["id"]),
                   "swsdb-signature": signature(ev["secret"], body)}
        status, error = None, None
        try:
            await asyncio.to_thread(check_url, ev["url"])  # again now: DNS may point elsewhere than at creation
            r = await self.http.post(ev["url"], content=body, headers=headers)
            status = r.status_code
            if not 200 <= r.status_code < 300:
                error = f"answered {r.status_code}"
        except (httpx.HTTPError, StoreError) as e:
            error = f"{type(e).__name__}: {e}"[:500]
        if error is None:
            await self.d.rows("UPDATE swsdb.webhook_events SET delivered_at = now(), last_status = %s, "
                              "last_error = NULL, locked_until = NULL WHERE id = %s", [status, ev["id"]])
            return
        give_up = ev["attempts"] >= MAX_ATTEMPTS
        log.info("webhook delivery %s (%s) failed, attempt %d%s: %s", ev["id"], ev["event"], ev["attempts"],
                 ", giving up" if give_up else "", error)
        await self.d.rows(
            "UPDATE swsdb.webhook_events SET last_status = %s, last_error = %s, locked_until = NULL, "
            "failed_at = CASE WHEN %s THEN now() END, next_attempt_at = now() + make_interval(secs => %s) "
            "WHERE id = %s", [status, error, give_up, backoff(ev["attempts"]), ev["id"]])


def public(row: dict) -> dict:
    return {"id": row["id"], "tenant": row["tenant"], "url": row["url"], "events": row["events"],
            "collections": row["collections"], "created_at": row["created_at"].isoformat()}
