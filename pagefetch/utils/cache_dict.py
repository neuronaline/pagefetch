"""Generic thread-safe bounded LRU + monotonic TTL cache utility."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BoundedTTLCache(Generic[K, V]):
    """Thread-safe ordered map with O(1) LRU eviction and optional monotonic TTL."""

    __slots__ = ("_max_size", "_default_ttl", "_lock", "_data")

    def __init__(self, max_size: int, *, ttl: float | None = None) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._default_ttl = float(ttl) if ttl is not None else None
        self._lock = threading.Lock()
        self._data: OrderedDict[K, tuple[float | None, V]] = OrderedDict()

    def _deadline(self, ttl: float | None, now: float) -> float | None:
        effective = self._default_ttl if ttl is None else ttl
        return None if effective is None else now + effective

    def _sweep_and_evict(self, now: float) -> None:
        if len(self._data) < self._max_size:
            return
        expired = [k for k, (exp, _) in self._data.items() if exp is not None and now >= exp]
        for k in expired:
            del self._data[k]
        while len(self._data) >= self._max_size:
            self._data.popitem(last=False)

    def get(self, key: K, default: V | None = None) -> V | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            exp, value = entry
            if exp is not None and now >= exp:
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def set(self, key: K, value: V, *, ttl: float | None = None) -> None:
        now = time.monotonic()
        deadline = self._deadline(ttl, now)
        with self._lock:
            if key not in self._data:
                self._sweep_and_evict(now)
            self._data[key] = (deadline, value)
            self._data.move_to_end(key)

    def get_or_create(self, key: K, factory: Callable[[], V], *, ttl: float | None = None) -> V:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                exp, value = entry
                if exp is None or now < exp:
                    self._data.move_to_end(key)
                    return value
                del self._data[key]
            self._sweep_and_evict(now)
            value = factory()
            self._data[key] = (self._deadline(ttl, now), value)
            self._data.move_to_end(key)
            return value

    def increment(self, key: K, delta: int = 1, *, ttl: float | None = None) -> int:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            current = 0
            if entry is not None:
                exp, val = entry
                if exp is None or now < exp:
                    current = int(val)  # type: ignore[arg-type]
                else:
                    del self._data[key]
            else:
                self._sweep_and_evict(now)
            updated = current + delta
            self._data[key] = (self._deadline(ttl, now), updated)  # type: ignore[assignment]
            self._data.move_to_end(key)
            return updated

    def append_timestamp(self, key: K, *, window: float, now: float | None = None) -> int:
        """Record a monotonic timestamp under *key*, prune outside *window*, return count."""
        ts = time.monotonic() if now is None else now
        with self._lock:
            entry = self._data.get(key)
            history: list[float] = []
            if entry is not None:
                _, prev = entry
                history = [t for t in prev if ts - t <= window]  # type: ignore[attr-defined]
            else:
                self._sweep_and_evict(ts)
            history.append(ts)
            self._data[key] = (ts + window, history)  # type: ignore[assignment]
            self._data.move_to_end(key)
            return len(history)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
