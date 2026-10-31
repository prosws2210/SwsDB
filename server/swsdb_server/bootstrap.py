"""Prepare the container before any engine starts (runs once per container start, as root).

Lays out the data volume, refuses data it can't safely open, creates per-engine secrets, initialises Postgres on first
boot and writes each enabled engine's configuration into the run directory. Engines read only what this writes, so a
restart always starts from the same, current settings.
"""

from __future__ import annotations

import glob
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import DATA_FORMAT, __version__
from .engines import EngineSelectionError
from .settings import Settings

USER = "swsdb"
PG_MAJOR = os.environ.get("SWSDB_PG_MAJOR", "17")
FALKORDB_MODULE = Path("/opt/swsdb/engines/falkordb/falkordb.so")


class BootstrapError(RuntimeError):
    """Something the user must fix; the message says what."""


# ------------------------------------------------------------------ pure renderers (unit-tested)


def render_pg_conf(s: Settings) -> str:
    listen = "*" if s.expose_native else "127.0.0.1"
    return "\n".join([
        "# Written by swsdb on every start; change settings with ALTER SYSTEM or SWSDB_* variables.",
        f"listen_addresses = '{listen}'",
        "port = 5432",
        f"unix_socket_directories = '{s.pg_socket_dir}'",
        "max_connections = 100",
        "shared_buffers = 128MB",
        "password_encryption = 'scram-sha-256'",
        "log_destination = 'stderr'",
        "logging_collector = off",
        "log_line_prefix = '[postgres] %m [%p] '",
        "log_timezone = 'UTC'",
        "timezone = 'UTC'",
        *(["shared_preload_libraries = 'age'"] if s.in_postgres("graph") and "graph" in s.engines else []),
        "",
    ])


def render_pg_hba(s: Settings) -> str:
    lines = [
        "# Written by swsdb on every start.",
        "# Over the Unix socket the OS user swsdb may log in as swsdb (superuser) or swsdb_app (pass-through SQL).",
        "local   all   all                 peer map=swsdb",
        "host    all   all   127.0.0.1/32  scram-sha-256",
        "host    all   all   ::1/128       scram-sha-256",
    ]
    if s.expose_native:
        lines += ["host    all   all   0.0.0.0/0     scram-sha-256", "host    all   all   ::/0          scram-sha-256"]
    return "\n".join(lines) + "\n"


def render_pg_ident() -> str:
    # OS user swsdb may connect as the swsdb superuser (the server's own work), swsdb_app (the default tenant's
    # pass-through SQL) or a tenant's sandbox role swsdb_t_<tenant> (other tenants' pass-through SQL)
    return ("# Written by swsdb on every start.\nswsdb   swsdb   swsdb\nswsdb   swsdb   swsdb_app\n"
            "swsdb   swsdb   /^swsdb_t_[a-z0-9_-]+$\n")


def render_qdrant_config(s: Settings, secret: dict[str, str]) -> str:
    # JSON is valid YAML, and Qdrant reads the file by its .yaml extension
    cfg = {
        "log_level": "INFO",
        "storage": {
            "storage_path": str(s.qdrant_data / "storage"),
            "snapshots_path": str(s.qdrant_data / "snapshots"),
            "on_disk_payload": True,
        },
        "service": {
            "host": "0.0.0.0" if s.expose_native else "127.0.0.1",
            "http_port": 6333,
            "grpc_port": 6334,
            "api_key": secret["qdrant_api_key"],
            "enable_static_content": False,
        },
        "cluster": {"enabled": False},
        # A database shouldn't report usage anywhere unless the operator chooses to.
        "telemetry_disabled": True,
    }
    return json.dumps(cfg, indent=2) + "\n"


def render_redis_conf(s: Settings, secret: dict[str, str]) -> str:
    bind = "0.0.0.0 ::" if s.expose_native else "127.0.0.1 -::1"
    return "\n".join([
        "# Written by swsdb on every start.",
        f"bind {bind}",
        "port 6379",
        "protected-mode yes",
        f'requirepass "{secret["falkordb_password"]}"',
        "daemonize no",
        'logfile ""',
        f"dir {s.graph_data}",
        # durability: an append-only log fsynced every second, plus periodic snapshots
        "appendonly yes",
        "appendfsync everysec",
        "save 3600 1 300 100 60 10000",
        f"loadmodule {FALKORDB_MODULE}",
        "",
    ])


