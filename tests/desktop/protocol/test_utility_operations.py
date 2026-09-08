"""Protocol adapter tests for desktop workspace utilities."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tongs.config import Config
from tongs.desktop.protocol.messages import ProtocolError, ProtocolErrorCode
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.desktop.protocol.utility_operations import UtilityOperations
from tongs.forges.models import CIStatus, ForgeHost, MRDetail, MRState, User
from tongs.scanner.repo import ForgeType
from tongs.services import (
    EditorReservation,
    JobRef,
    RepositoryRef,
    ReviewRef,
    ServiceEventKind,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import ForgeCapabilities, ReviewSnapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)
REPOSITORY = RepositoryRef("github.com", "acme/widgets")
REVIEW = ReviewRef(REPOSITORY, 7)
JOB = JobRef(REPOSITORY, 31)


class Session:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config(editor_command="code --wait")
        self.cache_clears = 0
        self.review_reads: list[ReviewRef] = []
        self.job_reads: list[JobRef] = []
        self.events: list[ServiceEventKind] = []

    async def get_review(self, review: ReviewRef) -> ReviewSnapshot:
        self.review_reads.append(review)
        detail = MRDetail(
            forge_host=ForgeHost(
                "github.com", ForgeType.GITHUB, "https://api.github.com"
            ),
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
            web_url="https://github.com/acme/widgets/pull/7",
            description="Details",
            head_sha="head",
            base_sha="base",
        )
        return ReviewSnapshot(
            ref=review,
            detail=detail,
            revision=None,
            capabilities=ForgeCapabilities(False, False, False, False, False),
            revision_error=ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE, "Revision unavailable."
            ),
        )

    async def get_job_log(self, job: JobRef) -> str:
        self.job_reads.append(job)
        return "safe log\n"

    async def clear_cache(self) -> None:
        self.cache_clears += 1

    def emit_change(
        self, kind: ServiceEventKind, resource: object | None = None
    ) -> None:
        assert resource is None
        self.events.append(kind)


class Reservations:
    def __init__(self) -> None:
        self.reserved: list[tuple[int, str]] = []
        self.released: list[tuple[int, str]] = []
        self.available = True

    async def reserve(self, job_id: int, token: str) -> EditorReservation | None:
        self.reserved.append((job_id, token))
        if not self.available:
            return None
        return EditorReservation(2, token, f"tongs-slot-2-job-{job_id}-{token}.log")

    async def release(self, slot: int, token: str) -> bool:
        self.released.append((slot, token))
        return True


def _operations(
    session: Session | None = None,
) -> tuple[UtilityOperations, Session, Reservations, str, str]:
    actual = session or Session()
    reservations = Reservations()
    handles = HandleRegistry()
    review_handle = handles.issue(HandleKind.REVIEW, REVIEW)
    job_handle = handles.issue(HandleKind.JOB, JOB)
    return (
        UtilityOperations(
            session=actual,
            handles=handles,
            reservations=reservations,
            environment={},
        ),
        actual,
        reservations,
        review_handle,
        job_handle,
    )


@pytest.mark.asyncio
async def test_review_url_resolves_only_an_issued_review_handle() -> None:
    operations, session, _reservations, review_handle, _job_handle = _operations()

    result = await operations.review_url({"review": review_handle}, object())

    assert result == {
        "review": review_handle,
        "url": "https://github.com/acme/widgets/pull/7",
    }
    assert session.review_reads == [REVIEW]
    with pytest.raises(ProtocolError) as caught:
        await operations.review_url({"review": "not-issued"}, object())
    assert caught.value.code is ProtocolErrorCode.INVALID_HANDLE


@pytest.mark.asyncio
async def test_cache_clear_uses_shared_cache_and_emits_resync() -> None:
    operations, session, _reservations, _review_handle, _job_handle = _operations()

    result = await operations.cache_clear({}, object())

    assert result == {"cleared": True}
    assert session.cache_clears == 1
    assert session.events == [ServiceEventKind.RESYNC_REQUIRED]


@pytest.mark.asyncio
async def test_editor_export_is_bound_to_exact_issued_job() -> None:
    operations, session, reservations, _review_handle, job_handle = _operations()

    result = await operations.job_log_export({"job": job_handle}, object())

    assert result == {
        "status": "ready",
        "message": "The configured editor launch plan is ready.",
        "job": job_handle,
        "job_id": 31,
        "argv": ["code", "--wait"],
        "content": "safe log\n",
        "slot": 2,
        "token": reservations.reserved[0][1],
        "export_name": (f"tongs-slot-2-job-31-{reservations.reserved[0][1]}.log"),
    }
    assert session.job_reads == [JOB]


@pytest.mark.asyncio
async def test_editor_disabled_is_reported_without_fetching_log() -> None:
    operations, session, _reservations, _review_handle, job_handle = _operations(
        Session(Config(editor_command="code --wait", external_editor_enabled=False))
    )

    result = await operations.job_log_export({"job": job_handle}, object())

    assert result["status"] == "disabled"  # type: ignore[index]
    assert result["argv"] == []  # type: ignore[index]
    assert result["content"] is None  # type: ignore[index]
    assert result["slot"] is None  # type: ignore[index]
    assert session.job_reads == []


@pytest.mark.asyncio
async def test_editor_release_is_internal_token_scoped_mutation() -> None:
    operations, _session, reservations, _review_handle, _job_handle = _operations()
    token = "a" * 32

    result = await operations.job_log_release({"slot": 2, "token": token}, object())

    assert result == {"released": True}
    assert reservations.released == [(2, token)]
    with pytest.raises(ProtocolError) as caught:
        await operations.job_log_release(
            {"slot": 2, "token": token, "path": "/tmp/other"}, object()
        )
    assert caught.value.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_malformed_utility_payload_fails_before_session_access() -> None:
    operations, session, _reservations, review_handle, _job_handle = _operations()

    with pytest.raises(ProtocolError) as caught:
        await operations.review_url(
            {"review": review_handle, "url": "https://example.com"}, object()
        )

    assert caught.value.code is ProtocolErrorCode.INVALID_PARAMS
    assert session.review_reads == []
