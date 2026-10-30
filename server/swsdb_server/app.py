"""The HTTP API on port 6464."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import DATA_FORMAT, __version__
from .api_v1 import router as v1_router
from .documents import Documents
from .embed import Embedder
from .extract import Extractor
from .health import Monitor, read_secrets
from .names import NameError_
from .settings import Settings
from .stores import Conflict, GraphStore, NotFound, SqlStore, StoreError, Unavailable, VectorStore

log = logging.getLogger("swsdb")

ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
               422: "invalid", 503: "unavailable"}


def _key_from(request: Request) -> str | None:
    if key := request.headers.get("api-key"):
        return key
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": ERROR_CODES.get(status, "error"), "message": message}, status_code=status)


async def _start_documents(app: FastAPI) -> None:
    """Documents need Postgres and Qdrant: wait until both answer, then resume unfinished ingestion."""
    s: Settings = app.state.settings
    while not all(app.state.monitor.status[c].state == "ready" for c in ("sql", "vector")):  # noqa: ASYNC110 - polls the monitor
        await asyncio.sleep(0.5)
    embedder = Embedder(s.embeddings, s.embeddings_model, s.embeddings_url,
                        s.embeddings_api_key.get_secret_value() if s.embeddings_api_key else None,
                        cache_dir=str(s.models_dir))
    extractor = None
    if s.llm_model and "graph" in s.engines:
        extractor = Extractor(s.llm_url or "https://api.openai.com/v1", s.llm_model,
                              s.llm_api_key.get_secret_value() if s.llm_api_key else None)
        log.info("entity extraction with %s", s.llm_model)
    docs = Documents(s, app.state.sql, app.state.vector, app.state.graph, embedder, extractor)
    delay = 1.0
    while True:  # e.g. Postgres restarting right now: keep trying, /readyz stays 503 meanwhile
        try:
            await docs.start()
            break
        except Exception as e:
            app.state.docs_error = f"{type(e).__name__}: {e}"
            log.warning("document service not started yet: %s (retrying in %.0fs)", app.state.docs_error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    app.state.docs, app.state.docs_error = docs, None
    log.info("documents ready (embeddings %s)", embedder.name)


def documents_expected(app: FastAPI) -> bool:
    return app.state.sql is not None and app.state.vector is not None


def create_app(settings: Settings | None = None, monitor: Monitor | None = None, stores: dict | None = None) -> FastAPI:
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = app.state.monitor is None
        if owned:
            app.state.monitor = Monitor(s)
            await app.state.monitor.start()
        made: list = []
        if stores is None:
            secret = read_secrets(s)
            if "sql" in s.engines:
                app.state.sql = SqlStore(str(s.pg_socket_dir))
                await app.state.sql.open()
                made.append(app.state.sql)
            if "vector" in s.engines:
                app.state.vector = VectorStore("http://127.0.0.1:6333", secret["qdrant_api_key"])
                made.append(app.state.vector)
            if "graph" in s.engines:
                app.state.graph = GraphStore("127.0.0.1", 6379, secret["falkordb_password"])
                made.append(app.state.graph)
        starter = None
        if app.state.sql is not None and app.state.vector is not None and app.state.docs is None:
            starter = asyncio.create_task(_start_documents(app), name="swsdb-documents-start")
        yield
        if starter:
            starter.cancel()
        if app.state.docs is not None:
            await app.state.docs.stop()
            await app.state.docs.embedder.close()
            if app.state.docs.extractor:
                await app.state.docs.extractor.close()
        for st in made:
            await st.close()
        if owned:
            await app.state.monitor.stop()

    app = FastAPI(title="swsdb", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)
    app.state.settings = s
    app.state.monitor = monitor
    app.state.sql = app.state.vector = app.state.graph = app.state.docs = None
    app.state.docs_error = None
    for k, v in (stores or {}).items():
        setattr(app.state, k, v)

    # one error shape everywhere: {"error": code, "message": text}
    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return _error(e.status_code, str(e.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in err['loc'][1:]) or 'body'}: {err['msg']}" for err in e.errors()]
        return _error(422, "; ".join(msgs))

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return _error(404, str(e))

    @app.exception_handler(Conflict)
    async def _cf(_: Request, e: Conflict):
        return _error(409, str(e))

    @app.exception_handler(StoreError)
    async def _bad(_: Request, e: StoreError):
        return _error(400, str(e))

    @app.exception_handler(NameError_)
    async def _name(_: Request, e: NameError_):
        return _error(422, str(e))

    @app.exception_handler(ValueError)
    async def _value(_: Request, e: ValueError):
        return _error(400, str(e))

    @app.exception_handler(Unavailable)
    async def _down(_: Request, e: Unavailable):
        return _error(503, str(e))

    def require_key(request: Request) -> str:
        """'admin' or 'read' for the caller. With no keys configured, every caller is admin (local use)."""
        admin, read = s.api_key, s.read_only_api_key
        if admin is None and read is None:
            return "admin"
        given = _key_from(request)
        if given and admin and hmac.compare_digest(given, admin.get_secret_value()):
            return "admin"
        if given and read and hmac.compare_digest(given, read.get_secret_value()):
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
        body: dict = {"status": m.overall(), "engines": {c: {"engine": st.engine, "state": st.state}
                                                         for c, st in m.status.items()}}
        ready = m.ready
        if documents_expected(request.app):  # ready means documents can be accepted, not just engines answering
            started = request.app.state.docs is not None
            body["documents"] = {"state": "ready" if started else "starting",
                                 **({"error": request.app.state.docs_error} if request.app.state.docs_error else {})}
            if not started:
                ready = False
                if body["status"] == "ready":
                    body["status"] = "starting"
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/v1/status", tags=["health"])
    async def status(request: Request, role: str = Depends(require_key)) -> dict:
        m: Monitor = request.app.state.monitor
        return {
            "version": __version__,
            "data_format": DATA_FORMAT,
            "status": m.overall(),
            "uptime_s": round(time.time() - m.started, 1),
            "native_ports_exposed": s.expose_native,
            "role": role,
            "engines": {c: st.public() for c, st in m.status.items()},
        }

    app.include_router(v1_router(require_key))
    return app
