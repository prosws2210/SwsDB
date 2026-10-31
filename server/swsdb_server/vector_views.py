"""A closer look at a vector collection, for the console (the ideas of Qdrant's own dashboard, for swsdb's collections):
its configuration and payload indexes, memory, segments, how good its approximate index is, and a 2D map of its
points. Every function takes the collection's engine name; nothing here returns another tenant's names.

Qdrant answers all of it. The lite profile (pgvector) answers the configuration, memory (table and index sizes) and the
map; segments, index quality and payload indexes are Qdrant's.
"""

from __future__ import annotations

import time
from typing import Any

from .stores import Conflict, StoreError, _qdrant_filter

INDEX_TYPES = ("keyword", "integer", "float", "bool", "geo", "datetime", "text", "uuid")


class NotForThisEngine(Conflict):
    """The engine behind this swsdb doesn't have it (HTTP 409)."""


def _qdrant(store) -> None:
    if getattr(store, "engine", None) != "qdrant":
        raise NotForThisEngine("this is Qdrant's, and this swsdb keeps vectors in Postgres (the lite profile)")


def _vector_list(params: dict) -> list[dict]:
    v = params.get("vectors") or {}
    named = {"": v} if "size" in v else v  # one unnamed vector, or several by name
    out = []
    for name, c in named.items():
        out.append({"name": name, "kind": "multivector" if c.get("multivector_config") else "dense",
                    "size": c.get("size"), "distance": c.get("distance"), "on_disk": bool(c.get("on_disk")),
                    **({"hnsw": c["hnsw_config"]} if c.get("hnsw_config") else {}),
                    **({"quantization": c["quantization_config"]} if c.get("quantization_config") else {})})
    return out


async def inspect(store, name: str) -> dict:
    """How the collection is set up: its vectors (named, sparse, multivectors), payload indexes, segments, status."""
    if getattr(store, "engine", None) != "qdrant":
        i = await store.info(name)
        return {"engine": "pgvector", "status": i.get("status"), "points": i.get("points"),
                "vectors": [{"name": "", "kind": "dense", "size": i.get("size"), "distance": i.get("distance")}],
                "sparse": [{"name": s} for s in i.get("sparse") or []], "payload_indexes": []}
    r = (await store._call("GET", f"/collections/{name}"))["result"]
    cfg = r.get("config") or {}
    params = cfg.get("params") or {}
    return {
        "engine": "qdrant", "status": r.get("status"), "optimizer_status": r.get("optimizer_status"),
        "points": r.get("points_count"), "indexed_vectors": r.get("indexed_vectors_count"),
        "segments": r.get("segments_count"), "vectors": _vector_list(params),
        "sparse": [{"name": k, **({"modifier": v["modifier"]} if (v or {}).get("modifier") else {})}
                   for k, v in (params.get("sparse_vectors") or {}).items()],
        "payload_indexes": sorted(({"field": f, "type": s.get("data_type"), "points": s.get("points")}
                                   for f, s in (r.get("payload_schema") or {}).items()), key=lambda x: x["field"]),
        "hnsw": cfg.get("hnsw_config"), "optimizer": cfg.get("optimizer_config"),
        "quantization": cfg.get("quantization_config"), "shards": params.get("shard_number"),
        "replication": params.get("replication_factor"), "on_disk_payload": params.get("on_disk_payload"),
    }


async def memory(store, name: str) -> dict:
    """Where the collection's bytes are: per vector, sparse vector, payload and payload index (RAM, disk, cache)."""
    if getattr(store, "engine", None) == "qdrant":
        return {"engine": "qdrant", **(await store._call("GET", f"/collections/{name}/memory"))["result"]}
    coll = await store._coll(name)
    table = f"swsdb_lite.{coll['tbl']}"
    rows = await store._rows(
        "SELECT pg_table_size(%s::regclass) AS t, pg_indexes_size(%s::regclass) AS i, "
        "coalesce(pg_total_relation_size(to_regclass(%s)), 0) AS kw", [table, table, f"{table}_t"])
    r = rows[0]
    usage = lambda n: {"disk_bytes": int(n), "ram_bytes": 0, "cached_bytes": 0, "expected_cache_bytes": 0}  # noqa: E731
    return {"engine": "pgvector", "total": usage(r["t"] + r["i"] + r["kw"]),
            "vectors": [{"name": "", "storage": usage(r["t"]), "index": usage(r["i"])}],
            "sparse_vectors": [{"name": "keywords", "storage": usage(r["kw"]), "index": usage(0)}] if r["kw"] else [],
            "payload": usage(0), "payload_index": []}


async def segments(store, name: str) -> dict:
    """The collection's segments and the optimizations running or waiting on them."""
    _qdrant(store)
    r = (await store._call("GET", f"/collections/{name}/optimizations",
                           params={"with": "queued,completed,idle_segments"}))["result"]
    return {"summary": r.get("summary") or {}, "running": r.get("running") or [], "queued": r.get("queued") or [],
            "completed": (r.get("completed") or [])[:20], "idle_segments": r.get("idle_segments") or []}


def _one_vector(v: Any, using: str) -> Any:
    if isinstance(v, dict) and "indices" not in v:  # named vectors
        if using not in v:
            raise StoreError(f"no vector named {using!r}; this collection has {', '.join(sorted(v)) or 'none'}")
        return v[using]
    return v


