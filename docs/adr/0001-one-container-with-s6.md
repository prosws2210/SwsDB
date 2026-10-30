# ADR 0001: One container, engines supervised by s6-overlay

Status: accepted · Date: 2026-10-30

## Context

swsdb must start the way Qdrant does: one image, one `docker run`, one volume. Inside, it needs PostgreSQL, Qdrant and
a graph engine, plus the swsdb server. Each engine is its own process with its own start-up, readiness, crash and
shutdown behaviour.

## Decision

- One image built from `python:3.12-slim-trixie`, with **s6-overlay v3** as PID 1.
- Engines come from their official images at pinned versions (`COPY --from=`), PostgreSQL from the PGDG apt
  repository. A build step fails if any engine binary has an unresolved library.
- A **oneshot bootstrap** (`swsdb_server.bootstrap`, as root) prepares the volume, generates per-engine secrets,
  initialises Postgres on first start, and rewrites every engine's configuration on every start. It refuses data written
  by a newer swsdb (`layout.json`) or by another Postgres major version.
- **Longrun services** for each engine and for the server, all dropping to the `swsdb` user (uid 10001). A disabled
  engine's service marks itself "once" and exits, so `SWSDB_ENGINES` needs no rebuild.
- The server depends on the engine services in s6, so on shutdown it stops **first** and the engines stop after it.
  Postgres stops on SIGINT (fast shutdown with a checkpoint); Redis/FalkorDB on SIGTERM (saves).
- **Readiness is owned by the server**, not s6: `/readyz` probes each enabled engine (Postgres over its Unix socket with
  peer auth, Qdrant over HTTP with its API key, FalkorDB with its password) and reports `starting` / `ready` / `down` /
  `failed` with versions and latency. The image's `HEALTHCHECK` uses `/readyz`.
- Engines bind to `127.0.0.1` only; `SWSDB_EXPOSE_NATIVE=true` opens them, with authentication always on.
- Qdrant telemetry is disabled.

## Alternatives considered

- **Docker Compose with one container per engine.** Better isolation and independent scaling, but not the "one
  `docker run`" experience the product is built around. Planned later as an optional layout (PLAN Phase 7).
- **The swsdb server as the supervisor (tini + Python child processes).** Simpler to customise, but a server crash would
  take every engine with it, and process supervision is a solved problem s6 handles better.
- **supervisord.** Needs Python at PID 1, weaker shutdown ordering and readiness semantics than s6.

## Consequences

- One image to ship and test; crash recovery and clean shutdown are covered by `tests/image`.
- An engine crash restarts that engine only (1 s backoff by s6); `/readyz` shows `down` meanwhile.
- All engines share the container's memory and CPU limits; the defaults must stay small (measured: 166 MB idle).
- Upgrading an engine means a new swsdb release, tested against data from the previous one.
