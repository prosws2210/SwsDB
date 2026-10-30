"""Watches the engines inside the container and reports their state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

import httpx
import psycopg
import redis.asyncio as aioredis

from .engines import ENGINES
from .settings import Settings

log = logging.getLogger("swsdb.health")

State = Literal["starting", "ready", "down", "failed"]


@dataclass
class EngineStatus:
    capability: str
    engine: str
    state: State = "starting"
    version: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    since: float = field(default_factory=time.time)
    restarts_seen: int = 0  # times it went from ready to down while this server watched

    def public(self) -> dict:
        d = asdict(self)
        d["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.since))
        return d


def read_secrets(s: Settings) -> dict[str, str]:
    return json.loads(s.secrets_file.read_text(encoding="utf-8"))


class Probes:
    """One check per engine: reachable, answering, and which version."""

    def __init__(self, s: Settings, secret: dict[str, str]):
        self.s, self.secret = s, secret
        self.http = httpx.AsyncClient(timeout=3.0, headers={"api-key": secret.get("qdrant_api_key", "")})
        self.redis = aioredis.Redis(host="127.0.0.1", port=6379, password=secret.get("falkordb_password"),
                                    socket_timeout=3, socket_connect_timeout=3)
        self._db_ready = False

    async def sql(self) -> str:
        async with await psycopg.AsyncConnection.connect(
                host=str(self.s.pg_socket_dir), dbname="postgres", user="swsdb", connect_timeout=3,
                autocommit=True) as conn:
            version = (await (await conn.execute("SHOW server_version")).fetchone())[0]
            if not self._db_ready:  # the database swsdb keeps its own tables in
                exists = await (await conn.execute("SELECT 1 FROM pg_database WHERE datname = 'swsdb'")).fetchone()
                if not exists:
                    await conn.execute("CREATE DATABASE swsdb")
                    log.info("created database swsdb")
                self._db_ready = True
            return str(version).split()[0]

    async def vector(self) -> str:
        r = await self.http.get("http://127.0.0.1:6333/readyz")
        r.raise_for_status()
        info = (await self.http.get("http://127.0.0.1:6333/")).json()
        return str(info.get("version", "?"))

    async def graph(self) -> str:
        await self.redis.ping()
        for m in await self.redis.module_list():
            name = m.get(b"name") or m.get("name")
            if name in (b"graph", "graph"):
                v = int(m.get(b"ver") or m.get("ver"))
                return f"{v // 10000}.{v // 100 % 100}.{v % 100}"
        raise RuntimeError("the FalkorDB module is not loaded")

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()


class Monitor:
    def __init__(self, s: Settings):
        self.s = s
        self.started = time.time()
        self.status: dict[str, EngineStatus] = {
            c: EngineStatus(capability=c, engine=ENGINES[c].product) for c in s.engines}
        self._task: asyncio.Task | None = None
        self.probes: Probes | None = None

    @property
    def ready(self) -> bool:
        return all(st.state == "ready" for st in self.status.values())

    def overall(self) -> str:
        states = {st.state for st in self.status.values()}
        if states <= {"ready"}:
            return "ready"
        if "failed" in states or "down" in states:
            return "degraded"
        return "starting"

    async def check(self, cap: str) -> None:
        st = self.status[cap]
        t0 = time.perf_counter()
        try:
            version = await asyncio.wait_for(getattr(self.probes, cap)(), timeout=5)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}".strip().rstrip(":")[:300]
            if st.state == "ready":
                st.state, st.since, st.restarts_seen = "down", time.time(), st.restarts_seen + 1
                log.warning("%s (%s) stopped answering: %s", cap, st.engine, reason)
            elif st.state == "starting" and time.time() - self.started > self.s.engine_start_timeout_s:
                st.state, st.since = "failed", time.time()
                log.error("%s (%s) did not start within %.0fs: %s", cap, st.engine,
                          self.s.engine_start_timeout_s, reason)
            st.error, st.latency_ms = reason, None
            return
        if st.state != "ready":
            log.info("%s ready: %s %s", cap, st.engine, version)
            st.state, st.since = "ready", time.time()
        st.version, st.error = version, None
        st.latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    async def check_all(self) -> None:
        await asyncio.gather(*(self.check(c) for c in self.status))

    async def _loop(self) -> None:
        while True:
            await self.check_all()
            await asyncio.sleep(10 if self.ready else 1)

    async def start(self) -> None:
        self.probes = Probes(self.s, read_secrets(self.s))
        self._task = asyncio.create_task(self._loop(), name="swsdb-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.probes:
            await self.probes.aclose()
