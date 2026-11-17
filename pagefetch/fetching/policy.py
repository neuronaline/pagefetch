"""Per-host pacing and adaptive browser-escalation memory."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import urlsplit

from ..utils.cache_dict import BoundedTTLCache
from ..utils.urls import registrable_host

logger = logging.getLogger("pagefetch")

# Process-wide adaptive host memory so short-lived PageFetch instances
# remember hosts that required browser escalation within the last 10 minutes.
_HOST_ESCALATIONS: BoundedTTLCache[str, list[float]] = BoundedTTLCache(max_size=1000, ttl=300.0)
_HOST_MEMORY: BoundedTTLCache[str, bool] = BoundedTTLCache(max_size=1000, ttl=600.0)


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
    """Coordinates per-host request pacing and cross-instance escalation memory."""

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

    def record_escalation(self, url: str) -> None:
        """Record an escalation to browser mode for adaptive host memory."""
        host = (urlsplit(url).hostname or url).lower()
        if _HOST_ESCALATIONS.append_timestamp(host, window=300.0) >= 2:
            _HOST_MEMORY.set(host, True)
            logger.info("Host %s remembered for browser mode (10m TTL)", host)

    def is_remembered(self, url: str) -> bool:
        """Return True if host is cached as requiring browser mode."""
        host = (urlsplit(url).hostname or url).lower()
        return bool(_HOST_MEMORY.get(host, False))
