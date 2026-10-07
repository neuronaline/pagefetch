"""Bounded LRU pool for httpx.AsyncClient instances keyed by proxy URL."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import httpx

logger = logging.getLogger("pagefetch.proxy.pool")

_RESOURCE_CLOSE_TIMEOUT = 0.5
_MAX_PROXY_HTTP_CLIENTS = 50


class _ProxyClientPool:
    """Bounded LRU pool of ``httpx.AsyncClient`` instances keyed by proxy URL.

    Evicted clients are scheduled for asynchronous ``aclose()`` on the
    running event loop — the eviction path itself never blocks — and any
    pending eviction tasks are awaited by :meth:`aclose_all` during the
    outer :meth:`PageFetch.close` so no socket or FD leaks past teardown.
    """

    __slots__ = ("_max_size", "_clients", "_eviction_tasks")

    def __init__(self, max_size: int = _MAX_PROXY_HTTP_CLIENTS) -> None:
        self._max_size = max_size
        self._clients: OrderedDict[str, httpx.AsyncClient] = OrderedDict()
        self._eviction_tasks: set[asyncio.Task[None]] = set()

    def __contains__(self, key: str) -> bool:
        return key in self._clients

    def __len__(self) -> int:
        return len(self._clients)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return dict(self._clients) == other
        if isinstance(other, _ProxyClientPool):
            return self._clients == other._clients
        return NotImplemented

    def __iter__(self):
        return iter(self._clients)

    def get(self, key: str) -> httpx.AsyncClient | None:
        client = self._clients.get(key)
        if client is not None:
            self._clients.move_to_end(key)
        return client

    def values(self):
        return self._clients.values()

    def clear(self) -> None:
        self._clients.clear()

    def put(self, key: str, client: httpx.AsyncClient) -> None:
        """Insert *client* under *key*, evicting the oldest entry if needed.

        Eviction schedules ``aclose()`` in the background and never blocks
        the caller, so the hot path stays cheap even under heavy churn.
        """
        if key in self._clients:
            old_client = self._clients[key]
            if old_client is not client:
                self._schedule_close(key, old_client)
            self._clients[key] = client
            self._clients.move_to_end(key)
            return
        self._clients[key] = client
        if len(self._clients) > self._max_size:
            evicted_key, evicted_client = self._clients.popitem(last=False)
            self._schedule_close(evicted_key, evicted_client)

    def _schedule_close(self, key: str, client: httpx.AsyncClient) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — nothing to schedule. The evicted client
            # will be GC'd; httpx.AsyncClient does not hold native
            # resources once it has never been opened.
            return
        task = loop.create_task(self._safe_close(key, client))
        self._eviction_tasks.add(task)
        task.add_done_callback(self._eviction_tasks.discard)

    @staticmethod
    async def _safe_close(key: str, client: httpx.AsyncClient) -> Exception | None:
        try:
            await asyncio.wait_for(client.aclose(), timeout=_RESOURCE_CLOSE_TIMEOUT)
            return None
        except TimeoutError as exc:
            logger.warning(
                "proxy client %s close timed out after %.1f seconds",
                key,
                _RESOURCE_CLOSE_TIMEOUT,
            )
            return exc
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.debug(
                "evicted proxy client close failed (%s): %s",
                key,
                type(exc).__name__,
            )
            return exc

    async def aclose_all(self) -> list[BaseException | None]:
        """Await pending evictions, then close every still-pooled client.

        Returns the list of exceptions raised by individual ``aclose()`` calls
        (or ``None`` for each successful close) so callers can log per-client
        failures the same way they did for the previous gather-on-values
        teardown loop.
        """
        clients = list(self._clients.values())
        self._clients.clear()
        pending = list(self._eviction_tasks)
        self._eviction_tasks.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        results = await asyncio.gather(
            *(self._safe_close(f"pool-{i}", client) for i, client in enumerate(clients)),
            return_exceptions=True,
        )
        return [r if isinstance(r, BaseException) else None for r in results]

