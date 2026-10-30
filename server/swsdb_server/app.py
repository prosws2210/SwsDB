"""The HTTP API on port 6464."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import socket
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import DATA_FORMAT, __version__, mcp_http, observe
from .answer import Answerer
from .api_v1 import router as v1_router
from .auth import Caller, KeyStore
from .backups import Backups
from .documents import Documents
from .embed import Embedder
from .extract import Extractor, NameExtractor
from .health import Monitor, qdrant_key, qdrant_url, read_secrets
from .names import NameError_
from .settings import Settings
from .snapshot import Snapshots
from .stores import Conflict, Forbidden, GraphStore, NotFound, SqlStore, StoreError, Unavailable, VectorStore

log = logging.getLogger("swsdb")

CONSOLE_DIR = Path(__file__).parent / "console"

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
    if "graph" in s.engines and s.entities != "off":
        if s.llm_model and s.entities in ("auto", "llm"):
            extractor = Extractor(s.llm_url or "https://api.openai.com/v1", s.llm_model,
                                  s.llm_api_key.get_secret_value() if s.llm_api_key else None)
            log.info("entity extraction with %s", s.llm_model)
        elif s.entities == "llm":
            log.warning("SWSDB_ENTITIES=llm but SWSDB_LLM_MODEL is not set: no entity extraction")
        else:
            extractor = NameExtractor()
            log.info("entity linking by names (set SWSDB_LLM_MODEL for model-based extraction)")
    docs = Documents(s, app.state.sql, app.state.vector, app.state.graph, embedder, extractor)
    delay = 1.0
    while True:  # e.g. Postgres restarting right now: keep trying, /readyz stays 503 meanwhile
        try:
            await docs.sync.start()
            break
        except Exception as e:
            app.state.docs_error = f"{type(e).__name__}: {e}"
            log.warning("document service not started yet: %s (retrying in %.0fs)", app.state.docs_error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    app.state.monitor.on_ready.append(docs.sync.engine_recovered)
    app.state.docs, app.state.docs_error = docs, None
    log.info("documents ready (embeddings %s)", embedder.name)


async def _preload_keys(app: FastAPI) -> None:
    """Load the managed keys as soon as Postgres answers, so key checks never wait on it later."""
    delay = 0.5
    while True:
        try:
            await app.state.keys.any_active()
            return
        except Exception as e:  # Postgres starting: callers with managed keys get 503 until this succeeds
            log.debug("managed keys not loaded yet: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)


async def _sync_stats(app: FastAPI) -> dict:
    try:
        return await app.state.docs.sync.stats()
    except Exception as e:  # Postgres down: say so rather than fail the status call
        return {"error": f"{type(e).__name__}: {e}"}


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
                app.state.sql = SqlStore(None if s.postgres_url else str(s.pg_socket_dir),
                                         dsn=s.postgres_url.get_secret_value() if s.postgres_url else None)
                app.state.sql.backup_dir = s.data_dir / "backups"
                await app.state.sql.open()
                made.append(app.state.sql)
            if "vector" in s.engines:
                app.state.vector = VectorStore(qdrant_url(s), qdrant_key(s, secret))
                made.append(app.state.vector)
            if "graph" in s.engines:
                app.state.graph = (GraphStore(url=s.falkordb_url.get_secret_value()) if s.falkordb_url
                                   else GraphStore(password=secret["falkordb_password"]))
                made.append(app.state.graph)
        app.state.keys = KeyStore(app.state.sql)
        if (s.answer_model or s.llm_model) and app.state.answerer is None:
            app.state.answerer = Answerer(s.llm_url or "https://api.openai.com/v1", s.answer_model or s.llm_model,
                                          s.llm_api_key.get_secret_value() if s.llm_api_key else None)
        preload = asyncio.create_task(_preload_keys(app), name="swsdb-keys-load")
        starter = None
        if app.state.sql is not None and app.state.vector is not None and app.state.docs is None:
            starter = asyncio.create_task(_start_documents(app), name="swsdb-documents-start")
        app.state.backups.start()
        async with AsyncExitStack() as stack:
            for m in app.state.mcp_servers:  # the MCP transports' session managers
                await stack.enter_async_context(m.session_manager.run())
            yield
        preload.cancel()
        await app.state.backups.stop()
        if starter:
            starter.cancel()
        if app.state.docs is not None:
            await app.state.docs.sync.stop()
            await app.state.docs.embedder.close()
            if app.state.docs.extractor:
                await app.state.docs.extractor.close()
        if app.state.answerer is not None:
            await app.state.answerer.close()
        for st in made:
            await st.close()
        if owned:
            await app.state.monitor.stop()

    app = FastAPI(title="swsdb", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)
    app.add_middleware(observe.HttpMetrics)
    observe.setup_tracing(__version__)
    app.state.settings = s
    app.state.monitor = monitor
    app.state.sql = app.state.vector = app.state.graph = app.state.docs = None
    app.state.docs_error = None
    app.state.keys = KeyStore((stores or {}).get("sql"))
    app.state.maintenance = None  # "snapshot" or "restore" while one runs
    app.state.answerer = None
    app.state.snapshots = Snapshots(app)
    app.state.backups = Backups(app)
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

    @app.exception_handler(Forbidden)
    async def _forbidden(_: Request, e: Forbidden):
        return _error(403, str(e))

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

    async def caller(request: Request) -> Caller:
        """Who is calling. Keys from the environment reach every tenant; managed keys reach one. With no keys at all
        (none in the environment and no managed ones), every caller is a global admin, for local use."""
        admin, read = s.api_key, s.read_only_api_key
        given = _key_from(request)
        if given and admin and hmac.compare_digest(given, admin.get_secret_value()):
            return Caller("admin", None)
        if given and read and hmac.compare_digest(given, read.get_secret_value()):
            return Caller("read", None)
        keys: KeyStore = request.app.state.keys
        if given and (c := await keys.resolve(given)) is not None:
            return c
        if admin is None and read is None and not await keys.any_active():
            return Caller("admin", None)
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
        if request.app.state.maintenance == "restore":
            body["status"], ready = "restoring", False
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
    async def status(request: Request, c: Caller = Depends(caller)) -> dict:
        m: Monitor = request.app.state.monitor
        return {
            "version": __version__,
            "data_format": DATA_FORMAT,
            "status": m.overall(),
            "uptime_s": round(time.time() - m.started, 1),
            "native_ports_exposed": s.expose_native,
            "role": c.role,
            "tenant": c.tenant,
            "engines": {c: st.public() for c, st in m.status.items()},
            **({"sync": await _sync_stats(request.app)} if request.app.state.docs is not None else {}),
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request, c: Caller = Depends(caller)) -> Response:
        """Prometheus metrics. Needs an API key once keys are set (they reveal document counts), from the environment:
        the numbers cover every tenant."""
        if not c.is_global:
            raise HTTPException(403, "metrics cover every tenant: use a key from the environment, not a tenant's key")
        await observe.refresh(request.app)
        return Response(observe.exposition(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/console", include_in_schema=False)
    async def console_root() -> RedirectResponse:
        return RedirectResponse("/console/")

    app.include_router(v1_router(caller))
    app.state.mcp_servers, mcp_routes = mcp_http.servers(s, caller)
    app.router.routes.extend(mcp_routes)
    app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

    local_names = {"localhost", socket.gethostname().lower(), *s.allowed_hosts_list}

    def _known_host(host: str) -> bool:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.endswith("]") or "]:" in host else host
        name = name.strip("[]").lower()
        if name in local_names or name.endswith(".localhost"):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        """Browsers: only swsdb's own pages may call it (no cross-site requests, so no CSRF). And while the API is open
        (no keys), only names that can't be a DNS-rebinding trick: localhost, IP addresses, this container's hostname
        and SWSDB_ALLOWED_HOSTS. A web page you visit can't reach a local swsdb through its own domain."""
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
        origin = request.headers.get("origin")
        if origin is not None:
            o = urlsplit(origin).netloc.lower() if origin != "null" else "null"
            if o != host and o.rsplit(":", 1)[0] not in s.allowed_hosts_list:
                return _error(403, "cross-origin requests are refused: swsdb serves its own console; call the API "
                                   "from your backend, or list the site in SWSDB_ALLOWED_HOSTS")
        if s.api_key is None and s.read_only_api_key is None and not request.app.state.keys.has_any_cached():
            if host and not _known_host(host):
                return _error(403, f"host {host!r} is refused while swsdb has no API key (a web page could otherwise "
                                   "reach it through DNS rebinding): set SWSDB_API_KEY, or add the name to "
                                   "SWSDB_ALLOWED_HOSTS")
        return await call_next(request)

    @app.middleware("http")
    async def restoring(request: Request, call_next):
        """While a snapshot is restored the data is being replaced: only status and the snapshot list answer."""
        if request.app.state.maintenance == "restore" and request.url.path.startswith("/v1/") and not (
                request.url.path == "/v1/status" or (request.method == "GET"
                                                     and request.url.path.startswith("/v1/admin/snapshots"))):
            r = _error(503, "swsdb is restoring a snapshot; try again in a moment")
            r.headers["Retry-After"] = "5"
            return r
        return await call_next(request)

    @app.middleware("http")
    async def console_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console"):
            # the console only talks to this server; nothing inline, nothing from elsewhere
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            # revalidate every time (a cheap 304 when unchanged), so an upgraded server never runs yesterday's console
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
