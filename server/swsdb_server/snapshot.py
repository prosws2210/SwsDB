"""One consistent snapshot of every engine, and restoring it.

A snapshot is a single tar file:

    manifest.json        what is inside and what wrote it (versions, schema, collections)
    postgres.dump        pg_dump of the swsdb database (catalog, keys, every tenant's SQL schema)
    qdrant/<name>.snapshot   one Qdrant snapshot per collection
    graph/<key>.dump     one FalkorDB DUMP per graph
    files/...            the uploaded originals (documents still waiting to be processed need them)

Consistency: Postgres is dumped from one MVCC snapshot, so it is consistent on its own. The vector and graph stores
are derived from it by the outbox workers, so they are paused (finishing what they hold) and writes that touch those
stores directly are refused for the few seconds the copy takes. Every store is then copied at the same point.
Changes still waiting in the outbox are in the dump and are applied after a restore.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pgsql

from . import DATA_FORMAT, __version__
from .stores import MIGRATIONS, NotFound, StoreError, Unavailable, pg_bin, run_pg

log = logging.getLogger("swsdb.snapshot")

FORMAT = 1
ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
ENGINE_PREFIX = "sw_"  # every engine object swsdb owns


def _version_tuple(v: str | None) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


class Snapshots:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self.dir: Path = self.s.data_dir / "snapshots"
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ files

    def path(self, sid: str) -> Path:
        if not ID.match(sid):
            raise StoreError(f"invalid snapshot id {sid!r}: lowercase letters, digits, '.', '-' and '_'")
        return self.dir / f"{sid}.tar"

    def manifest(self, sid: str) -> dict:
        p = self.path(sid)
        if not p.is_file():
            raise NotFound(f"snapshot {sid!r} not found")
        try:
            with tarfile.open(p) as tar:
                m = json.load(tar.extractfile("manifest.json"))
        except (tarfile.TarError, KeyError, ValueError, TypeError) as e:
            raise StoreError(f"{p.name} is not a swsdb snapshot: {e}") from None
        return {**m, "id": sid, "bytes": p.stat().st_size}

    def list(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.tar"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                out.append(self.manifest(p.stem))
            except StoreError as e:
                out.append({"id": p.stem, "bytes": p.stat().st_size, "error": str(e)})
        return out

    def delete(self, sid: str) -> None:
        p = self.path(sid)
        if not p.is_file():
            raise NotFound(f"snapshot {sid!r} not found")
        p.unlink()

    async def receive(self, sid: str, chunks) -> dict:
        """Store an uploaded snapshot (streamed), then check it is one."""
        p = self.path(sid)
        if p.exists():
            raise StoreError(f"snapshot {sid!r} already exists; delete it first or pick another id")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{sid}.upload"
        try:
            with tmp.open("wb") as f:
                async for chunk in chunks:
                    await asyncio.to_thread(f.write, chunk)
            tmp.replace(p)
            return self.manifest(sid)
        except BaseException:
            tmp.unlink(missing_ok=True)
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
            raise

    # ------------------------------------------------------------------ create

    async def create(self, name: str | None = None) -> dict:
        if self.lock.locked():
            raise Unavailable("a snapshot or restore is already running")
        async with self.lock:
            return await self._create(name)

    async def _create(self, name: str | None) -> dict:
        st = self.app.state
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        sid = f"swsdb-{stamp}" + (f"-{name}" if name else "")
        final = self.path(sid)
        if final.exists():
            raise StoreError(f"snapshot {sid!r} already exists")
        self.dir.mkdir(parents=True, exist_ok=True)
        work = self.dir / f".{sid}.work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        t0 = time.monotonic()
        manifest: dict[str, Any] = {
            "format": FORMAT, "name": name, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "swsdb_version": __version__, "data_format": DATA_FORMAT, "engines": {}, "migrations": None,
            "sql_tenants": [], "vector_collections": [], "graphs": [], "documents": 0,
        }
        for cap, es in st.monitor.status.items():
            manifest["engines"][cap] = {"engine": es.engine, "version": es.version}
        docs = st.docs
        st.maintenance = "snapshot"
        paused = False
        try:
            if docs is not None:
                await docs.sync.pause()
                paused = True
            if st.sql is not None:
                await st.sql.migrate()
                await run_pg(pg_bin("pg_dump"), "-h", st.sql.socket_dir, "-U", "swsdb", "-d", "swsdb", "-Fc",
                             "-f", str(work / "postgres.dump"))
                manifest["migrations"] = (await st.sql.rows("SELECT max(version) AS v FROM swsdb.migrations"))[0]["v"]
                manifest["sql_tenants"] = [r["role"] for r in await st.sql.rows(
                    "SELECT role FROM swsdb.sql_tenants ORDER BY role")]
                manifest["documents"] = (await st.sql.rows("SELECT count(*) AS n FROM swsdb.documents"))[0]["n"]
            if st.vector is not None:
                (work / "qdrant").mkdir()
                for c in await self._vector_names():
                    await self._vector_snapshot(c, work / "qdrant" / f"{c}.snapshot")
                    manifest["vector_collections"].append(c)
            if st.graph is not None:
                (work / "graph").mkdir()
                for g in await self._graph_names():
                    data = await st.graph.r.dump(g)
                    if data is not None:
                        (work / "graph" / f"{g}.dump").write_bytes(data)
                        manifest["graphs"].append(g)
            (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            await asyncio.to_thread(self._pack, work, final)
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        finally:
            if paused:
                docs.sync.resume()
            st.maintenance = None
            shutil.rmtree(work, ignore_errors=True)
        took = round(time.monotonic() - t0, 2)
        log.info("snapshot %s: %d documents, %d vector collections, %d graphs in %.1fs", sid, manifest["documents"],
                 len(manifest["vector_collections"]), len(manifest["graphs"]), took)
        return {**self.manifest(sid), "took_s": took}

    def _pack(self, work: Path, final: Path) -> None:
        tmp = final.with_name(f".{final.name}.partial")
        with tarfile.open(tmp, "w") as tar:
            tar.add(work / "manifest.json", "manifest.json")  # first, so reading it is cheap
            for name in ("postgres.dump", "qdrant", "graph"):
                if (work / name).exists():
                    tar.add(work / name, name)
            if self.s.files_dir.is_dir():
                tar.add(self.s.files_dir, "files")
        os.replace(tmp, final)

    async def _vector_names(self) -> list[str]:
        r = await self.app.state.vector._call("GET", "/collections")
        return sorted(c["name"] for c in r["result"]["collections"] if c["name"].startswith(ENGINE_PREFIX))

    async def _graph_names(self) -> list[str]:
        names = [n.decode() if isinstance(n, bytes) else n for n in await self.app.state.graph._cmd("GRAPH.LIST")]
        return sorted({n for n in names if n.startswith(ENGINE_PREFIX)})

    async def _vector_snapshot(self, name: str, out: Path) -> None:
        v = self.app.state.vector
        snap = (await v._call("POST", f"/collections/{name}/snapshots", params={"wait": "true"}))["result"]["name"]
        try:
            async with v.http.stream("GET", f"/collections/{name}/snapshots/{snap}", timeout=None) as r:
                if r.status_code != 200:
                    raise Unavailable(f"Qdrant: downloading the snapshot of {name} answered {r.status_code}")
                with out.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
        finally:
            with contextlib.suppress(Exception):
                await v._call("DELETE", f"/collections/{name}/snapshots/{snap}", params={"wait": "true"})

    # ------------------------------------------------------------------ restore

    def check(self, m: dict) -> None:
        """Refuse what this server can't open safely."""
        problems = []
        if int(m.get("format", 0)) > FORMAT:
            problems.append(f"snapshot format {m['format']} is newer than this swsdb understands ({FORMAT})")
        if int(m.get("data_format", 0)) > DATA_FORMAT:
            problems.append(f"data format {m['data_format']} is newer than this swsdb ({DATA_FORMAT})")
        if (m.get("migrations") or 0) > len(MIGRATIONS):
            problems.append(f"its database schema ({m['migrations']}) is newer than this swsdb ({len(MIGRATIONS)})")
        st = self.app.state
        here = {cap: es.version for cap, es in st.monitor.status.items()}
        if m.get("migrations") is not None and st.sql is None:
            problems.append("it holds PostgreSQL data but this swsdb runs without the sql engine")
        if m.get("vector_collections") and st.vector is None:
            problems.append("it holds vector collections but this swsdb runs without the vector engine")
        if m.get("graphs") and st.graph is None:
            problems.append("it holds graphs but this swsdb runs without the graph engine")
        for cap in ("vector", "graph"):
            theirs = (m.get("engines") or {}).get(cap, {}).get("version")
            if theirs and here.get(cap) and _version_tuple(theirs) > _version_tuple(here[cap]):
                problems.append(f"it was taken with {cap} engine {theirs}, newer than this one ({here[cap]})")
        if problems:
            raise StoreError("can't restore this snapshot: " + "; ".join(problems))

    async def restore(self, sid: str, *, backup_first: bool = True) -> dict:
        if self.lock.locked():
            raise Unavailable("a snapshot or restore is already running")
        m = self.manifest(sid)
        self.check(m)
        backup = None
        if backup_first:  # a way back if the restored data turns out to be the wrong one
            backup = (await self.create("before-restore"))["id"]
        async with self.lock:
            return {**await self._restore(sid, m), "backup": backup}

    async def _restore(self, sid: str, m: dict) -> dict:
        st = self.app.state
        t0 = time.monotonic()
        work = self.dir / f".{sid}.restore"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        st.maintenance = "restore"
        docs = st.docs
        if docs is not None:
            await docs.sync.stop()
        try:
            await asyncio.to_thread(self._unpack, self.path(sid), work)
            if st.sql is not None and (work / "postgres.dump").is_file():
                await self._restore_postgres(work / "postgres.dump", m.get("sql_tenants") or [])
            if st.vector is not None:
                await self._restore_vectors(work / "qdrant", m.get("vector_collections") or [])
            if st.graph is not None:
                await self._restore_graphs(work / "graph", m.get("graphs") or [])
            await asyncio.to_thread(self._restore_files, work / "files")
            st.keys._rows = None  # the restored database has its own keys
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if docs is not None:
                await docs.sync.start()
            st.maintenance = None
        report = None
        if docs is not None:  # the stores were copied at one point; check anyway and queue whatever differs
            with contextlib.suppress(Exception):
                r = await docs.sync.reconcile(None, dry_run=False)
                report = {"issues": r["issues"], "totals": r["totals"]}
        took = round(time.monotonic() - t0, 2)
        log.info("restored snapshot %s (%s, swsdb %s) in %.1fs", sid, m.get("created_at"), m.get("swsdb_version"),
                 took)
        return {"restored": sid, "created_at": m.get("created_at"), "swsdb_version": m.get("swsdb_version"),
                "documents": m.get("documents"), "vector_collections": len(m.get("vector_collections") or []),
                "graphs": len(m.get("graphs") or []), "reconcile": report, "took_s": took}

    @staticmethod
    def _unpack(tar_path: Path, work: Path) -> None:
        with tarfile.open(tar_path) as tar:
            tar.extractall(work, filter="data")  # no absolute paths, links out of the folder or devices

    async def _restore_postgres(self, dump: Path, tenant_roles: list[str]) -> None:
        sql = self.app.state.sql
        conninfo = psycopg.conninfo.make_conninfo(host=sql.socket_dir, dbname="postgres", user="swsdb")
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
            for role in tenant_roles:  # roles live outside the database: create those the dump's objects belong to
                if not role.startswith("swsdb_t_"):
                    continue
                exists = await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])).fetchone()
                if not exists:
                    await conn.execute(pgsql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                        "NOINHERIT CONNECTION LIMIT 20").format(pgsql.Identifier(role)))
            await conn.execute("DROP DATABASE IF EXISTS swsdb WITH (FORCE)")
            await conn.execute("CREATE DATABASE swsdb")
        await sql.drop_connections()
        sql.forget()
        await run_pg(pg_bin("pg_restore"), "-h", sql.socket_dir, "-U", "swsdb", "-d", "swsdb", "--exit-on-error",
                     str(dump))
        for role in tenant_roles:  # search_path is a role setting, not part of the dump
            if role.startswith("swsdb_t_"):
                await sql.execute(pgsql.SQL("ALTER ROLE {r} SET search_path = {r}").format(
                    r=pgsql.Identifier(role)))
        await sql.migrate()  # a snapshot from an older swsdb is brought up to this version's schema

    async def _restore_vectors(self, folder: Path, names: list[str]) -> None:
        v = self.app.state.vector
        for c in await self._vector_names():
            await v._call("DELETE", f"/collections/{c}")
        for c in names:
            path = folder / f"{c}.snapshot"
            with path.open("rb") as f:
                r = await v.http.post(f"/collections/{c}/snapshots/upload", params={"priority": "snapshot",
                                                                                     "wait": "true"},
                                      files={"snapshot": (path.name, f, "application/octet-stream")}, timeout=None)
            if r.status_code != 200:
                raise Unavailable(f"Qdrant: restoring {c} answered {r.status_code}: {r.text[:500]}")

    async def _restore_graphs(self, folder: Path, names: list[str]) -> None:
        g = self.app.state.graph
        for key in await self._graph_names():
            await g.drop(key)
        for key in names:
            await g.r.restore(key, 0, (folder / f"{key}.dump").read_bytes(), replace=True)

    def _restore_files(self, folder: Path) -> None:
        target = self.s.files_dir
        old = target.with_name(f".{target.name}.old")
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        if folder.is_dir():
            shutil.move(str(folder), str(target))
        else:
            target.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old, ignore_errors=True)
