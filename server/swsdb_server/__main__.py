"""Run the swsdb server: `python -m swsdb_server`."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .settings import Settings


def _logging(s: Settings) -> None:
    logging.basicConfig(level=s.log_level.upper(), format="[swsdb] %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def worker_app():
    """One of several worker processes (SWSDB_WORKERS > 1): a server of the cluster under its own name, so the
    others hear its events (a server ignores its own) and it takes its share of the outbox and the leader's jobs."""
    from .app import create_app
    from .cluster import server_id
    s = Settings()
    _logging(s)
    return create_app(s.model_copy(update={"server_id": f"{server_id(s.server_id)[:90]}/{os.getpid()}"}))


def main() -> None:
    try:
        s = Settings()
        _ = s.engines, s.tls  # fail fast on a bad SWSDB_ENGINES or half a TLS configuration
    except ValueError as e:
        print(f"[swsdb] fatal: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    _logging(s)
    tls = {"ssl_certfile": str(s.tls_cert_file), "ssl_keyfile": str(s.tls_key_file)} if s.tls else {}
    common = dict(host=s.host, port=s.port, log_level=s.log_level, access_log=False, server_header=False,
                  proxy_headers=False, timeout_graceful_shutdown=20, **tls)
    if s.workers > 1:
        uvicorn.run("swsdb_server.__main__:worker_app", factory=True, workers=s.workers, **common)
    else:
        from .app import create_app
        uvicorn.run(create_app(s), **common)


if __name__ == "__main__":
    main()
