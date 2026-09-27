"""Paged repository review reads in the application session (#344)."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest

from tests.services.test_session import (
    NOW,
    FakeClient,
    FakeRegistry,
    make_summary,
    start_session,
)
from tongs.forges.models import MRPage, MRState
from tongs.services import (
    RepositoryRef,
    ReviewQuery,
    ReviewScope,
    ServiceError,
    ServiceErrorCode,
)

_REPOSITORY = RepositoryRef("github.com", "acme/widgets")


class _PagedClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.repository_reviews = [
            make_summary(number=n, updated_at=NOW - timedelta(minutes=n))
            for n in range(1, 6)
        ]
        self.page_calls: list[tuple[str, str, int, int]] = []

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        self.page_calls.append((repo_path, state, page, per_page))
        start = (page - 1) * per_page
        items = tuple(self.repository_reviews[start : start + per_page])
        return MRPage(items, has_next=start + per_page < len(self.repository_reviews))


async def _session_with(client: FakeClient):
    session = await start_session(FakeRegistry({"github.com": client}))
    await session.open_repository("github.com", "acme/widgets")
    return session


@pytest.mark.asyncio
async def test_paged_repository_read_returns_next_cursor_until_last_page() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    client.page_calls.clear()

    numbers: list[int] = []
    cursors: list[str | None] = []
    cursor: str | None = None
    while True:
        page = await session.list_reviews(
            ReviewQuery(
                ReviewScope.ALL_OPEN,
                repository=_REPOSITORY,
                per_page=2,
                paged=True,
                cursor=cursor,
            )
        )
        numbers.extend(item.summary.number for item in page.items)
        cursors.append(page.next_cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert numbers == [1, 2, 3, 4, 5]
    assert cursors == ["2", "3", None]
    assert client.page_calls == [
        ("acme/widgets", "open", 1, 2),
        ("acme/widgets", "open", 2, 2),
        ("acme/widgets", "open", 3, 2),
    ]
    assert client.list_mrs_calls == 0
    await session.close()


@pytest.mark.asyncio
async def test_unpaged_query_keeps_walking_every_review() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    client.page_calls.clear()

    page = await session.list_reviews(
        ReviewQuery(ReviewScope.ALL_OPEN, repository=_REPOSITORY, per_page=2)
    )

    assert [item.summary.number for item in page.items] == [1, 2, 3, 4, 5]
    assert page.next_cursor is None
    assert client.page_calls == []
    assert client.list_mrs_calls == 1
    await session.close()


@pytest.mark.asyncio
async def test_open_repository_reads_one_single_item_page() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    assert client.page_calls == [("acme/widgets", "open", 1, 1)]
    assert client.list_mrs_calls == 0
    await session.close()


@pytest.mark.asyncio
async def test_invalid_page_from_forge_is_an_invalid_response() -> None:
    class _BrokenClient(_PagedClient):
        async def list_mrs_page(self, *_args: object, **_kwargs: object) -> MRPage:
            return cast(MRPage, [make_summary()])

    client = _BrokenClient()
    session = await start_session(FakeRegistry({"github.com": client}))
    with pytest.raises(ServiceError) as caught:
        await session.open_repository("github.com", "acme/widgets")
    assert caught.value.code is ServiceErrorCode.INVALID_RESPONSE
    await session.close()


@pytest.mark.parametrize(
    "cursor", ["1", "0", "02", "abc", "", "100000", " 2", "٢", "2.5"]
)
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValueError):
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True, cursor=cursor
        )


@pytest.mark.parametrize(
    "query",
    [
        {"scope": ReviewScope.ALL_OPEN, "repository": _REPOSITORY},
        {"scope": ReviewScope.ALL_OPEN, "paged": True},
        {"scope": ReviewScope.MY_REVIEWS, "repository": _REPOSITORY, "paged": True},
    ],
)
def test_cursor_needs_a_paged_all_open_repository_read(query: dict) -> None:
    with pytest.raises(ValueError):
        ReviewQuery(**query, cursor="2")


def test_cursor_query_accepts_a_later_page() -> None:
    query = ReviewQuery(
        ReviewScope.ALL_OPEN,
        repository=_REPOSITORY,
        state=MRState.CLOSED,
        paged=True,
        cursor="35",
    )
    assert query.cursor == "35"


class _FlakyPagedClient(_PagedClient):
    def __init__(self) -> None:
        super().__init__()
        self.failing_pages: set[int] = set()

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        if page in self.failing_pages:
            self.page_calls.append((repo_path, state, page, per_page))
            raise TimeoutError("controlled page timeout")
        return await super().list_mrs_page(repo_path, state, page, per_page)


@pytest.mark.asyncio
async def test_failed_later_page_keeps_its_cursor_for_a_retry() -> None:
    client = _FlakyPagedClient()
    session = await _session_with(client)
    client.failing_pages.add(2)
    query = {
        "scope": ReviewScope.ALL_OPEN,
        "repository": _REPOSITORY,
        "per_page": 2,
        "paged": True,
    }

    failed = await session.list_reviews(ReviewQuery(**query, cursor="2"))

    assert failed.items == ()
    assert len(failed.failures) == 1
    assert failed.failures[0].repository == _REPOSITORY
    assert failed.next_cursor == "2"

    client.failing_pages.clear()
    retried = await session.list_reviews(
        ReviewQuery(**query, cursor=failed.next_cursor)
    )
    assert [item.summary.number for item in retried.items] == [3, 4]
    assert retried.failures == ()
    assert retried.next_cursor == "3"
    await session.close()


@pytest.mark.asyncio
async def test_failed_first_page_has_no_cursor() -> None:
    client = _FlakyPagedClient()
    session = await _session_with(client)
    client.failing_pages.add(1)

    page = await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, per_page=2, paged=True
        )
    )

    assert page.items == ()
    assert len(page.failures) == 1
    assert page.next_cursor is None
    await session.close()
