"""Where a request's time goes: each stage (auth, catalog, embed, qdrant, postgres, graph...) adds its duration to the
request's record, and the response carries them in a `Server-Timing` header (a web standard: browsers' developer tools
show it), e.g. `Server-Timing: postgres;dur=0.84;desc="2 calls", embed;dur=14.2, total;dur=16.9`.

It costs a few microseconds a request. `SWSDB_TIMING=false` turns the header off.
"""

from __future__ import annotations

import functools
import time
from contextlib import contextmanager
from contextvars import ContextVar

# the current request's stages: name -> [seconds, calls]. A mutable dict, so tasks the request starts (they copy the
# context) add to the same record
_record: ContextVar[dict[str, list] | None] = ContextVar("swsdb_timing", default=None)


def add(stage: str, seconds: float) -> None:
    rec = _record.get()
    if rec is not None:
        s = rec.get(stage)
        if s is None:
            rec[stage] = [seconds, 1]
        else:
            s[0] += seconds
            s[1] += 1


@contextmanager
def stage(name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        add(name, time.perf_counter() - t0)


def header(rec: dict[str, list], total: float) -> bytes:
    parts = [f'{k};dur={v[0] * 1000:.2f}' + (f';desc="{v[1]} calls"' if v[1] > 1 else "") for k, v in rec.items()]
    parts.append(f"total;dur={total * 1000:.2f}")
    return ", ".join(parts).encode()


class ServerTiming:
    """Pure ASGI middleware (no extra task per request): starts the record, adds the header to the response."""

    def __init__(self, app, enabled: bool = True):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        rec: dict[str, list] = {}
        token = _record.set(rec)
        t0 = time.perf_counter()

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"server-timing", header(rec, time.perf_counter() - t0))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _record.reset(token)


def timed(name: str):
    """A decorator: the async function's duration counts toward stage `name`."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                add(name, time.perf_counter() - t0)
        return wrapper
    return deco
