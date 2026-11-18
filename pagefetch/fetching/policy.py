"""Per-host request pacing."""

from __future__ import annotations

import asyncio
import random
import time
from urllib.parse import urlsplit

from ..utils.cache_dict import BoundedTTLCache
from ..utils.urls import registrable_host


class AsyncPacer:
    """Async token-bucket pacer for the ``request_pacing`` bot-evasion signal."""

    __slots__ = ("_max_delay", "_lock", "_next_release")

    def __init__(self, max_delay: float) -> None:
        if max_delay < 0:
            raise ValueError("max_delay must be non-negative")
        self._max_delay = float(max_delay)
        self._lock = asyncio.Lock()
        self._next_release = time.monotonic()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            base = max(now, self._next_release)
            target = base + random.uniform(0, self._max_delay)
            self._next_release = target
        delay = base - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class HostPolicy:
    """Coordinates per-host request pacing."""

    __slots__ = ("_request_pacing", "_host_pacers")

    def __init__(self, *, request_pacing: float = 0.0) -> None:
        self._request_pacing = float(request_pacing)
        self._host_pacers: BoundedTTLCache[str, AsyncPacer] = BoundedTTLCache(max_size=256)

    async def pace(self, url: str) -> None:
        """Apply per-host request pacing so concurrent single fetches do not burst."""
        if self._request_pacing <= 0:
            return
        host = registrable_host(url) or (urlsplit(url).hostname or "").lower()
        if not host:
            return
        pacer = self._host_pacers.get_or_create(host, lambda: AsyncPacer(self._request_pacing))
        await pacer.acquire()
