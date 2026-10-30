"""Scheduled backups to S3-compatible storage (moto's S3 server stands in for AWS/MinIO): runs on a schedule and on
demand, prunes to SWSDB_BACKUP_KEEP in both places, and a remote copy restores on a fresh machine."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from swsdb import SwsDB
from test_container import Box, free_port, sh

pytestmark = pytest.mark.image

ADMIN = "backup-admin-key"
BUCKET = "swsdb-backups"


@pytest.fixture(scope="module")
def env():
    tag = uuid.uuid4().hex[:8]
    net, s3 = f"swsdb-net-{tag}", f"swsdb-s3-{tag}"
    port = free_port()
    sh("docker", "network", "create", net)
    sh("docker", "run", "-d", "--name", s3, "--network", net, "--network-alias", "s3", "-p", f"127.0.0.1:{port}:5000",
       "python:3.12-slim-trixie", "sh", "-c", "pip install -q 'moto[server]' && moto_server -H 0.0.0.0 -p 5000")
    try:
        from swsdb_server.s3 import S3
        deadline = time.time() + 300
        while True:  # moto installs itself first
            try:
                asyncio.run(_create_bucket(S3(f"http://127.0.0.1:{port}", BUCKET, "test", "test")))
                break
            except Exception:
                assert time.time() < deadline, sh("docker", "logs", s3, check=False)[-2000:]
                time.sleep(3)
        yield net, port
    finally:
        sh("docker", "rm", "-f", s3, check=False)
        sh("docker", "network", "rm", net, check=False)


async def _create_bucket(store) -> None:
    try:
        await store._req("PUT", None)
    finally:
        await store.close()


def backup_env(**extra) -> dict:
    return {"SWSDB_API_KEY": ADMIN, "SWSDB_BACKUP_S3_URL": "http://s3:5000", "SWSDB_BACKUP_S3_BUCKET": BUCKET,
            "SWSDB_BACKUP_S3_ACCESS_KEY": "test", "SWSDB_BACKUP_S3_SECRET_KEY": "test", "SWSDB_BACKUP_KEEP": "2",
            **extra}


def test_backups_upload_prune_and_restore_elsewhere(env, boxes):
    net, _ = env
    vol_a, vol_b = f"swsdb-bk-{uuid.uuid4().hex[:6]}", f"swsdb-bk-{uuid.uuid4().hex[:6]}"
    try:
        a = Box(vol_a, backup_env(SWSDB_BACKUP_S3_PREFIX="site-a"), extra=["--network", net]).start()
        boxes.append(a)
        a.wait_ready()
        db = SwsDB(f"http://127.0.0.1:{a.port}", api_key=ADMIN, timeout=300)
        kb = db.create_collection("kb", documents=True)
        kb.add_text("refunds", "Refunds take five business days.")
        kb.wait_ready("refunds")
        runs = [db.run_backup() for _ in range(3)]
        assert all(r["ok"] and r["uploaded"].startswith("site-a/") for r in runs)
        status = db.backups()
        remote = [o["key"] for o in status["remote"]]
        assert len(remote) == 2 and runs[0]["uploaded"] not in remote  # pruned to SWSDB_BACKUP_KEEP
        assert len([s for s in db.snapshots() if s["id"].endswith("-backup")]) == 2
        assert status["schedule"]["last"]["ok"] and status["schedule"]["s3"]["bucket"] == BUCKET

        b = Box(vol_b, backup_env(SWSDB_BACKUP_S3_PREFIX="site-a"), extra=["--network", net]).start()
        boxes.append(b)
        b.wait_ready()
        fresh = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN, timeout=300)
        manifest = fresh.fetch_backup(runs[-1]["uploaded"])
        r = fresh.restore_snapshot(manifest["id"], backup_first=False)
        assert r["reconcile"]["issues"] == 0
        assert fresh.collection("kb").query("how long do refunds take", k=1)[0]["document"] == "refunds"
    finally:
        for v in (vol_a, vol_b):
            sh("docker", "volume", "rm", "-f", v, check=False)


def test_the_schedule_runs_by_itself(env, volume, boxes):
    net, _ = env
    b = Box(volume, backup_env(SWSDB_BACKUP_INTERVAL_H="0.01", SWSDB_BACKUP_S3_PREFIX="scheduled"),
            extra=["--network", net]).start()  # every 36 s, after the first minute
    boxes.append(b)
    b.wait_ready()
    db = SwsDB(f"http://127.0.0.1:{b.port}", api_key=ADMIN)
    deadline = time.time() + 240
    while not db.backups()["remote"]:
        assert time.time() < deadline, db.backups()
        time.sleep(5)
    s = db.backups()["schedule"]
    assert s["interval_h"] == 0.01 and s["last"]["ok"] and s["next_at"]


def test_large_snapshots_go_up_in_parts(env, tmp_path, monkeypatch):
    _, port = env
    from swsdb_server import s3 as s3mod
    monkeypatch.setattr(s3mod, "PART", 5 * 1024 * 1024)  # S3's minimum part size
    big = tmp_path / "big.tar"
    big.write_bytes(os.urandom(12 * 1024 * 1024))

    async def go():
        store = s3mod.S3(f"http://127.0.0.1:{port}", BUCKET, "test", "test", prefix="multipart")
        try:
            key = await store.upload(big, "big.tar")
            back = tmp_path / "back.tar"
            n = await store.download(key, back)
            listed = await store.list()
            await store.delete(key)
            return n, back.read_bytes() == big.read_bytes(), listed
        finally:
            await store.close()

    n, same, listed = asyncio.run(go())
    assert n == 12 * 1024 * 1024 and same and any(o["key"] == "multipart/big.tar" for o in listed)
