"""Run the swsdb server: `python -m swsdb_server`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .settings import Settings


def main() -> None:
    try:
        s = Settings()
        _ = s.engines, s.tls  # fail fast on a bad SWSDB_ENGINES or half a TLS configuration
    except ValueError as e:
        print(f"[swsdb] fatal: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    logging.basicConfig(level=s.log_level.upper(), format="[swsdb] %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from .app import create_app
    tls = {"ssl_certfile": str(s.tls_cert_file), "ssl_keyfile": str(s.tls_key_file)} if s.tls else {}
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level=s.log_level, access_log=False,
                server_header=False, proxy_headers=False, timeout_graceful_shutdown=20, **tls)


if __name__ == "__main__":
    main()
