"""Tests for CacheStore."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = CacheStore(db_path=tmp_path / "test.db", max_size_mb=1)
    await s.open()
    yield s
    await s.close()


class TestPutGet:
    @pytest.mark.asyncio
    async def test_roundtrip(self, store):
        """put then get returns the same bytes."""
        await store.put("key1", b"hello", ttl=60)
        result = await store.get("key1")
        assert result == b"hello"

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, store):
        """get on a nonexistent key returns None."""
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self, store):
        """Expired entries are not returned by get."""
        await store.put("expire_me", b"data", ttl=10)
        # Advance time past expiration
        with patch("tongs.cache.store.time") as mock_time:
            mock_time.time.return_value = 9999999999.0
            result = await store.get("expire_me")
        assert result is None


class TestExcludedPrefixes:
    @pytest.mark.asyncio
    async def test_put_excluded_job_log_is_noop(self, store):
        """Keys starting with job_log: are silently ignored on put."""
        await store.put("job_log:abc123", b"log data", ttl=60)
        # Bypass exclusion check by querying DB directly
        cursor = await store._db.execute(
            "SELECT value FROM cache WHERE key = ?", ("job_log:abc123",)
        )
        row = await cursor.fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_get_excluded_stream_log_is_noop(self, store):
        """Keys starting with stream_log: always return None on get."""
        # Force-insert via SQL to bypass put exclusion
        import time

        now = time.time()
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("stream_log:xyz", b"data", now + 3600, now, 4),
        )
        await store._db.commit()
        result = await store.get("stream_log:xyz")
        assert result is None


class TestJson:
    @pytest.mark.asyncio
    async def test_json_roundtrip(self, store):
        """put_json/get_json preserves dict structure."""
        payload = {"status": "ok", "count": 42, "items": [1, 2, 3]}
        await store.put_json("json_key", payload, ttl=60)
        result = await store.get_json("json_key")
        assert result == payload


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_removes_key(self, store):
        """invalidate removes a single key."""
        await store.put("remove_me", b"data", ttl=60)
        await store.invalidate("remove_me")
        assert await store.get("remove_me") is None

    @pytest.mark.asyncio
    async def test_invalidate_prefix_removes_matching_only(self, store):
        """invalidate_prefix removes keys with the prefix, leaves others."""
        await store.put("pr:1", b"a", ttl=60)
        await store.put("pr:2", b"b", ttl=60)
        await store.put("ci:1", b"c", ttl=60)
        await store.invalidate_prefix("pr:")
        assert await store.get("pr:1") is None
        assert await store.get("pr:2") is None
        assert await store.get("ci:1") == b"c"


class TestClearAndPrune:
    @pytest.mark.asyncio
    async def test_clear_removes_all(self, store):
        """clear empties the entire cache."""
        await store.put("a", b"1", ttl=60)
        await store.put("b", b"2", ttl=60)
        await store.clear()
        assert await store.get("a") is None
        assert await store.get("b") is None

    @pytest.mark.asyncio
    async def test_prune_removes_only_expired(self, store):
        """prune deletes expired entries but keeps live ones."""
        import time

        now = time.time()
        # Insert one expired and one live entry via SQL for precise control
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("expired", b"old", now - 1, now - 100, 3),
        )
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("live", b"fresh", now + 3600, now, 5),
        )
        await store._db.commit()
        await store.prune()
        assert await store.get("expired") is None
        assert await store.get("live") == b"fresh"


class TestNotOpened:
    @pytest.mark.asyncio
    async def test_operations_noop_when_not_opened(self, tmp_path):
        """All operations are silent noops when open() was never called."""
        s = CacheStore(db_path=tmp_path / "unopened.db", max_size_mb=1)
        # None of these should raise
        await s.put("k", b"v", ttl=60)
        assert await s.get("k") is None
        assert await s.get_json("k") is None
        await s.invalidate("k")
        await s.invalidate_prefix("k")
        await s.clear()
        await s.prune()
        await s.close()


class TestCloseIsTerminal:
    @pytest.mark.asyncio
    async def test_operations_noop_after_close(self, tmp_path):
        """After close(), operations silently noop."""
        s = CacheStore(db_path=tmp_path / "closed.db", max_size_mb=1)
        await s.open()
        await s.put("k", b"v", ttl=60)
        await s.close()
        # All operations should noop without raising
        assert await s.get("k") is None
        await s.put("k2", b"v2", ttl=60)
        assert await s.get("k2") is None
        await s.invalidate("k")
        await s.invalidate_prefix("k")
        await s.clear()
        await s.prune()
