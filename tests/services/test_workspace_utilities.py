"""Tests for the narrow workspace utility service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tongs.config import Config
from tongs.forges.models import CIStatus, ForgeHost, MRDetail, MRState, User
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    ForgeCapabilities,
    JobRef,
    RepositoryRef,
    ReviewRef,
    ReviewSnapshot,
)
from tongs.services.workspace_utilities import (
    EditorPlanStatus,
    EditorReservation,
    WorkspaceUtilityService,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
REPOSITORY = RepositoryRef("github.com", "acme/widgets")
REVIEW = ReviewRef(REPOSITORY, 7)
JOB = JobRef(REPOSITORY, 31)


def _snapshot(url: str = "https://github.com/acme/widgets/pull/7") -> ReviewSnapshot:
    detail = MRDetail(
        forge_host=ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com"),
        repo_path="acme/widgets",
        local_path="",
        number=7,
        title="Review widgets",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=NOW,
        updated_at=NOW,
        web_url=url,
        description="Details",
        head_sha="head",
        base_sha="base",
    )
    return ReviewSnapshot(
        ref=REVIEW,
        detail=detail,
        revision=None,
        capabilities=ForgeCapabilities(False, False, False, False, False),
        revision_error=ServiceError(
            ServiceErrorCode.REVISION_UNAVAILABLE, "Revision unavailable."
        ),
    )


class Calls:
    def __init__(self, *, url: str = "https://github.com/acme/widgets/pull/7") -> None:
        self.url = url
        self.reviews: list[ReviewRef] = []
        self.jobs: list[JobRef] = []
        self.cache_clears = 0
        self.reservations: list[tuple[int, str]] = []
        self.releases: list[tuple[int, str]] = []
        self.capacity = True

    async def get_review(self, review: ReviewRef) -> ReviewSnapshot:
        self.reviews.append(review)
        return _snapshot(self.url)

    async def get_job_log(self, job: JobRef) -> str:
        self.jobs.append(job)
        return "line one\nline two\n"

    async def clear_cache(self) -> None:
        self.cache_clears += 1

    async def reserve_editor_export(
        self, job_id: int, token: str
    ) -> EditorReservation | None:
        self.reservations.append((job_id, token))
        if not self.capacity:
            return None
        return EditorReservation(1, token, f"tongs-slot-1-job-{job_id}-{token}.log")

    async def release_editor_export(self, slot: int, token: str) -> bool:
        self.releases.append((slot, token))
        return True


def _service(
    calls: Calls,
    *,
    config: Config | None = None,
    environment: dict[str, str] | None = None,
    max_bytes: int = 1024,
) -> WorkspaceUtilityService:
    return WorkspaceUtilityService(
        config=config or Config(editor_command="code --wait"),
        get_review=calls.get_review,
        get_job_log=calls.get_job_log,
        clear_cache=calls.clear_cache,
        reserve_editor_export=calls.reserve_editor_export,
        release_editor_export=calls.release_editor_export,
        environment={} if environment is None else environment,
        max_editor_log_bytes=max_bytes,
        token_factory=lambda: "1" * 32,
    )


@pytest.mark.asyncio
async def test_review_url_is_fetched_for_exact_admitted_review() -> None:
    calls = Calls()

    result = await _service(calls).review_url(REVIEW)

    assert result.review == REVIEW
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert calls.reviews == [REVIEW]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets/pull/7",
        "https://token@github.com/acme/widgets/pull/7",
        "https://example.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/\udcff",
        "https://github.com/acme/widgets/\x7f",
    ],
)
async def test_review_url_rejects_non_forge_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ServiceError) as caught:
        await _service(Calls(url=url)).review_url(REVIEW)

    assert caught.value.code is ServiceErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_clear_cache_uses_only_supplied_shared_cache_authority() -> None:
    calls = Calls()

    await _service(calls).clear_shared_cache()

    assert calls.cache_clears == 1
    assert calls.reviews == []
    assert calls.jobs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "environment", "status"),
    [
        (Config(external_editor_enabled=False), {}, EditorPlanStatus.DISABLED),
        (Config(), {}, EditorPlanStatus.MISSING),
        (Config(editor_command="'unterminated"), {}, EditorPlanStatus.MALFORMED),
        (Config(editor_command="code\udcff --wait"), {}, EditorPlanStatus.MALFORMED),
        (Config(editor_command="nvim"), {}, EditorPlanStatus.TERMINAL_UNSUPPORTED),
        (Config(), {"VISUAL": "code --wait"}, EditorPlanStatus.READY),
        (Config(), {"EDITOR": "kate --block"}, EditorPlanStatus.READY),
    ],
)
async def test_editor_configuration_outcomes_do_not_fetch_unless_ready(
    config: Config, environment: dict[str, str], status: EditorPlanStatus
) -> None:
    calls = Calls()

    result = await _service(
        calls, config=config, environment=environment
    ).prepare_editor_log(JOB)

    assert result.status is status
    assert calls.jobs == ([JOB] if status is EditorPlanStatus.READY else [])
    assert calls.reservations == (
        [(JOB.job_id, "1" * 32)] if status is EditorPlanStatus.READY else []
    )


@pytest.mark.asyncio
async def test_editor_plan_parses_arguments_without_a_shell() -> None:
    calls = Calls()
    config = Config(editor_command="code --wait --reuse-window")

    result = await _service(calls, config=config).prepare_editor_log(JOB)

    assert result.status is EditorPlanStatus.READY
    assert result.argv == ("code", "--wait", "--reuse-window")
    assert result.content == "line one\nline two\n"
    assert result.reservation == EditorReservation(
        1, "1" * 32, f"tongs-slot-1-job-{JOB.job_id}-{'1' * 32}.log"
    )
    assert calls.jobs == [JOB]


@pytest.mark.asyncio
async def test_editor_plan_rejects_oversized_log_without_truncation() -> None:
    calls = Calls()

    async def oversized(job: JobRef) -> str:
        calls.jobs.append(job)
        return "four"

    service = WorkspaceUtilityService(
        config=Config(editor_command="code --wait"),
        get_review=calls.get_review,
        get_job_log=oversized,
        clear_cache=calls.clear_cache,
        reserve_editor_export=calls.reserve_editor_export,
        release_editor_export=calls.release_editor_export,
        environment={},
        max_editor_log_bytes=3,
        token_factory=lambda: "1" * 32,
    )

    result = await service.prepare_editor_log(JOB)

    assert result.status is EditorPlanStatus.LOG_TOO_LARGE
    assert result.content is None
    assert calls.jobs == [JOB]
    assert calls.releases == [(1, "1" * 32)]


@pytest.mark.asyncio
async def test_editor_capacity_is_reserved_before_log_fetch() -> None:
    calls = Calls()
    calls.capacity = False

    result = await _service(calls).prepare_editor_log(JOB)

    assert result.status is EditorPlanStatus.CAPACITY_EXCEEDED
    assert calls.reservations == [(JOB.job_id, "1" * 32)]
    assert calls.jobs == []
