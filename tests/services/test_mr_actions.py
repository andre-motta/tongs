"""Failure-oriented tests for revision-bound merge request actions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from tongs.errors import AuthError, NetworkError
from tongs.forges.http import map_http_error
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    MRDetail,
    MRState,
    SourceCleanupStatus,
    User,
)
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    ForgeCapabilities,
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)
from tongs.services.mr_actions import (
    CloseReviewCommand,
    MergeReviewCommand,
    MRAction,
    MRActionOutcome,
    MRActionService,
    ReopenReviewCommand,
    ReviewActionTarget,
    SourceBranchTarget,
    UnapproveReviewCommand,
)

REPOSITORY = RepositoryRef("github.com", "acme/widgets")
REVIEW = ReviewRef(REPOSITORY, 42)
REVISION = ReviewRevision("head-42", "base-42")


def _snapshot(
    *,
    state: MRState = MRState.OPEN,
    revision: ReviewRevision | None = REVISION,
    forge: ForgeType = ForgeType.GITHUB,
) -> ReviewSnapshot:
    detail = MRDetail(
        forge_host=ForgeHost(REPOSITORY.hostname, forge, ""),
        repo_path=REPOSITORY.project_path,
        local_path="",
        number=REVIEW.number,
        title="Review",
        author=User("alice"),
        state=state,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        web_url="",
        head_sha=revision.head_sha if revision else "",
        base_sha=revision.base_sha if revision else "",
    )
    error = (
        None
        if revision is not None
        else ServiceError(
            ServiceErrorCode.REVISION_UNAVAILABLE,
            "The review revision is unavailable.",
        )
    )
    return ReviewSnapshot(
        REVIEW,
        detail,
        revision,
        ForgeCapabilities(True, True, True, True, True),
        error,
    )


def _client(**overrides):
    methods = {
        "merge_mr": AsyncMock(
            return_value=ForgeMergeResult(
                "merge-42", "merged-sha", SourceCleanupStatus.CONFIRMED
            )
        ),
        "close_mr": AsyncMock(return_value=ForgeMutationResult("review-42")),
        "reopen_mr": AsyncMock(return_value=ForgeMutationResult("review-42")),
        "unapprove_mr": AsyncMock(return_value=ForgeMutationResult("review-42")),
        "supports_unapprove": True,
    }
    methods.update(overrides)
    return SimpleNamespace(**methods)


def _service(client, *, snapshot=None, emit=None, close_timeout=0.01, size=16):
    current = snapshot or _snapshot()
    get_review = AsyncMock(return_value=current)
    get_client = AsyncMock(return_value=client)
    emitter = emit or Mock()
    service = MRActionService(
        get_client=get_client,
        get_review=get_review,
        emit_change=emitter,
        max_operations=size,
        close_timeout=close_timeout,
    )
    return service, get_client, get_review, emitter


def _target(*, state: MRState = MRState.OPEN) -> ReviewActionTarget:
    return ReviewActionTarget(REVIEW, REVISION, state)


@pytest.mark.asyncio
async def test_merge_binds_fresh_revision_options_and_exact_cleanup_target() -> None:
    client = _client()
    service, get_client, get_review, emitter = _service(client)
    command = MergeReviewCommand(
        "merge:42:head",
        _target(),
        squash=True,
        source_cleanup=SourceBranchTarget(REPOSITORY, "feature"),
    )

    first = await service.execute(command)
    repeated = await service.execute(command)

    assert first == repeated
    assert first.outcome is MRActionOutcome.KNOWN
    assert first.merge_sha == "merged-sha"
    assert first.source_cleanup is SourceCleanupStatus.CONFIRMED
    assert first.resync_required is False
    get_review.assert_awaited_once_with(REVIEW)
    get_client.assert_awaited_once_with(REVIEW, "mr_merge")
    client.merge_mr.assert_awaited_once_with(
        "acme/widgets",
        42,
        True,
        True,
        head_sha="head-42",
        expected_source_repository="acme/widgets",
        expected_source_branch="feature",
        expected_target_branch="main",
    )
    emitter.assert_called_once_with(ServiceEventKind.REVIEW_CHANGED, REVIEW, REVISION)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "target", "code"),
    [
        (
            _snapshot(revision=ReviewRevision("changed", "base-42")),
            _target(),
            ServiceErrorCode.REVISION_CHANGED,
        ),
        (_snapshot(state=MRState.CLOSED), _target(), ServiceErrorCode.CONFLICT),
        (_snapshot(revision=None), _target(), ServiceErrorCode.REVISION_UNAVAILABLE),
        (_snapshot(), _target(state=MRState.CLOSED), ServiceErrorCode.INVALID_INPUT),
    ],
)
async def test_fresh_revision_and_state_fail_before_dispatch(
    snapshot: ReviewSnapshot,
    target: ReviewActionTarget,
    code: ServiceErrorCode,
) -> None:
    client = _client()
    service, *_ = _service(client, snapshot=snapshot)

    with pytest.raises(ServiceError) as raised:
        await service.execute(MergeReviewCommand("merge-conflict", target))

    assert raised.value.code is code
    client.merge_mr.assert_not_awaited()


@pytest.mark.asyncio
async def test_inconsistent_fresh_snapshot_fails_before_dispatch() -> None:
    snapshot = _snapshot()
    inconsistent = ReviewSnapshot(
        snapshot.ref,
        replace(snapshot.detail, head_sha="different"),
        snapshot.revision,
        snapshot.capabilities,
    )
    client = _client()
    service, *_ = _service(client, snapshot=inconsistent)

    with pytest.raises(ServiceError) as raised:
        await service.execute(CloseReviewCommand("inconsistent", _target()))

    assert raised.value.code is ServiceErrorCode.INVALID_RESPONSE
    client.close_mr.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup",
    [
        SourceBranchTarget(RepositoryRef("github.com", "other/fork"), "feature"),
        SourceBranchTarget(REPOSITORY, "changed"),
        SourceBranchTarget(REPOSITORY, "main"),
    ],
)
async def test_unsafe_source_cleanup_fails_before_dispatch(
    cleanup: SourceBranchTarget,
) -> None:
    client = _client()
    service, *_ = _service(client)

    with pytest.raises(ServiceError) as raised:
        await service.execute(
            MergeReviewCommand("unsafe-cleanup", _target(), source_cleanup=cleanup)
        )

    assert raised.value.code is ServiceErrorCode.CONFLICT
    client.merge_mr.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_exact_repeat_coalesces_and_id_rebind_conflicts() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def merge(*args, **kwargs):
        entered.set()
        await release.wait()
        return ForgeMergeResult("merge-42", "merged-sha")

    client = _client(merge_mr=AsyncMock(side_effect=merge))
    service, *_ = _service(client)
    command = MergeReviewCommand("same-operation", _target())
    owner = asyncio.create_task(service.execute(command))
    await entered.wait()
    duplicate = asyncio.create_task(service.execute(command))

    with pytest.raises(ServiceError) as raised:
        await service.execute(CloseReviewCommand("same-operation", _target()))
    assert raised.value.code is ServiceErrorCode.CONFLICT

    release.set()
    assert await owner == await duplicate
    client.merge_mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_failure_after_dispatch_is_unknown_and_not_replayed() -> None:
    client = _client(merge_mr=AsyncMock(side_effect=NetworkError("secret")))
    service, *_rest, emitter = _service(client)
    command = MergeReviewCommand("unknown-merge", _target())

    first = await service.execute(command)
    repeated = await service.execute(command)

    assert first == repeated
    assert first.outcome is MRActionOutcome.UNKNOWN
    assert first.error is not None
    assert first.error.code is ServiceErrorCode.NETWORK_UNAVAILABLE
    assert first.remote_id is None
    assert first.resync_required is True
    client.merge_mr.assert_awaited_once()
    emitter.assert_called_once_with(ServiceEventKind.RESYNC_REQUIRED, REVIEW, REVISION)


@pytest.mark.asyncio
async def test_malformed_post_dispatch_result_is_unknown_and_completes_duplicate() -> (
    None
):
    client = _client(close_mr=AsyncMock(return_value=None))
    service, *_ = _service(client)
    command = CloseReviewCommand("malformed-result", _target())

    first = await service.execute(command)
    repeated = await asyncio.wait_for(service.execute(command), timeout=0.1)

    assert first == repeated
    assert first.outcome is MRActionOutcome.UNKNOWN
    assert first.error and first.error.code is ServiceErrorCode.INVALID_RESPONSE
    client.close_mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_definite_rejection_is_safe_retained_and_not_replayed() -> None:
    client = _client(close_mr=AsyncMock(side_effect=AuthError("token=secret")))
    service, *_ = _service(client)
    command = CloseReviewCommand("known-rejection", _target())

    for _ in range(2):
        with pytest.raises(ServiceError) as raised:
            await service.execute(command)
        assert raised.value.code is ServiceErrorCode.AUTHENTICATION_FAILED
        assert "secret" not in str(raised.value)
    client.close_mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_merge_405_conflict_is_known_rejection_not_unknown_receipt() -> None:
    """GitHub's merge endpoint answers a blocked merge with 405 and a reason,
    e.g. {"message": "Pull Request has merge conflicts"} (#229). That must
    surface as a known, typed rejection the same way a 409 does: no unknown
    receipt, no resync required, no mutation lock. The dispatch-level
    `ForgeError` carries the forge's reason, but the translated, user-visible
    `ServiceError` must not: `translate_error` never copies exception text
    across the service boundary.
    """
    reason = httpx.Response(405, json={"message": "Pull Request has merge conflicts"})
    dispatch_error = map_http_error(reason)
    assert "Pull Request has merge conflicts" in str(dispatch_error)
    client = _client(merge_mr=AsyncMock(side_effect=dispatch_error))
    service, *_rest, emitter = _service(client)
    command = MergeReviewCommand("merge-405", _target())

    for _ in range(2):
        with pytest.raises(ServiceError) as raised:
            await service.execute(command)
        assert raised.value.code is ServiceErrorCode.CONFLICT
        assert "Pull Request has merge conflicts" not in str(raised.value)

    client.merge_mr.assert_awaited_once()
    emitter.assert_not_called()
    assert await service.receipt("merge-405") is None


@pytest.mark.asyncio
async def test_post_dispatch_cancellation_is_retained_unknown() -> None:
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client = _client(close_mr=AsyncMock(side_effect=blocked))
    service, *_ = _service(client)
    command = CloseReviewCommand("cancelled-owner", _target())
    owner = asyncio.create_task(service.execute(command))
    await entered.wait()
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    retained = await service.execute(command)

    assert retained.outcome is MRActionOutcome.UNKNOWN
    assert retained.resync_required is True
    client.close_mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_cleanup_unknown_retains_confirmed_merge_and_requests_resync() -> (
    None
):
    client = _client(
        merge_mr=AsyncMock(
            return_value=ForgeMergeResult(
                "merge-42", "merged-sha", SourceCleanupStatus.UNKNOWN
            )
        )
    )
    service, *_ = _service(client)
    command = MergeReviewCommand(
        "partial-merge",
        _target(),
        source_cleanup=SourceBranchTarget(REPOSITORY, "feature"),
    )

    result = await service.execute(command)

    assert result.outcome is MRActionOutcome.KNOWN
    assert result.merge_sha == "merged-sha"
    assert result.source_cleanup is SourceCleanupStatus.UNKNOWN
    assert result.resync_required is True


@pytest.mark.asyncio
async def test_lifecycle_capabilities_and_native_results() -> None:
    client = _client()
    open_service, *_ = _service(client)
    closed_service, *_ = _service(client, snapshot=_snapshot(state=MRState.CLOSED))

    open_capabilities = await open_service.capabilities(REVIEW)
    assert open_capabilities.merge is True
    assert open_capabilities.close is True
    assert open_capabilities.reopen is False
    assert open_capabilities.unapprove is True
    assert (await closed_service.capabilities(REVIEW)).reopen is True

    assert (
        await open_service.execute(UnapproveReviewCommand("unapprove", _target()))
    ).action is MRAction.UNAPPROVE
    assert (
        await open_service.execute(CloseReviewCommand("close", _target()))
    ).action is MRAction.CLOSE
    closed_target = _target(state=MRState.CLOSED)
    assert (
        await closed_service.execute(ReopenReviewCommand("reopen", closed_target))
    ).action is MRAction.REOPEN


@pytest.mark.asyncio
async def test_close_retries_cancellation_and_finishes_exact_duplicate() -> None:
    entered = asyncio.Event()
    cancellations = 0

    async def resistant(*args, **kwargs):
        nonlocal cancellations
        entered.set()
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations += 1
                if cancellations >= 2:
                    raise

    client = _client(close_mr=AsyncMock(side_effect=resistant))
    service, *_ = _service(client)
    command = CloseReviewCommand("close-owner", _target())
    owner = asyncio.create_task(service.execute(command))
    await entered.wait()
    duplicate = asyncio.create_task(service.execute(command))
    await asyncio.sleep(0)

    await service.close()

    with pytest.raises(asyncio.CancelledError):
        await owner
    retained = await asyncio.wait_for(duplicate, timeout=0.1)
    assert retained.outcome is MRActionOutcome.UNKNOWN
    assert cancellations == 2


@pytest.mark.asyncio
async def test_close_is_bounded_and_keeps_resistant_owner_identifiable() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return ForgeMutationResult("review-42")

    client = _client(close_mr=AsyncMock(side_effect=resistant))
    service, *_ = _service(client, close_timeout=0.001)
    owner = asyncio.create_task(
        service.execute(CloseReviewCommand("never-stops", _target()))
    )
    await entered.wait()

    with pytest.raises(RuntimeError, match="did not stop"):
        await asyncio.wait_for(service.close(), timeout=0.1)
    assert owner in service._owner_tasks

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    retained = service._record_result(service._operations["never-stops"])
    assert retained.outcome is MRActionOutcome.KNOWN
    assert not service._owner_tasks


@pytest.mark.asyncio
async def test_event_failure_marks_known_receipt_for_resync() -> None:
    emitter = Mock(side_effect=RuntimeError("subscriber failed"))
    service, *_ = _service(_client(), emit=emitter)

    result = await service.execute(CloseReviewCommand("event-failure", _target()))

    assert result.outcome is MRActionOutcome.KNOWN
    assert result.resync_required is True