async def recall(store, name: str, samples: int = 50, k: int = 10, using: str = "") -> dict:
    """How often the approximate (HNSW) search finds the true nearest neighbours: for `samples` random points, the
    top `k` by the index and by an exact scan, compared (recall@k). 1.0 means the index finds exactly what a full scan
    would."""
    _qdrant(store)
    info = await inspect(store, name)
    dense = {v["name"]: v for v in info["vectors"]}
    if using not in dense:
        raise StoreError(f"no dense vector named {using!r}; this collection has "
                         f"{', '.join(repr(n) for n in dense) or 'none'}")
    picked = (await store._call("POST", f"/collections/{name}/points/query", json={
        "query": {"sample": "random"}, "limit": samples, "with_payload": False,
        "with_vector": [using] if using else True}))["result"]["points"]
    if not picked:
        return {"k": k, "samples": 0, "recall": None, "using": using, "note": "the collection has no points"}
    base: dict[str, Any] = {"limit": k, "with_payload": False, **({"using": using} if using else {})}
    queries = [{**base, "query": _one_vector(p["vector"], using)} for p in picked]

    async def batch(exact: bool) -> tuple[list[list], float]:
        t0 = time.perf_counter()
        r = (await store._call("POST", f"/collections/{name}/points/query/batch", json={
            "searches": [{**q, "params": {"exact": exact}} for q in queries]}))["result"]
        return [[h["id"] for h in x["points"]] for x in r], (time.perf_counter() - t0) * 1000

    approx, approx_ms = await batch(False)
    exact, exact_ms = await batch(True)
    per = [len(set(a) & set(e)) / len(e) if e else 1.0 for a, e in zip(approx, exact, strict=True)]
    worst = sorted(zip(per, picked, strict=True), key=lambda x: x[0])[:5]
    note = None
    if not info.get("indexed_vectors"):
        note = ("no HNSW index is built yet (Qdrant indexes a segment once it outgrows its full-scan threshold), "
                "so every search is already exact")
    return {"k": k, "samples": len(per), "using": using, "recall": sum(per) / len(per), "min": min(per),
            "approx_ms": round(approx_ms / len(per), 2), "exact_ms": round(exact_ms / len(per), 2),
            "worst": [{"id": p["id"], "recall": r} for r, p in worst if r < 1], "note": note}


async def project(store, name: str, limit: int = 1000, using: str = "", color_by: str | None = None,
                  where: dict | None = None) -> dict:
    """A 2D map of up to `limit` points: their vectors projected onto the two directions they vary most along (PCA),
    each with the value of a payload field to colour it by. Close points have similar vectors, roughly: PCA keeps the
    broad shape and loses detail (it is a picture, not a search)."""
    import numpy as np

    points, offset = [], None
    while len(points) < limit:
        page = await store.browse(name, min(500, limit - len(points)), offset, where, with_vector=True)
        points += page["points"]
        offset = page["next"]
        if offset is None:
            break
    rows, kept = [], []
    for p in points:
        v = p.get("vector")
        if v is None:
            continue
        v = _one_vector(v, using)
        if isinstance(v, dict):
            raise StoreError("sparse vectors can't be drawn on a map; pick a dense vector")
        if v and isinstance(v[0], list):  # a multivector: its mean stands for it
            v = np.mean(np.asarray(v, dtype=float), axis=0)
        rows.append(np.asarray(v, dtype=float))
        kept.append(p)
    if len(rows) < 3:
        return {"points": [], "explained": [], "using": using, "note": "a map needs at least 3 points"}
    x = np.vstack(rows)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    x = x / np.where(norms == 0, 1, norms)  # by direction, as cosine sees them
    x = x - x.mean(axis=0)
    _, s, vt = np.linalg.svd(x, full_matrices=False)
    xy = x @ vt[:2].T
    span = np.abs(xy).max(axis=0)
    xy = xy / np.where(span == 0, 1, span)
    var = (s ** 2) / max(float((s ** 2).sum()), 1e-12)

    def label(p: dict) -> Any:
        if not color_by:
            return None
        v: Any = p.get("payload") or {}
        for part in color_by.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)

    return {"using": using, "explained": [round(float(var[0]), 4), round(float(var[1]), 4)],
            "points": [{"id": p["id"], "x": round(float(a), 4), "y": round(float(b), 4), "label": label(p),
                        "payload": p.get("payload")} for p, (a, b) in zip(kept, xy, strict=True)]}


async def create_index(store, name: str, field: str, kind: str) -> None:
    _qdrant(store)
    if kind not in INDEX_TYPES:
        raise StoreError(f"index type is one of {', '.join(INDEX_TYPES)}")
    await store._call("PUT", f"/collections/{name}/index", params={"wait": "true"},
                      json={"field_name": field, "field_schema": kind})


async def drop_index(store, name: str, field: str) -> None:
    _qdrant(store)
    await store._call("DELETE", f"/collections/{name}/index/{field}", params={"wait": "true"})


async def similar(store, name: str, *, like: str | int | None, vector: list | None, using: str, limit: int,
                  where: dict | None) -> list[dict]:
    """Nearest neighbours of a point (by its id, for any kind of vector: the point itself is left out) or of a given
    vector, on a named vector."""
    if getattr(store, "engine", None) != "qdrant":
        if like is None:
            return await store.search(name, vector, limit, where)
        got = await store.get(name, [like])
        if not got:
            raise StoreError(f"no point {like!r}")
        hits = await store.search(name, got[0]["vector"], limit + 1, where)
        return [x for x in hits if str(x["id"]) != str(like)][:limit]
    from .names import point_id

    body: dict[str, Any] = {"query": point_id(like) if like is not None else vector, "limit": limit,
                            "with_payload": True, **({"using": using} if using else {})}
    if (f := _qdrant_filter(where)) is not None:
        body["filter"] = f
    out = []
    for h in (await store._call("POST", f"/collections/{name}/points/query", json=body))["result"]["points"]:
        payload = dict(h.get("payload") or {})
        out.append({"id": payload.pop("_id", h["id"]), "score": h["score"], "payload": payload})
    return out
