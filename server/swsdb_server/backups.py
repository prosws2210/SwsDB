"""Scheduled backups: a snapshot every SWSDB_BACKUP_INTERVAL_H hours, copied to S3-compatible storage when configured,
keeping the last SWSDB_BACKUP_KEEP of them in each place.

The schedule survives restarts (the last run is recorded on the volume), so a server that was down when a backup was
due takes it shortly after starting. Restoring from a remote copy is two steps: fetch it back into the server's
snapshots, then restore it like any snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

from .s3 import S3, S3Error
from .stores import NotFound, StoreError

log = logging.getLogger("swsdb.backups")

NAME = "backup"  # scheduled snapshots are swsdb-<time>-backup


class Backups:
    def __init__(self, app):
        self.app = app
        s = app.state.settings
        self.s = s
        self.state_file: Path = s.meta_dir / "backups.json"
        self.s3: S3 | None = None
        if s.backup_s3_bucket:
            self.s3 = S3(s.backup_s3_url or "https://s3.amazonaws.com", s.backup_s3_bucket,
                         s.backup_s3_access_key.get_secret_value() if s.backup_s3_access_key else "",
                         s.backup_s3_secret_key.get_secret_value() if s.backup_s3_secret_key else "",
                         s.backup_s3_region, s.backup_s3_prefix)
        self._task: asyncio.Task | None = None
        self.running = False

    # ------------------------------------------------------------------ state

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        with contextlib.suppress(OSError):
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)

    def status(self) -> dict:
        state = self._load()
        interval = self.s.backup_interval_h
        last_ok = state.get("last_success_at")
        next_at = None
        if interval > 0:
            base = state.get("last_success_epoch") or time.time()
            next_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(base + interval * 3600, time.time())))
        return {"interval_h": interval or None, "keep": self.s.backup_keep, "running": self.running,
                "s3": {"endpoint": self.s3.endpoint, "bucket": self.s3.bucket, "prefix": self.s3.prefix}
                if self.s3 else None,
                "last": state.get("last"), "last_success_at": last_ok, "next_at": next_at}

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self.s.backup_interval_h > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="swsdb-backups")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self.s3:
            await self.s3.close()

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # let the engines and the document service settle after a start
        while True:
            state = self._load()
            due = (state.get("last_success_epoch") or 0) + self.s.backup_interval_h * 3600
            wait = due - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 3600))
                continue
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # recorded by run(); try again in an hour at the latest
                log.warning("scheduled backup failed: %s", e)
                await asyncio.sleep(min(3600, self.s.backup_interval_h * 3600))

    async def run(self) -> dict:
        """Take a snapshot now, upload it, and prune old ones."""
        if self.running:
            raise StoreError("a backup is already running")
        self.running = True
        t0 = time.monotonic()
        state = self._load()
        last: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            snap = await self.app.state.snapshots.create(NAME)
            last.update(snapshot=snap["id"], bytes=snap["bytes"])
            if self.s3:
                path = self.app.state.snapshots.path(snap["id"])
                last["uploaded"] = await self.s3.upload(path, f"{snap['id']}.tar")
            last["pruned"] = await self._prune()
            last["ok"] = True
            state.update(last_success_at=last["at"], last_success_epoch=time.time())
            log.info("backup %s done in %.0fs%s", snap["id"], time.monotonic() - t0,
                     f", uploaded to {last['uploaded']}" if self.s3 else "")
        except Exception as e:
            last.update(ok=False, error=f"{type(e).__name__}: {e}"[:500])
            raise
        finally:
            last["took_s"] = round(time.monotonic() - t0, 1)
            state["last"] = last
            self._save(state)
            self.running = False
        return last

    async def _prune(self) -> dict:
        keep = self.s.backup_keep
        snaps = self.app.state.snapshots
        local = sorted(s["id"] for s in snaps.list() if s["id"].endswith(f"-{NAME}"))
        removed_local = local[:-keep] if keep and len(local) > keep else []
        for sid in removed_local:
            with contextlib.suppress(NotFound):
                snaps.delete(sid)
        removed_remote: list[str] = []
        if self.s3:
            remote = sorted(o["key"] for o in await self.s3.list() if o["key"].endswith(f"-{NAME}.tar"))
            removed_remote = remote[:-keep] if keep and len(remote) > keep else []
            for key in removed_remote:
                await self.s3.delete(key)
        return {"local": removed_local, "remote": removed_remote}

    # ------------------------------------------------------------------ remote copies

    async def remote(self) -> list[dict]:
        if not self.s3:
            return []
        return sorted(await self.s3.list(), key=lambda o: o["key"], reverse=True)

    async def fetch(self, key: str) -> dict:
        """Download a remote copy into the server's snapshots (then restore it like any snapshot)."""
        if not self.s3:
            raise StoreError("no S3 storage is configured (SWSDB_BACKUP_S3_BUCKET)")
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".tar"):
            raise StoreError("not a snapshot file")
        snaps = self.app.state.snapshots
        sid = name.removesuffix(".tar")
        dest = snaps.path(sid)  # validates the id
        if dest.exists():
            return snaps.manifest(sid)
        snaps.dir.mkdir(parents=True, exist_ok=True)
        try:
            await self.s3.download(key, dest)
        except S3Error as e:
            raise StoreError(str(e)) from None
        return snaps.manifest(sid)
