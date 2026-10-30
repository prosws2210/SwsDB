"""Python client for swsdb.

    from swsdb import SwsDB
    db = SwsDB("http://localhost:6464", api_key="...")
    docs = db.create_collection("docs", vector_size=384)
    docs.upsert([{"id": "a", "vector": [...], "payload": {"team": "support"}}])
    docs.search([...], limit=5, where={"team": "support"})
"""

from __future__ import annotations

from .client import Collection, SwsDB, SwsDBError

__all__ = ["Collection", "SwsDB", "SwsDBError"]
__version__ = "0.1.0"
