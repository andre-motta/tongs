"""SQLite-backed cache for forge API responses."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiosqlite

from tongs.config import cache_dir


class CacheStore:
    """Async SQLite cache with TTL and LRU eviction."""

    _EXCLUDED_PREFIXES = ("job_log:", "stream_log:")

    def __init__(
        self,
        db_path: Path | None = None,
        max_size_mb: int = 100,
    ) -> None:
        self._db_path = db_path or (Path(cache_dir()) / "cache.db")
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._db_path.exists():
            fd = os.open(str(self._db_path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                size_bytes INTEGER NOT NULL
            )
            """
        )
        await self._db.commit()

    async def get(self, key: str) -> bytes | None:
        if self._db is None:
            return None
        if any(key.startswith(p) for p in self._EXCLUDED_PREFIXES):
            return None
        now = time.time()
        cursor = await self._db.execute(
            "SELECT value FROM cache WHERE key = ? AND expires_at > ?",
            (key, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await self._db.execute(
            "UPDATE cache SET created_at = ? WHERE key = ?",
            (now, key),
        )
        await self._db.commit()
        return row[0]

    async def put(self, key: str, value: bytes, ttl: int) -> None:
        if self._db is None:
            return
        if any(key.startswith(p) for p in self._EXCLUDED_PREFIXES):
            return
        now = time.time()
        expires_at = now + ttl
        size_bytes = len(value)
        await self._db.execute(
            """
            INSERT OR REPLACE INTO cache (key, value, expires_at, created_at, size_bytes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, value, expires_at, now, size_bytes),
        )
        await self._db.commit()
        await self._enforce_size_limit()

    async def invalidate(self, key: str) -> None:
        if self._db is None:
            return
        await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
        await self._db.commit()

    async def invalidate_prefix(self, prefix: str) -> None:
        if self._db is None:
            return
        await self._db.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
        await self._db.commit()

    async def clear(self) -> None:
        if self._db is None:
            return
        await self._db.execute("DELETE FROM cache")
        await self._db.commit()

    async def prune(self) -> None:
        if self._db is None:
            return
        now = time.time()
        await self._db.execute("DELETE FROM cache WHERE expires_at <= ?", (now,))
        await self._db.commit()

    async def _enforce_size_limit(self) -> None:
        if self._db is None:
            return
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM cache"
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        if total <= self._max_size_bytes:
            return
        await self._db.execute(
            """
            DELETE FROM cache WHERE key IN (
                SELECT key FROM cache ORDER BY created_at ASC
                LIMIT (SELECT COUNT(*) / 4 FROM cache)
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get_json(self, key: str) -> dict | list | None:
        data = await self.get(key)
        if data is None:
            return None
        return json.loads(data)

    async def put_json(self, key: str, value: dict | list, ttl: int) -> None:
        encoded = json.dumps(value).encode()
        await self.put(key, encoded, ttl)
