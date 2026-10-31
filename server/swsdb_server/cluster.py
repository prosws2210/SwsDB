"""Several swsdb servers on the same engines (ADR 0006).

Everything durable is already in Postgres, and outbox claims use FOR UPDATE SKIP LOCKED, so servers share the work.
What a server kept to itself is coordinated here, through the database they share:

- who is running: a heartbeat row per server (`swsdb.servers`), shown in /v1/status;
- what changed: LISTEN/NOTIFY on `swsdb_events`, so a key revoked on one server is refused by all of them at once, and
  a restore resets every server's caches (a server that loses the connection resets them too, as it may have missed
  events);
- when the stores must hold still: an advisory lock. Every change to the vector or graph store (an outbox item, a
  direct write) holds it shared; a snapshot or restore takes it exclusively, so it waits for changes in flight on
  every server and keeps new ones out until it is done. The lock lives in the `postgres` database because a restore
  drops and recreates the swsdb one;
- jobs one server should run: scheduled backups and periodic reconciliation go to the server holding the leader
  lock (another one takes over when it stops).

Uploaded originals must be shared too: SWSDB_FILES=postgres keeps them in Postgres instead of the server's volume.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from . import __version__
from .stores import SqlStore, Unavailable

log = logging.getLogger("swsdb.cluster")

FENCE = 7367736470  # shared: a change to the derived stores is running; exclusive: a snapshot or restore
LEADER = 7367736471
CHANNEL = "swsdb_events"
HEARTBEAT_S = 5.0
ALIVE_S = 20.0  # a server not seen for this long is gone (its claimed work can be taken over)


def server_id(configured: str | None = None) -> str:
    return (configured or os.environ.get("SWSDB_SERVER_ID") or socket.gethostname() or "swsdb")[:100]


def volume(server: str) -> str:
    """The server whose data volume a server (or one of its worker processes, `<server>/<pid>`) uses."""
    return server.split("/", 1)[0]


class Cluster:
    def __init__(self, sql: SqlStore | None, *, files: str = "disk", profile: str = "default", fence_size: int = 8,
                 name: str | None = None):
        self.sql = sql if isinstance(sql, SqlStore) else None  # (tests pass stand-ins: no shared database then)
        self.id = server_id(name)
        self.files, self.profile = files, profile
        self.leader = self.sql is None  # no shared database: this server is alone
        self.peers: list[dict] = []
        self.handlers: list = []  # async callbacks(event: dict) for events from other servers (and "reset")
        self._tasks: list[asyncio.Task] = []
        self._lead_conn: psycopg.AsyncConnection | None = None
        self._fence: AsyncConnectionPool | None = None
        if self.sql is not None:
            self._fence = AsyncConnectionPool(conninfo=sql.admin_conninfo("postgres"), min_size=0,
                                              max_size=fence_size, open=False, kwargs={"autocommit": True})

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.sql is None:
            return
        await self._fence.open(wait=False)
        self._tasks = [asyncio.create_task(self._heartbeat(), name="swsdb-cluster-heartbeat"),
                       asyncio.create_task(self._listen(), name="swsdb-cluster-events"),
                       asyncio.create_task(self._lead(), name="swsdb-cluster-leader")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._lead_conn is not None:
            with contextlib.suppress(Exception):
                await self._lead_conn.close()
        if self._fence is not None:
            await self._fence.close()
        if self.sql is not None:
            with contextlib.suppress(Exception):
                await self.sql.execute("DELETE FROM swsdb.servers WHERE id = %s", [self.id])

    def status(self) -> dict:
        # worker processes of one server are named <server>/<pid> (SWSDB_WORKERS): they share its volume
        others = [p for p in self.peers if volume(p["id"]) != volume(self.id)]
        out = {"server": self.id, "leader": self.leader, "files": self.files,
               "servers": [{k: p[k] for k in ("id", "version", "files", "seen_s")} for p in self.peers]}
        if others and any(p["files"] != "postgres" for p in self.peers):
            out["warning"] = ("several servers share this database but not every one keeps uploads in Postgres: "
                              "set SWSDB_FILES=postgres on all of them")
        return out

    # ------------------------------------------------------------------ the fence

    @asynccontextmanager
    async def changing(self, wait: bool = True):
        """Around one change to the vector or graph store. `wait=False` refuses (503) instead of waiting while a
        snapshot runs, for API requests."""
        if self._fence is None:
            yield
            return
        # a transaction-level lock: it ends with the transaction however that ends (commit, an error, a cancelled
        # task whose connection the pool rolls back or discards), so a lock can never outlive its change
        fn = "pg_advisory_xact_lock_shared" if wait else "pg_try_advisory_xact_lock_shared"
        try:
            cm = self._fence.connection(timeout=30)
            conn = await cm.__aenter__()
        except PoolTimeout as e:
            raise Unavailable("PostgreSQL: no connection available for the write fence") from e
        except psycopg.OperationalError as e:
            raise Unavailable(f"PostgreSQL: {e}") from e
        try:
            async with conn.transaction():
                try:
                    got = (await (await conn.execute(f"SELECT {fn}(%s)", [FENCE])).fetchone())[0]
                except psycopg.OperationalError as e:
                    raise Unavailable(f"PostgreSQL: {e}") from e
                if got is False:
                    raise Unavailable("a snapshot or restore is running on another swsdb server; this write can be "
                                      "retried in a few seconds")
                yield
        except BaseException as e:
            await cm.__aexit__(type(e), e, e.__traceback__)
            raise
        else:
            await cm.__aexit__(None, None, None)

    @asynccontextmanager
    async def exclusive(self, timeout_s: float = 120):
        """The stores hold still on every server: changes in flight finish, new ones wait."""
        if self.sql is None:
            yield
            return
        conn = await psycopg.AsyncConnection.connect(self.sql.admin_conninfo("postgres"), autocommit=True)
        try:
            await conn.execute(f"SET lock_timeout = '{int(timeout_s * 1000)}ms'")
            try:
                await conn.execute("SELECT pg_advisory_lock(%s)", [FENCE])
            except psycopg.errors.LockNotAvailable as e:
                raise Unavailable(f"changes were still running on other servers after {timeout_s:g}s") from e
            yield
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    # ------------------------------------------------------------------ events

    async def notify(self, kind: str, **data) -> None:
        if self.sql is None:
            return
        with contextlib.suppress(Exception):  # a missed event is caught by the reset on reconnect
            await self.sql.execute("SELECT pg_notify(%s, %s)",
                                   [CHANNEL, json.dumps({"kind": kind, "from": self.id, **data})])

    async def _dispatch(self, event: dict) -> None:
        for h in self.handlers:
            try:
                await h(event)
            except Exception as e:
                log.warning("handling %s event failed: %s", event.get("kind"), e)

    async def _listen(self) -> None:
        connected_before = False
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.sql.admin_conninfo(self.sql.dbname),
                                                                 autocommit=True, connect_timeout=5) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    if connected_before:  # events may have been missed (or the database restored) meanwhile
                        await self._dispatch({"kind": "reset", "from": None})
                    connected_before = True
                    async for n in conn.notifies():
                        try:
                            event = json.loads(n.payload)
                        except ValueError:
                            continue
                        if event.get("from") != self.id:
                            await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("event connection: %s", e)
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ heartbeat and leader

    async def _heartbeat(self) -> None:
        while True:
            try:
                await self.sql.execute(
                    "INSERT INTO swsdb.servers (id, version, files, profile) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET seen_at = now(), version = EXCLUDED.version, "
                    "files = EXCLUDED.files, profile = EXCLUDED.profile",
                    [self.id, __version__, self.files, self.profile])
                rows = await self.sql.rows(
                    "SELECT id, version, files, extract(epoch FROM now() - seen_at) AS seen_s FROM swsdb.servers "
                    "WHERE seen_at > now() - make_interval(secs => %s) ORDER BY id", [ALIVE_S])
                self.peers = [{**r, "seen_s": round(float(r["seen_s"]), 1)} for r in rows]
                await self.sql.execute("DELETE FROM swsdb.servers WHERE seen_at < now() - interval '1 day'")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("heartbeat: %s", e)
            await asyncio.sleep(HEARTBEAT_S)

    async def _lead(self) -> None:
        while True:
            try:
                if self._lead_conn is None or self._lead_conn.closed:
                    self.leader = False
                    self._lead_conn = await psycopg.AsyncConnection.connect(
                        self.sql.admin_conninfo("postgres"), autocommit=True, connect_timeout=5)
                if not self.leader:
                    self.leader = bool((await (await self._lead_conn.execute(
                        "SELECT pg_try_advisory_lock(%s)", [LEADER])).fetchone())[0])
                    if self.leader:
                        log.info("this server (%s) runs the scheduled jobs", self.id)
                else:
                    await self._lead_conn.execute("SELECT 1")  # still holding it
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("leader connection: %s", e)
                self.leader = False
                if self._lead_conn is not None:
                    with contextlib.suppress(Exception):
                        await self._lead_conn.close()
                self._lead_conn = None
            await asyncio.sleep(HEARTBEAT_S)
