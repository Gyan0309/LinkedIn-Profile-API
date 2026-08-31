"""In-process TTL cache, so repeat calls do not become repeat LinkedIn requests.

In-process rather than Redis: losing it on restart costs one refetch, which is
cheaper than requiring another service to boot.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

MAX_ENTRIES = 512


@dataclass
class _Entry[T]:
    value: T
    expires_at: float


class TTLCache[T]:
    def __init__(self, ttl_seconds: int, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, _Entry[T]] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= time.time():
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    async def set(self, key: str, value: T) -> None:
        async with self._lock:
            if len(self._entries) >= self._max_entries:
                self._evict_locked()
            self._entries[key] = _Entry(value=value, expires_at=time.time() + self._ttl)

    def _evict_locked(self) -> None:
        """Drop expired entries first, then the oldest, to stay under the cap."""
        now = time.time()
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for key in expired:
            del self._entries[key]
        while len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._entries), "hits": self.hits, "misses": self.misses}
