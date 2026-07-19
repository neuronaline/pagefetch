"""Async SQLite cache implementation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from ..models import FetchResult


class SQLiteCache:
    """Persistent cache for successful fetch results."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    async def get(self, key: str) -> FetchResult | None:
        """Return the cached result for *key*, or ``None`` when the key is missing
        or the cached entry has expired.

        Expired entries are automatically deleted from the database before
        returning ``None``.
        """
        if self._db is None:
            raise RuntimeError("cache has not been started")
        cursor = await self._db.execute(
            "SELECT payload, expires_at FROM fetch_cache WHERE cache_key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        if row[1] <= time.time():
            await self._db.execute("DELETE FROM fetch_cache WHERE cache_key = ?", (key,))
            await self._db.commit()
            return None
        try:
            result = FetchResult.from_dict(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            await self._db.execute("DELETE FROM fetch_cache WHERE cache_key = ?", (key,))
            await self._db.commit()
            return None
        result.from_cache = True
        result.fetch_method = "cache"
        return result

    async def set(self, key: str, result: FetchResult, ttl: int) -> None:
        """Persist *result* in the cache with the given *ttl* (in seconds).

        Only successful results are cached.  If the result cannot be serialized
        a :exc:`RuntimeError` is raised with details about the failure.
        """
        if self._db is None:
            raise RuntimeError("cache has not been started")
        if not result.success:
            return
        now = time.time()
        try:
            payload = result.json(include_html=True)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Failed to serialize fetch result for cache key {key!r}: {exc}"
            ) from exc
        await self._db.execute(
            """
            INSERT INTO fetch_cache(cache_key, payload, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload=excluded.payload,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (key, payload, now, now + ttl),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

