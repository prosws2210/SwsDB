"""Search results kept in memory (plans/performance.md, 2.2).

A result is keyed by everything that decides it, the caller's principals included, and by the collection's generation:
a counter Postgres bumps in the same transaction that makes a document version live or starts a delete. A request
reads the generation first, so a result computed while a change commits is stored under the old generation and never
served after it. The expiry is a safety net for changes that bypass the catalog (an admin's raw writes through
/qdrant); a reset (a restore, a lost event connection) clears everything.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class ResultCache:
    def __init__(self, size: int, ttl_s: float):
        self.size, self.ttl_s = size, ttl_s
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.hits = self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.size > 0

    def get(self, key: Any) -> Any | None:
        item = self._items.get(key)
        if item is None or item[0] < time.monotonic():
            if item is not None:
                del self._items[key]
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item[1]

    def put(self, key: Any, value: Any) -> None:
        if not self.enabled:
            return
        self._items[key] = (time.monotonic() + self.ttl_s, value)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