def new_secrets() -> dict[str, str]:
    return {k: secrets.token_urlsafe(32) for k in ("postgres_password", "qdrant_api_key", "falkordb_password")}


def check_layout(meta: dict | None) -> None:
    """Refuse data written by a newer swsdb: opening it could corrupt it."""
    if meta and int(meta.get("format", 0)) > DATA_FORMAT:
        raise BootstrapError(
            f"the data volume was written by swsdb {meta.get('written_by', '?')} (data format {meta['format']}), "
            f"newer than this image ({__version__}, format {DATA_FORMAT}). Run a newer image, or restore a backup "
            "taken with this version.")


def check_pg_major(pg_data: Path) -> None:
    v = pg_data / "PG_VERSION"
    if v.is_file() and v.read_text().strip() != PG_MAJOR:
        raise BootstrapError(
            f"the Postgres data in {pg_data} is from PostgreSQL {v.read_text().strip()}, but this image runs "
            f"PostgreSQL {PG_MAJOR}. Use the swsdb release that matches the data, then upgrade with "
            "`swsdb snapshot` / `swsdb restore`.")


# ------------------------------------------------------------------ side effects


def _pg_bin(name: str) -> str:
    hits = glob.glob(f"/usr/lib/postgresql/{PG_MAJOR}/bin/{name}")
    if not hits:
        raise BootstrapError(f"PostgreSQL {PG_MAJOR} binary {name} not found in the image")
    return hits[0]


def _owner(path: Path, uid: int, gid: int, mode: int | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    if (st.st_uid, st.st_gid) != (uid, gid):
        # first start on a new volume (or a volume from another uid): fix ownership once, not on every boot
        for root, dirs, files in os.walk(path):
            for n in (root, *[os.path.join(root, d) for d in dirs], *[os.path.join(root, f) for f in files]):
                os.lchown(n, uid, gid)
    if mode is not None:
        path.chmod(mode)


def _write(path: Path, text: str, uid: int, gid: int, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def _load_secrets(s: Settings, uid: int, gid: int) -> dict[str, str]:
    if s.secrets_file.is_file():
        data = json.loads(s.secrets_file.read_text(encoding="utf-8"))
        fresh = new_secrets()
        if all(k in data for k in fresh):
            return data
        data = {**fresh, **data}
    else:
        data = new_secrets()
    _write(s.secrets_file, json.dumps(data, indent=2) + "\n", uid, gid)
    return data


def _wait_for_guard(s: Settings, timeout: float = 20) -> None:
    """Nothing touches the volume until this container holds its lock (swsdb_server.guard)."""
    flag = s.run_dir / "guard.ok"
    deadline = time.monotonic() + timeout
    while not flag.is_file():
        if time.monotonic() > deadline:
            raise BootstrapError("the data-volume lock was not acquired (is another swsdb container using it?)")
        time.sleep(0.1)


def clear_stale_pid(pg_data: Path) -> bool:
    """A postmaster.pid left by a killed container blocks Postgres forever once its PID is reused in a new container.
    Safe to remove only because this container holds the volume lock and no Postgres runs in it yet."""
    pid_file = pg_data / "postmaster.pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").splitlines()[0])
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, IndexError):
        comm = ""
    if comm == "postgres":
        raise BootstrapError(f"PostgreSQL is already running on {pg_data} (PID {pid})")
    pid_file.unlink()
    print("[swsdb] removed a stale postmaster.pid (the previous container did not shut down cleanly)", flush=True)
    return True


