"""The HTTP API on port 6464."""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import DATA_FORMAT, __version__
from .health import Monitor
from .settings import Settings

log = logging.getLogger("swsdb")


def _key_from(request: Request) -> str | None:
    if key := request.headers.get("api-key"):
        return key
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else None


def create_app(settings: Settings | None = None, monitor: Monitor | None = None) -> FastAPI:
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.monitor is None:
            app.state.monitor = Monitor(s)
            await app.state.monitor.start()
            owned = True
        else:
            owned = False
        yield
        if owned:
            await app.state.monitor.stop()

    app = FastAPI(title="swsdb", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)
    app.state.settings = s
    app.state.monitor = monitor

    def require_key(request: Request) -> str:
        """'admin' or 'read' for the caller. With no keys configured, every caller is admin (local use)."""
        admin, read = s.api_key, s.read_only_api_key
        if admin is None and read is None:
            return "admin"
        given = _key_from(request)
        if given and admin and hmac.compare_digest(given, admin.get_secret_value()):
            return "admin"
        if given and read and hmac.compare_digest(given, read.get_secret_value()):
            if request.method not in ("GET", "HEAD") and not request.url.path.endswith("/search"):
                raise HTTPException(403, "this key is read-only")
            return "read"
        raise HTTPException(401, "missing or wrong API key (send it as the `api-key` header or a Bearer token)")

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"title": "swsdb", "version": __version__, "engines": list(s.engines)}

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        """The server process is alive. Use /readyz to know whether swsdb can serve requests."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        m: Monitor = request.app.state.monitor
        body = {"status": m.overall(), "engines": {c: {"engine": st.engine, "state": st.state}
                                                   for c, st in m.status.items()}}
        return JSONResponse(body, status_code=200 if m.ready else 503)

    @app.get("/v1/status", tags=["health"])
    async def status(request: Request, role: str = Depends(require_key)) -> dict:
        m: Monitor = request.app.state.monitor
        return {
            "version": __version__,
            "data_format": DATA_FORMAT,
            "status": m.overall(),
            "uptime_s": round(time.time() - m.started, 1),
            "native_ports_exposed": s.expose_native,
            "engines": {c: st.public() for c, st in m.status.items()},
        }

    return app
