"""Bounded LRU pool for ``httpx.AsyncHTTPTransport`` instances keyed by proxy URL."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import httpx

logger = logging.getLogger("pagefetch.proxy.pool")

_RESOURCE_CLOSE_TIMEOUT = 0.5
_MAX_PROXY_TRANSPORTS = 50


class ProxyTransportPool:
    """Bounded LRU pool of ``httpx.AsyncHTTPTransport`` instances keyed by proxy URL.

    Evicted transports are scheduled for asynchronous ``aclose()`` on the
    running event loop after any in-flight operations using them finish draining.
    """

    __slots__ = ("_max_size", "_transports", "_eviction_tasks", "_in_flight", "_drain_events")

    def __init__(self, max_size: int = _MAX_PROXY_TRANSPORTS) -> None:
        self._max_size = max_size
        self._transports: OrderedDict[str, httpx.AsyncHTTPTransport] = OrderedDict()
        self._eviction_tasks: set[asyncio.Task[None]] = set()
        self._in_flight: dict[httpx.AsyncHTTPTransport, int] = {}
        self._drain_events: dict[httpx.AsyncHTTPTransport, asyncio.Event] = {}

    def retain(self, transport: httpx.AsyncHTTPTransport) -> None:
        """Mark *transport* as actively handling an in-flight request."""
        count = self._in_flight.get(transport, 0) + 1
        self._in_flight[transport] = count
        event = self._drain_events.get(transport)
        if event is not None and count > 0:
            event.clear()

    def release(self, transport: httpx.AsyncHTTPTransport) -> None:
        """Mark one in-flight request on *transport* as completed."""
        count = self._in_flight.get(transport, 0) - 1
        if count <= 0:
            self._in_flight.pop(transport, None)
            event = self._drain_events.get(transport)
            if event is not None:
                event.set()
        else:
            self._in_flight[transport] = count

    def get(self, key: str) -> httpx.AsyncHTTPTransport | None:
        transport = self._transports.get(key)
        if transport is not None:
            self._transports.move_to_end(key)
        return transport

    def clear(self) -> None:
        self._transports.clear()
        self._in_flight.clear()
        self._drain_events.clear()

    def put(self, key: str, transport: httpx.AsyncHTTPTransport) -> None:
        """Insert *transport* under *key*, scheduling non-blocking eviction if over capacity."""
        if key in self._transports:
            old_transport = self._transports[key]
            if old_transport is not transport:
                self._schedule_close(key, old_transport)
            self._transports[key] = transport
            self._transports.move_to_end(key)
            return
        self._transports[key] = transport
        if len(self._transports) > self._max_size:
            evicted_key, evicted_transport = self._transports.popitem(last=False)
            self._schedule_close(evicted_key, evicted_transport)

    def _schedule_close(self, key: str, transport: httpx.AsyncHTTPTransport) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._safe_close(key, transport))
        self._eviction_tasks.add(task)
        task.add_done_callback(self._eviction_tasks.discard)

    async def _safe_close(
        self, key: str, transport: httpx.AsyncHTTPTransport
    ) -> Exception | None:
        if self._in_flight.get(transport, 0) > 0:
            event = self._drain_events.setdefault(transport, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "proxy transport %s in-flight requests did not drain within 5s before close",
                    key,
                )
            finally:
                self._drain_events.pop(transport, None)
        self._in_flight.pop(transport, None)
        try:
            await asyncio.wait_for(transport.aclose(), timeout=_RESOURCE_CLOSE_TIMEOUT)
            return None
        except TimeoutError as exc:
            logger.warning(
                "proxy transport %s close timed out after %.1f seconds",
                key,
                _RESOURCE_CLOSE_TIMEOUT,
            )
            return exc
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.debug(
                "evicted proxy transport close failed (%s): %s",
                key,
                type(exc).__name__,
            )
            return exc

    async def aclose_all(self) -> list[BaseException | None]:
        """Await pending evictions, then close every still-pooled transport."""
        transports = list(self._transports.values())
        self._transports.clear()
        pending = list(self._eviction_tasks)
        self._eviction_tasks.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        results = await asyncio.gather(
            *(self._safe_close(f"pool-{i}", t) for i, t in enumerate(transports)),
            return_exceptions=True,
        )
        return [r if isinstance(r, BaseException) else None for r in results]
