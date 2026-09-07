"""Tests for the shared application session and read boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from tongs.cache.cached_client import CachedForgeClient
from tongs.config import Config
from tongs.errors import AuthError
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    User,
)
from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services import (
    ApplicationSession,
    JobRef,
    PipelineRef,
    RepositoryRef,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
    ServiceError,
    ServiceErrorCode,
    ServiceEventKind,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")


def make_summary(
    host: ForgeHost = GITHUB_HOST,
    project: str = "acme/widgets",
    number: int = 7,
    *,
    updated_at: datetime = NOW,
    local_path: str = "",
) -> MRSummary:
    return MRSummary(
        forge_host=host,
        repo_path=project,
        local_path=local_path,
        number=number,
        title="Review widgets",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=NOW,
        updated_at=updated_at,
        web_url=f"https://{host.hostname}/{project}/pull/{number}",
    )


def make_detail(
    host: ForgeHost = GITHUB_HOST,
    project: str = "acme/widgets",
    number: int = 7,
    *,
    head: str = "head-1",
    base: str = "base-1",
    start: str | None = None,
    local_path: str = "",
) -> MRDetail:
    summary = make_summary(host, project, number, local_path=local_path)
    return MRDetail(
        **summary.__dict__,
        description="Details",
        head_sha=head,
        base_sha=base,
        start_sha=start,
    )


class FakeCache:
    def __init__(self, *, open_error: Exception | None = None) -> None:
        self.open_error = open_error
        self.open_calls = 0
        self.close_calls = 0

    async def open(self) -> None:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error

    async def close(self) -> None:
        self.close_calls += 1


class BlockingCache(FakeCache):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def open(self) -> None:
        self.open_calls += 1
        self.started.set()
        await self.release.wait()


class BlockingCloseCache(FakeCache):
    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.Event().wait()


class FakeClient:
    supports_batched_review = True
    supports_thread_resolution = True
    supports_draft_notes = False
    supports_unapprove = False
    supports_job_cancel = True

    def __init__(self, detail: MRDetail | None = None) -> None:
        self.detail_results = [detail or make_detail()]
        self.diff = [{"filename": "src/widget.py", "patch": "@@ -1 +1 @@"}]
        self.my_reviews = [make_summary()]
        self.my_mrs = [make_summary(number=8)]
        self.repository_reviews = [make_summary()]
        self.get_mr_fresh_calls = 0
        self.get_mr_diff_fresh_calls = 0
        self.list_mrs_calls = 0
        self.cancel_get_mr = False

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        self.get_mr_fresh_calls += 1
        if self.cancel_get_mr:
            await asyncio.Event().wait()
        if len(self.detail_results) > 1:
            return self.detail_results.pop(0)
        return self.detail_results[0]

    async def get_mr_diff_fresh(self, repo_path: str, number: int) -> list[dict]:
        self.get_mr_diff_fresh_calls += 1
        return self.diff

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        self.list_mrs_calls += 1
        return self.repository_reviews

    async def list_my_reviews(self) -> list[MRSummary]:
        return self.my_reviews

    async def list_my_mrs(self) -> list[MRSummary]:
        return self.my_mrs

    async def get_mr_discussions(self, repo_path: str, number: int) -> list[Discussion]:
        comment = InlineComment("1", User("alice"), "note", NOW, "src/widget.py")
        return [Discussion("thread-1", True, comment)]

    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]:
        return [Commit("abc", "abc", "Title", "Message", User("alice"))]

    async def list_pipelines(
        self, repo_path: str, per_page: int = 20
    ) -> list[Pipeline]:
        return [Pipeline(11, CIStatus.SUCCESS, "main", "abc", "https://ci")]

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        return [Pipeline(12, CIStatus.RUNNING, "feature", "def", "https://ci")]

    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]:
        return [PipelineJob(21, "test", "verify", CIStatus.SUCCESS)]

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        return "safe log"


class FakeRegistry:
    def __init__(
        self,
        clients: dict[str, object] | None = None,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.clients = clients or {"github.com": FakeClient()}
        self.close_error = close_error
        self.get_client_calls: list[str] = []
        self.close_calls = 0
        self.hosts = {
            "github.com": GITHUB_HOST,
            "gitlab.com": GITLAB_HOST,
        }

    def active_hostnames(self) -> list[str]:
        return list(self.clients)

    def get_host(self, hostname: str) -> ForgeHost | None:
        return self.hosts.get(hostname)

    async def get_client(self, hostname: str) -> ForgeClient:
        self.get_client_calls.append(hostname)
        result = self.clients[hostname]
        if isinstance(result, Exception):
            raise result
        return cast(ForgeClient, result)

    async def close_all(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def make_repo(
    root: Path,
    *,
    hostname: str = "github.com",
    project: str = "acme/widgets",
    forge_type: ForgeType = ForgeType.GITHUB,
) -> Repo:
    remote = Remote(
        "origin", f"https://{hostname}/{project}.git", hostname, project, forge_type
    )
    return Repo(root / project.split("/")[-1], (remote,), remote)


async def start_session(
    registry: FakeRegistry,
    *,
    cache: FakeCache | None = None,
    discoverer=None,
    event_queue_size: int = 4,
) -> ApplicationSession:
    kwargs = {}
    if discoverer is not None:
        kwargs["discoverer"] = discoverer
    session = ApplicationSession(
        config=Config(max_parallel=2),
        cache=cache or FakeCache(),
        forge_registry=registry,
        event_queue_size=event_queue_size,
        **kwargs,
    )
    return await session.start()


class TestReferences:
    @pytest.mark.parametrize(
        ("hostname", "project"),
        [
            ("HTTPS://github.com", "acme/widgets"),
            ("github.com/path", "acme/widgets"),
            ("github.com", "widgets"),
            ("github.com", "../widgets"),
            ("github.com", "acme\\widgets"),
        ],
    )
    def test_rejects_noncanonical_repository_refs(
        self, hostname: str, project: str
    ) -> None:
        with pytest.raises(ValueError):
            RepositoryRef(hostname, project)

    def test_review_number_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ReviewRef(RepositoryRef("github.com", "acme/widgets"), 0)


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_context_manager_closes_owned_resources_once(self) -> None:
        cache = FakeCache()
        registry = FakeRegistry()
        async with ApplicationSession(
            config=Config(), cache=cache, forge_registry=registry
        ) as session:
            assert session.config.scan_depth == 5
        await session.close()
        assert cache.open_calls == 1
        assert cache.close_calls == 1
        assert registry.close_calls == 1

    @pytest.mark.asyncio
    async def test_start_failure_closes_partially_opened_cache(self) -> None:
        cache = FakeCache(open_error=RuntimeError("ghp_secret"))
        session = ApplicationSession(config=Config(), cache=cache)
        with pytest.raises(ServiceError) as caught:
            await session.start()
        assert caught.value.code == ServiceErrorCode.INTERNAL
        assert "ghp_secret" not in str(caught.value)
        assert cache.open_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_cancelled_start_still_closes_partial_cache(self) -> None:
        cache = BlockingCache()
        session = ApplicationSession(config=Config(), cache=cache)
        task = asyncio.create_task(session.start())
        await cache.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_close_waits_for_start_then_closes_created_resources(self) -> None:
        cache = BlockingCache()
        registry = FakeRegistry()
        session = ApplicationSession(
            config=Config(), cache=cache, forge_registry=registry
        )
        start_task = asyncio.create_task(session.start())
        await cache.started.wait()

        close_task = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        cache.release.set()
        assert await start_task is session
        await close_task
        assert cache.close_calls == 1
        assert registry.close_calls == 1
        with pytest.raises(ServiceError) as caught:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert caught.value.code == ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_cancelled_registry_cleanup_still_closes_cache(self) -> None:
        cache = FakeCache()
        registry = FakeRegistry(close_error=asyncio.CancelledError())
        session = await start_session(registry, cache=cache)

        with pytest.raises(ServiceError) as caught:
            await session.close()
        assert caught.value.code == ServiceErrorCode.SHUTDOWN_FAILED
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_shutdown_timeout_closes_session_with_safe_error(self) -> None:
        cache = BlockingCloseCache()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            forge_registry=FakeRegistry(),
            shutdown_timeout=0.01,
        ).start()

        with pytest.raises(ServiceError) as caught:
            await session.close()
        assert caught.value.code == ServiceErrorCode.SHUTDOWN_FAILED
        assert cache.close_calls == 1
        with pytest.raises(ServiceError) as closed:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert closed.value.code == ServiceErrorCode.CLOSED


class TestResourceIssuance:
    @pytest.mark.asyncio
    async def test_well_formed_unissued_same_host_fails_before_forge_call(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry)
        ref = ReviewRef(RepositoryRef("github.com", "other/repository"), 1)
        with pytest.raises(ServiceError) as caught:
            await session.get_review(ref)
        assert caught.value.code == ServiceErrorCode.RESOURCE_NOT_ISSUED
        assert registry.get_client_calls == []
        await session.close()

    @pytest.mark.asyncio
    async def test_discovery_issues_reference_without_exposing_local_path(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)

        def discoverer(*args, **kwargs):
            return [repo]

        registry = FakeRegistry()
        session = await start_session(registry, discoverer=discoverer)
        snapshots = await session.discover_repositories()
        assert snapshots[0].ref == RepositoryRef("github.com", "acme/widgets")
        assert not hasattr(snapshots[0], "local_path")
        assert snapshots[0].ref in session.issued_repositories
        await session.close()

    @pytest.mark.asyncio
    async def test_discovery_collapses_duplicate_repository_identity(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)

        def discoverer(*args, **kwargs):
            return [repo, repo]

        session = await start_session(FakeRegistry(), discoverer=discoverer)
        snapshots = await session.discover_repositories()
        assert len(snapshots) == 1
        assert len(session.issued_repositories) == 1
        await session.close()

    @pytest.mark.asyncio
    async def test_explicit_open_validates_then_issues_repository(self) -> None:
        registry = FakeRegistry()
        client = cast(FakeClient, registry.clients["github.com"])
        session = await start_session(registry)
        snapshot = await session.open_repository("github.com", "acme/widgets")
        assert snapshot.ref in session.issued_repositories
        assert client.list_mrs_calls == 1
        await session.close()


class TestReviewReads:
    @pytest.mark.asyncio
    async def test_inbox_preserves_success_when_other_host_fails(self) -> None:
        github = FakeClient()
        github.my_reviews = [make_summary(updated_at=NOW + timedelta(minutes=1))]
        registry = FakeRegistry(
            {
                "github.com": github,
                "gitlab.com": AuthError("github_pat_secret should not escape"),
            }
        )
        session = await start_session(registry)
        page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        assert [item.ref.number for item in page.items] == [7]
        assert page.items[0].ref.repository in session.issued_repositories
        assert len(page.failures) == 1
        assert page.failures[0].code == ServiceErrorCode.AUTHENTICATION_FAILED
        assert "secret" not in page.failures[0].message
        await session.close()

    @pytest.mark.asyncio
    async def test_review_models_hide_process_local_routing_data(self) -> None:
        host = ForgeHost(
            "github.com", ForgeType.GITHUB, "https://private-api.example.test"
        )
        detail = make_detail(host, local_path="/home/user/private/widgets")
        client = FakeClient(detail)
        client.my_reviews = [
            make_summary(host, local_path="/home/user/private/widgets")
        ]
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)

        page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        assert page.items[0].summary.forge_host.api_base == ""
        assert page.items[0].summary.local_path == ""
        snapshot = await session.get_review(page.items[0].ref)
        assert snapshot.detail.forge_host.api_base == ""
        assert snapshot.detail.local_path == ""
        await session.close()

    @pytest.mark.asyncio
    async def test_missing_revision_keeps_review_detail_available(self) -> None:
        client = FakeClient(make_detail(head="", base=""))
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        snapshot = await session.get_review(ReviewRef(repo.ref, 7))
        assert snapshot.detail.description == "Details"
        assert snapshot.revision is None
        assert snapshot.revision_error is not None
        assert snapshot.revision_error.code == ServiceErrorCode.REVISION_UNAVAILABLE
        await session.close()

    @pytest.mark.asyncio
    async def test_discussion_commit_and_ci_reads_use_issued_repository(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repo.ref, 7)
        assert (await session.get_discussions(review))[0].id == "thread-1"
        assert (await session.get_commits(review))[0].sha == "abc"
        assert (await session.list_pipelines(repo.ref))[0].id == 11
        assert (await session.list_review_pipelines(review))[0].id == 12
        assert (await session.get_pipeline_jobs(PipelineRef(repo.ref, 11)))[0].id == 21
        assert await session.get_job_log(JobRef(repo.ref, 21)) == "safe log"
        await session.close()

    @pytest.mark.asyncio
    async def test_cancelled_forge_read_propagates_cancellation(self) -> None:
        client = FakeClient()
        client.cancel_get_mr = True
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        task = asyncio.create_task(session.get_review(ReviewRef(repo.ref, 7)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.close()


class TestRevisionBoundDiff:
    @pytest.mark.asyncio
    async def test_stable_complete_revision_labels_fresh_raw_diff(self) -> None:
        client = FakeClient(make_detail(start="start-1"))
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        result = await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert result.revision.head_sha == "head-1"
        assert result.revision.base_sha == "base-1"
        assert result.revision.start_sha == "start-1"
        assert result.changes[0]["filename"] == "src/widget.py"
        with pytest.raises(TypeError):
            result.changes[0]["filename"] = "changed.py"  # type: ignore[index]
        assert client.get_mr_fresh_calls == 2
        assert client.get_mr_diff_fresh_calls == 1
        await session.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "second",
        [
            make_detail(head="head-2", base="base-1", start="start-1"),
            make_detail(head="head-1", base="base-2", start="start-1"),
            make_detail(head="head-1", base="base-1", start="start-2"),
        ],
    )
    async def test_any_revision_change_rejects_diff(self, second: MRDetail) -> None:
        client = FakeClient()
        client.detail_results = [make_detail(start="start-1"), second]
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        with pytest.raises(ServiceError) as caught:
            await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert caught.value.code == ServiceErrorCode.REVISION_CHANGED
        await session.close()

    @pytest.mark.asyncio
    async def test_cached_client_fresh_methods_bypass_stale_diff_cache(self) -> None:
        inner = FakeClient()
        cache = AsyncMock()
        cache.get_json.side_effect = lambda key: (
            [{"filename": "stale.py"}] if key.endswith(":diff") else None
        )
        client = CachedForgeClient(
            cast(ForgeClient, inner), cache, hostname="github.com"
        )
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        cache.get_json.reset_mock()
        result = await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert result.changes[0]["filename"] == "src/widget.py"
        cache.get_json.assert_not_awaited()
        assert inner.get_mr_fresh_calls == 2
        assert inner.get_mr_diff_fresh_calls == 1
        await session.close()


class TestEvents:
    @pytest.mark.asyncio
    async def test_events_are_ordered_and_overflow_requests_resync(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry, event_queue_size=1)
        stream = session.events()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        first = await pending
        assert first.sequence == 1

        session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        session.emit_change(ServiceEventKind.PIPELINE_CHANGED)
        overflow = await anext(stream)
        assert overflow.sequence == 3
        assert overflow.kind == ServiceEventKind.RESYNC_REQUIRED
        await stream.aclose()
        await session.close()

    @pytest.mark.asyncio
    async def test_close_finishes_pending_event_stream(self) -> None:
        session = await start_session(FakeRegistry())
        stream = session.events()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await session.close()
        with pytest.raises(StopAsyncIteration):
            await pending
