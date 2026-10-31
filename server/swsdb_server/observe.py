"""Metrics (Prometheus, at /metrics) and traces (OpenTelemetry, when OTEL_EXPORTER_OTLP_ENDPOINT is set)."""

from __future__ import annotations

import contextlib
import logging
import os
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = logging.getLogger("swsdb.observe")

REGISTRY = CollectorRegistry(auto_describe=True)
LATENCY = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter("swsdb_http_requests_total", "HTTP requests", ["method", "route", "status"],
                        registry=REGISTRY)
HTTP_SECONDS = Histogram("swsdb_http_request_seconds", "HTTP request time", ["method", "route"], buckets=LATENCY,
                         registry=REGISTRY)
QUERY_SECONDS = Histogram("swsdb_query_seconds", "Document query time", ["mode", "graph"], buckets=LATENCY,
                          registry=REGISTRY)
QUERY_HITS = Histogram("swsdb_query_hits", "Hits returned per query", buckets=(0, 1, 3, 5, 10, 20, 50, 100),
                       registry=REGISTRY)
INGEST_SECONDS = Histogram("swsdb_ingest_seconds", "Time to index one document version", buckets=(*LATENCY, 60, 300),
                           registry=REGISTRY)
INGEST_STAGE_SECONDS = Histogram("swsdb_ingest_stage_seconds", "Time per indexing stage (chunks, vectors, extract, "
                                 "graph, switch)", ["stage"], buckets=(*LATENCY, 60, 300), registry=REGISTRY)
INGEST_DOCS = Counter("swsdb_ingest_documents_total", "Documents indexed or failed", ["result"], registry=REGISTRY)
INGEST_CHUNKS = Counter("swsdb_ingest_chunks_total", "Chunks indexed", registry=REGISTRY)
SYNC_FAILURES = Counter("swsdb_sync_failures_total", "Outbox operations that failed (and will retry or failed the "
                        "document)", ["op", "kind"], registry=REGISTRY)
ENGINE_UP = Gauge("swsdb_engine_up", "1 when the engine answers its health probe", ["engine"], registry=REGISTRY)
ENGINE_PROBE = Gauge("swsdb_engine_probe_seconds", "Latency of the last health probe", ["engine"], registry=REGISTRY)
DOCUMENTS = Gauge("swsdb_documents", "Documents by status", ["status"], registry=REGISTRY)
OUTBOX_PENDING = Gauge("swsdb_outbox_pending", "Accepted changes not yet applied to every store", registry=REGISTRY)
OUTBOX_RETRYING = Gauge("swsdb_outbox_retrying", "Changes being retried", registry=REGISTRY)
OUTBOX_OLDEST = Gauge("swsdb_outbox_oldest_seconds", "Age of the oldest pending change", registry=REGISTRY)
RECONCILE_ISSUES = Gauge("swsdb_reconcile_issues", "Issues found by the last reconciliation", registry=REGISTRY)


class Activity:
    """The last hour of searches and indexed documents, per tenant, for the console (Prometheus keeps the totals).
    In memory and per server: it starts empty after a restart."""

    WINDOW_S = 3600

    def __init__(self):
        from collections import deque
        self.events: deque = deque(maxlen=200_000)  # (time, tenant, kind, seconds)

    def query(self, tenant: str, seconds: float) -> None:
        self.events.append((time.time(), tenant, "search", seconds))

    def ingest(self, tenant: str, ok: bool) -> None:
        self.events.append((time.time(), tenant, "indexed" if ok else "failed", 0.0))

    def summary(self, tenant: str | None, minutes: int = 60) -> dict:
        now = time.time()
        start = now - minutes * 60
        while self.events and self.events[0][0] < now - self.WINDOW_S:
            self.events.popleft()
        buckets = [{"minute": int((start // 60 + i + 1) * 60), "search": 0, "indexed": 0, "failed": 0}
                   for i in range(minutes)]
        lat: list[float] = []
        for ts, t, kind, secs in self.events:
            if ts < start or (tenant is not None and t != tenant):
                continue
            i = min(int((ts - start) // 60), minutes - 1)
            buckets[i][kind] += 1
            if kind == "search":
                lat.append(secs)
        lat.sort()

        def q(p: float) -> float | None:
            return round(lat[min(len(lat) - 1, int(p * len(lat)))] * 1000, 1) if lat else None

        return {"minutes": buckets, "searches": len(lat), "indexed": sum(b["indexed"] for b in buckets),
                "failed": sum(b["failed"] for b in buckets), "latency_ms": {"p50": q(0.5), "p95": q(0.95)}}


ACTIVITY = Activity()


class HttpMetrics(BaseHTTPMiddleware):
    """Requests by route template (not raw path, so ids don't explode the series count), and a trace span each."""

    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        status = 500
        with span(f"{request.method} {request.url.path}"):
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", None) or "unmatched"
                if route != "/metrics":
                    HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
                    HTTP_SECONDS.labels(request.method, route).observe(time.perf_counter() - t0)


async def refresh(app) -> None:
    """Gauges read on scrape: engine state, document counts, sync backlog."""
    for st in app.state.monitor.status.values():
        ENGINE_UP.labels(st.engine).set(1 if st.state == "ready" else 0)
        if st.latency_ms is not None:
            ENGINE_PROBE.labels(st.engine).set(st.latency_ms / 1000)
    docs = app.state.docs
    if docs is None:
        return
    with contextlib.suppress(Exception):  # Postgres down: keep the last values, the engine gauge says why
        rows = await docs.rows("SELECT status, count(*) AS n FROM swsdb.documents GROUP BY status")
        for status in ("queued", "processing", "ready", "failed", "deleting"):
            DOCUMENTS.labels(status).set(next((r["n"] for r in rows if r["status"] == status), 0))
        st = await docs.sync.stats()
        OUTBOX_PENDING.set(st["pending"])
        OUTBOX_RETRYING.set(st["retrying"])
        OUTBOX_OLDEST.set(st["oldest_s"] or 0)
        if st.get("last_reconcile"):
            RECONCILE_ISSUES.set(st["last_reconcile"]["issues"])


def exposition() -> bytes:
    return generate_latest(REGISTRY)


# ------------------------------------------------------------------ tracing (optional)

_tracer = None


def setup_tracing(version: str) -> None:
    """OpenTelemetry over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT (or ..._TRACES_ENDPOINT) is set."""
    global _tracer  # noqa: PLW0603 - set once at start-up
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "swsdb"),
                                "service.version": version})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("swsdb")
    log.info("tracing to %s", os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
             or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


@contextlib.contextmanager
def span(name: str, **attrs):
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(f"swsdb.{k}", v)
        yield s