def _init_postgres(s: Settings, secret: dict[str, str], uid: int, gid: int) -> None:
    check_pg_major(s.pg_data)
    clear_stale_pid(s.pg_data)
    if not (s.pg_data / "PG_VERSION").is_file():
        if any(s.pg_data.iterdir()):
            raise BootstrapError(f"{s.pg_data} is not empty but holds no Postgres cluster; refusing to overwrite it")
        print(f"[swsdb] first start: creating the PostgreSQL {PG_MAJOR} cluster", flush=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=s.meta_dir) as pw:
            pw.write(secret["postgres_password"])
        os.chown(pw.name, uid, gid)
        try:
            subprocess.run(
                [_pg_bin("initdb"), "-D", str(s.pg_data), "-U", USER, "--auth-local=peer",
                 "--auth-host=scram-sha-256", f"--pwfile={pw.name}", "--encoding=UTF8", "--locale=C.UTF-8",
                 "--data-checksums"],
                user=uid, group=gid, extra_groups=[], check=True, stdout=subprocess.DEVNULL,
            )
        finally:
            os.unlink(pw.name)
    conf = s.pg_data / "postgresql.conf"
    include = f"include_if_exists = '{s.run_dir / 'postgresql.swsdb.conf'}'"
    if include not in conf.read_text(encoding="utf-8"):
        with conf.open("a", encoding="utf-8") as f:
            f.write(f"\n# swsdb settings (rewritten on every start)\n{include}\n")
    _write(s.run_dir / "postgresql.swsdb.conf", render_pg_conf(s), uid, gid, 0o644)
    _write(s.pg_data / "pg_hba.conf", render_pg_hba(s), uid, gid)
    _write(s.pg_data / "pg_ident.conf", render_pg_ident(), uid, gid)
    _owner(s.pg_socket_dir, uid, gid, 0o750)


def run(s: Settings) -> None:
    import pwd
    try:
        engines = s.engines
    except EngineSelectionError as e:
        raise BootstrapError(str(e)) from None
    pw = pwd.getpwnam(USER)
    uid, gid = pw.pw_uid, pw.pw_gid

    _wait_for_guard(s)
    _owner(s.run_dir, uid, gid, 0o750)
    _owner(s.data_dir, uid, gid, 0o750)
    _owner(s.meta_dir, uid, gid, 0o700)

    layout = s.meta_dir / "layout.json"
    meta = json.loads(layout.read_text(encoding="utf-8")) if layout.is_file() else None
    check_layout(meta)
    secret = _load_secrets(s, uid, gid)

    local = s.local_engines
    if "sql" in local:
        _owner(s.pg_data, uid, gid, 0o700)
        _init_postgres(s, secret, uid, gid)
    if "vector" in local:
        _owner(s.qdrant_data, uid, gid, 0o750)
        for d in ("storage", "snapshots"):
            _owner(s.qdrant_data / d, uid, gid, 0o750)
        _write(s.run_dir / "qdrant.yaml", render_qdrant_config(s, secret), uid, gid)
    if "graph" in local:
        _owner(s.graph_data, uid, gid, 0o750)
        if not FALKORDB_MODULE.is_file():
            raise BootstrapError(f"graph engine module missing at {FALKORDB_MODULE}")
        _write(s.run_dir / "redis.conf", render_redis_conf(s, secret), uid, gid)

    _write(s.run_dir / "engines", "\n".join(local) + "\n", uid, gid, 0o644)  # the s6 services run these
    _write(layout, json.dumps({
        "format": DATA_FORMAT,
        "written_by": __version__,
        "created_by": (meta or {}).get("created_by", __version__),
        "engines": sorted(set((meta or {}).get("engines", [])) | set(engines)),
    }, indent=2) + "\n", uid, gid)

    if s.tls:
        for f in (s.tls_cert_file, s.tls_key_file):
            if not f.is_file() or not os.access(f, os.R_OK):
                raise BootstrapError(f"TLS file {f} is missing or unreadable (mount it read-only into the container)")
    exposed = " (native ports exposed)" if s.expose_native else ""
    external = [c for c in engines if s.external(c) and not s.in_postgres(c)]
    note = f"; external: {', '.join(external)}" if external else ""
    inside = [f"{c} ({s.product(c)})" for c in engines if s.in_postgres(c)]
    note += f"; in PostgreSQL: {', '.join(inside)}" if inside else ""
    print(f"[swsdb] {__version__} starting with engines: {', '.join(local) or 'none'}{note}{exposed}", flush=True)
    if not s.api_key:
        print("[swsdb] WARNING: SWSDB_API_KEY is not set, so the API on port "
              f"{s.port} accepts every request. Set it before exposing the port beyond this machine.", flush=True)


def main() -> None:
    try:
        run(Settings())
    except (BootstrapError, ValueError) as e:
        print(f"[swsdb] fatal: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    os.umask(0o027)
    main()
