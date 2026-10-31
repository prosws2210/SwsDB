"""The Server-Timing header: each stage a request goes through, with its duration and call count."""

import asyncio

from swsdb_server import timing


async def _app(scope, receive, send):
    with timing.stage("postgres"):
        await asyncio.sleep(0.002)
    with timing.stage("postgres"):
        pass
    await timing.timed("embed")(asyncio.sleep)(0.001)
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _run(enabled: bool) -> dict:
    sent = []

    async def go():
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(m):
            sent.append(m)
        await timing.ServerTiming(_app, enabled=enabled)({"type": "http"}, receive, send)
    asyncio.run(go())
    return dict(sent[0]["headers"])


def test_every_stage_with_its_time_and_calls():
    h = _run(True)[b"server-timing"].decode()
    parts = {p.split(";")[0]: p for p in h.split(", ")}
    assert set(parts) == {"postgres", "embed", "total"}
    assert 'desc="2 calls"' in parts["postgres"] and "desc" not in parts["embed"]
    assert float(parts["postgres"].split("dur=")[1].split(";")[0]) >= 2
    assert float(parts["total"].split("dur=")[1]) >= float(parts["postgres"].split("dur=")[1].split(";")[0])


def test_off_and_outside_a_request():
    assert b"server-timing" not in _run(False)
    timing.add("postgres", 1.0)  # no request: nothing to record, nothing breaks
