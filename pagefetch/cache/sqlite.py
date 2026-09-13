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

    async def start(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
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

    async def _migrate_if_needed(self) -> None:
        """Reconcile older ``fetch_cache`` schemas with the current layout.

        Older releases added a ``created_at REAL NOT NULL`` column that newer
        INSERTs no longer write. ``CREATE TABLE IF NOT EXISTS`` would happily
        keep the stale column, which made every cache write fail with
        ``IntegrityError: NOT NULL constraint failed: fetch_cache.created_at``.

        We use SQLite's ``PRAGMA user_version`` as a one-shot marker and
        drop the legacy column in-place (requires SQLite ≥ 3.35). No-op for
        fresh databases and databases that have already been migrated.
        """
        if self._db is None:
            raise RuntimeError("cache has not been started")
        db = self._db
        ver_cursor = await db.execute("PRAGMA user_version")
        ver_row = await ver_cursor.fetchone()
        await ver_cursor.close()
        if ver_row and ver_row[0] >= SCHEMA_VERSION:
            return  # already on the current schema

        # Detect an existing fetch_cache table that still carries the legacy
        # ``created_at`` column. ``PRAGMA table_info`` returns one row per
        # column with (cid, name, type, notnull, dflt_value, pk).
        exists_cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fetch_cache'"
        )
        table_exists = await exists_cursor.fetchone() is not None
        await exists_cursor.close()
        if not table_exists:
            # Fresh install; CREATE TABLE in start() will lay out the schema.
            return

        info_cursor = await db.execute("PRAGMA table_info(fetch_cache)")
        columns = await info_cursor.fetchall()
        await info_cursor.close()
        has_legacy = any(col[1] == "created_at" for col in columns)
        if not has_legacy:
            # Table layout already matches the current schema; nothing to do.
            return

        # SQLite ≥ 3.35 supports ALTER TABLE … DROP COLUMN. The bundled
        # sqlite3 in our minimum supported Python (3.11+) ships 3.37+, so
        # this is always available.
        await db.execute("ALTER TABLE fetch_cache DROP COLUMN created_at")

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
