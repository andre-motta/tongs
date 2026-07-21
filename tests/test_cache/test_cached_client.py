"""Tests for CachedForgeClient caching and invalidation logic."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.cache.cached_client import CachedForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRState,
    MRSummary,
    User,
)
from tongs.scanner.repo import ForgeType


def _make_mr(number: int = 1, title: str = "Fix bug") -> MRSummary:
    """Build a minimal MRSummary for testing."""
    return MRSummary(
        forge_host=ForgeHost(
            hostname="gitlab.example.com",
            forge_type=ForgeType.GITLAB,
            api_base="https://gitlab.example.com/api/v4",
        ),
        repo_path="org/repo",
        local_path="/tmp/repo",
        number=number,
        title=title,
        author=User(username="alice", display_name="Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        web_url="https://gitlab.example.com/org/repo/-/merge_requests/1",
    )


@pytest_asyncio.fixture
async def cache(tmp_path):
    store = CacheStore(db_path=tmp_path / "test.db", max_size_mb=1)
    await store.open()
    yield store
    await store.close()


@pytest.fixture
def inner():
    """AsyncMock standing in for a real ForgeClient."""
    mock = AsyncMock()
    mock.list_mrs = AsyncMock(return_value=[_make_mr()])
    mock.get_mr_diff = AsyncMock(return_value=[{"old_path": "a.py", "diff": "@@"}])
    mock.approve_mr = AsyncMock()
    mock.merge_mr = AsyncMock()
    mock.close_mr = AsyncMock()
    mock.close = AsyncMock()
    # For __getattr__ fallback: add an attribute that is not overridden
    mock.some_uncached_method = AsyncMock(return_value="delegated")
    return mock


@pytest.fixture
def client(inner, cache):
    return CachedForgeClient(
        inner=inner,
        cache=cache,
        hostname="gitlab.example.com",
        mr_list_ttl=60,
        diff_ttl=300,
    )


# ===================================================================
# list_mrs caching
# ===================================================================


@pytest.mark.asyncio
class TestListMrsCaching:
    async def test_caches_on_first_call_returns_cached_on_second(self, client, inner):
        """First call hits inner, second call returns cached data without hitting inner again."""
        result1 = await client.list_mrs("org/repo")
        result2 = await client.list_mrs("org/repo")

        assert inner.list_mrs.await_count == 1
        assert len(result1) == 1
        assert len(result2) == 1
        assert result1[0].number == result2[0].number
        assert result1[0].title == result2[0].title

    async def test_cache_miss_returns_fresh_data(self, client, inner):
        """Different repo_path is a cache miss and calls inner again."""
        await client.list_mrs("org/repo")
        inner.list_mrs.return_value = [_make_mr(number=99, title="Other")]
        result = await client.list_mrs("org/other-repo")

        assert inner.list_mrs.await_count == 2
        assert result[0].number == 99

    async def test_different_state_is_cache_miss(self, client, inner):
        """Changing state parameter creates a different cache key."""
        await client.list_mrs("org/repo", state="open")
        inner.list_mrs.return_value = [_make_mr(number=2, title="Closed MR")]
        result = await client.list_mrs("org/repo", state="closed")

        assert inner.list_mrs.await_count == 2
        assert result[0].number == 2


# ===================================================================
# get_mr_diff caching
# ===================================================================


@pytest.mark.asyncio
class TestGetMrDiffCaching:
    async def test_caches_and_returns_cached(self, client, inner):
        """First call caches the diff, second call returns from cache."""
        result1 = await client.get_mr_diff("org/repo", 1)
        result2 = await client.get_mr_diff("org/repo", 1)

        assert inner.get_mr_diff.await_count == 1
        assert result1 == result2
        assert result1 == [{"old_path": "a.py", "diff": "@@"}]


# ===================================================================
# Mutation invalidation
# ===================================================================


@pytest.mark.asyncio
class TestApproveInvalidation:
    async def test_approve_invalidates_mr_list_cache(self, client, inner):
        """approve_mr invalidates the MR list cache so the next list_mrs call re-fetches."""
        await client.list_mrs("org/repo")
        assert inner.list_mrs.await_count == 1

        await client.approve_mr("org/repo", 1)
        inner.approve_mr.assert_awaited_once_with("org/repo", 1)

        # After invalidation, list_mrs should call inner again
        await client.list_mrs("org/repo")
        assert inner.list_mrs.await_count == 2


@pytest.mark.asyncio
class TestMergeInvalidation:
    async def test_merge_invalidates_all_repo_cache(self, client, inner):
        """merge_mr invalidates all cache for the repo (both MR list and diff)."""
        await client.list_mrs("org/repo")
        await client.get_mr_diff("org/repo", 1)
        assert inner.list_mrs.await_count == 1
        assert inner.get_mr_diff.await_count == 1

        await client.merge_mr("org/repo", 1)
        inner.merge_mr.assert_awaited_once_with("org/repo", 1, False, True)

        # Both list_mrs and get_mr_diff should re-fetch after merge
        await client.list_mrs("org/repo")
        await client.get_mr_diff("org/repo", 1)
        assert inner.list_mrs.await_count == 2
        assert inner.get_mr_diff.await_count == 2


@pytest.mark.asyncio
class TestCloseInvalidation:
    async def test_close_invalidates_mr_list_cache(self, client, inner):
        """close_mr invalidates the MR list cache."""
        await client.list_mrs("org/repo")
        assert inner.list_mrs.await_count == 1

        await client.close_mr("org/repo", 1)
        inner.close_mr.assert_awaited_once_with("org/repo", 1)

        # After close, list_mrs should re-fetch
        await client.list_mrs("org/repo")
        assert inner.list_mrs.await_count == 2


# ===================================================================
# __getattr__ delegation
# ===================================================================


@pytest.mark.asyncio
class TestGetAttrDelegation:
    async def test_uncached_method_delegates_to_inner(self, client, inner):
        """Methods not overridden on CachedForgeClient delegate to the inner client."""
        result = await client.some_uncached_method()
        assert result == "delegated"
        inner.some_uncached_method.assert_awaited_once()


class TestGetAttrDelegationSync:
    def test_attribute_access_delegates_to_inner(self, client, inner):
        """Non-method attributes also delegate to inner."""
        inner.supports_thread_resolution = True
        assert client.supports_thread_resolution is True
