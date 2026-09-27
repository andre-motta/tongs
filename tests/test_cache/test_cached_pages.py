"""Review list pages are cached one page at a time (#344)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRPage,
    MRState,
    MRSummary,
    ReviewDecision,
    User,
)
from tongs.scanner.repo import ForgeType

_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")


def _summary(number: int) -> MRSummary:
    return MRSummary(
        forge_host=_HOST,
        repo_path="acme/repo",
        local_path="",
        number=number,
        title=f"PR {number}",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        web_url="https://github.com/acme/repo/pull/1",
        comment_count=3,
        labels=("bug",),
        review_decision=ReviewDecision.APPROVED,
        additions=4,
        deletions=1,
    )


class _PagedClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        self.calls.append((repo_path, state, page, per_page))
        return MRPage((_summary(page * 10),), has_next=page < 3)


@pytest_asyncio.fixture
async def cached(tmp_path) -> AsyncIterator[tuple[CachedForgeClient, _PagedClient]]:
    store = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
    await store.open()
    inner = _PagedClient()
    yield CachedForgeClient(inner, store, hostname="github.com"), inner  # type: ignore[arg-type]
    await store.close()


@pytest.mark.asyncio
async def test_each_page_is_cached_under_its_own_key(cached) -> None:
    client, inner = cached
    first = await client.list_mrs_page("acme/repo", page=1, per_page=2)
    second = await client.list_mrs_page("acme/repo", page=2, per_page=2)
    again = await client.list_mrs_page("acme/repo", page=1, per_page=2)
    other_size = await client.list_mrs_page("acme/repo", page=1, per_page=5)

    assert inner.calls == [
        ("acme/repo", "open", 1, 2),
        ("acme/repo", "open", 2, 2),
        ("acme/repo", "open", 1, 5),
    ]
    assert first.items[0].number == 10
    assert second.items[0].number == 20
    assert again == first
    assert again.items[0].labels == ("bug",)
    assert again.items[0].review_decision is ReviewDecision.APPROVED
    assert other_size.has_next is True


@pytest.mark.asyncio
async def test_review_invalidation_drops_cached_pages(cached) -> None:
    client, inner = cached
    await client.list_mrs_page("acme/repo", page=1)
    assert await client.invalidate_review_reads("acme/repo", 10) is True
    await client.list_mrs_page("acme/repo", page=1)
    assert len(inner.calls) == 2
