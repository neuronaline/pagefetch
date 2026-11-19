"""Async SQLite cache implementation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from ..models import FetchResult

# Bump whenever the ``fetch_cache`` table layout changes. ``start()`` uses
# SQLite's ``PRAGMA user_version`` so existing on-disk databases are migrated
# to the latest layout exactly once instead of silently inheriting a stale
# schema via ``CREATE TABLE IF NOT EXISTS``.
SCHEMA_VERSION = 1


class SQLiteCache:
    """Persistent cache for successful fetch results."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._cleanup_counter: int = 0

    @property
    def is_open(self) -> bool:
        """Return ``True`` when the underlying SQLite connection is open."""
        return self._db is not None

    async def start(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        try:
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._migrate_if_needed()
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fetch_cache_expires_at ON fetch_cache(expires_at)"
            )
            await self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self._db.commit()
        except Exception:
            await self.close()
            raise

    async def _migrate_if_needed(self) -> None:
        """Reset transient cache table when on-disk schema predates SCHEMA_VERSION."""
        if self._db is None:
            raise RuntimeError("cache has not been started")
        async with self._db.execute("PRAGMA user_version") as cursor:
            ver_row = await cursor.fetchone()
        if not ver_row or ver_row[0] < SCHEMA_VERSION:
            await self._db.execute("DROP TABLE IF EXISTS fetch_cache")

    async def get(self, key: str, *, requested_screenshot: bool = False) -> FetchResult | None:
        """Return the cached result for *key*, or ``None`` when the key is missing
        or the cached entry has expired.

        Expired entries are automatically deleted from the database before
        returning ``None``.

        When ``requested_screenshot=True`` and the cached payload had no
        screenshot (screenshots are never persisted; see :meth:`set`), a
        warning is appended to the returned result so callers can re-fetch
        with ``use_cache=False`` if they need one.
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
        if not result.fetch_method:
            result.fetch_method = "cache"
        if requested_screenshot and result.screenshot is None:
            result.warnings.append(
                "Screenshot is not cached; pass use_cache=False to re-fetch."
            )
        return result

    async def set(self, key: str, result: FetchResult, ttl: int) -> None:
        """Persist *result* in the cache with the given *ttl* (in seconds).

        Only successful results are cached. Screenshots are intentionally
        dropped from the persisted payload (see § 6 of the extract plan) to
        avoid bloating the SQLite store with large, perishable blobs; HTML
        and structure round-trip intact.
        """
        if self._db is None:
            raise RuntimeError("cache has not been started")
        if not result.success:
            return
        now = time.time()
        try:
            payload = result.json(
                include_html=True,
                include_structure=True,
                include_screenshot=False,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Failed to serialize fetch result for cache key {key!r}: {exc}"
            ) from exc
        await self._db.execute(
            """
            INSERT INTO fetch_cache(cache_key, payload, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload=excluded.payload,
                expires_at=excluded.expires_at
            """,
            (key, payload, now + ttl),
        )
        # Probabilistic opportunistic cleanup: avoid running the DELETE on
        # every write (the per-row INSERT is on the hot path). The cleanup
        # itself is bounded to a small batch so it cannot stall callers.
        if not self._cleanup_counter & 0x1F:
            await self._db.execute(
                "DELETE FROM fetch_cache WHERE cache_key IN ("
                "SELECT cache_key FROM fetch_cache WHERE expires_at <= ? LIMIT 100)",
                (now,),
            )
        self._cleanup_counter += 1
        await self._db.commit()

    async def clear(self, *, expired_only: bool = False) -> int:
        """Delete cached entries and return the number of rows removed."""
        if self._db is None:
            raise RuntimeError("cache has not been started")
        if expired_only:
            async with self._db.execute(
                "DELETE FROM fetch_cache WHERE expires_at <= ?",
                (time.time(),),
            ) as cursor:
                removed = max(cursor.rowcount or 0, 0)
        else:
            async with self._db.execute("DELETE FROM fetch_cache") as cursor:
                removed = max(cursor.rowcount or 0, 0)
        await self._db.commit()
        return removed

    async def close(self) -> None:
        if self._db is not None:
            # Run a passive WAL checkpoint before closing so short-lived CLI
            # invocations (separate processes) see the writes immediately
            # instead of waiting for the next auto-checkpoint or connection
            # close to flush the WAL back to the main DB. Without this the
            # first read in a fresh process after a write can transiently
            # miss the just-written row.
            try:
                await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._db.commit()
            except Exception:
                # Never let a checkpoint failure mask a normal close.
                pass
            await self._db.close()
            self._db = None
