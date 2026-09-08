"""Failure-oriented tests for durable review draft submission."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from tongs.errors import AuthError, NetworkError
from tongs.forges.models import (
    CIStatus,
    Discussion,
    ForgeHost,
    ForgeMutationResult,
    MRDetail,
    MRState,
    ReviewDecision,
    User,
)
from tongs.forges.models import (
    InlineComment as ForgeInlineComment,
)
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ForgeCapabilities,
    RawDiffSnapshot,
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.review_mutations import (
    MutationOutcome,
    MutationReceipt,
    MutationStatus,
    ReviewMutationService,
)
from tongs.services.review_submission import (
    ReviewSubmissionService,
    SubmissionOutcome,
    SubmissionStepKind,
)
from tongs.state.drafts import (
    DiffSide,
    DraftConflictError,
    DraftContent,
    DraftState,
    DraftStateError,
    DraftStore,
    DraftStoreError,
    DraftVerdict,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
    context_fingerprint,
)

REVISION = ReviewRevision("head-1", "base-1", "start-1")
REF = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 42)
PATCH = "@@ -10,2 +10,2 @@\n context\n-old\n+new"


def _snapshot(
    forge: ForgeType = ForgeType.GITHUB,
    revision: ReviewRevision | None = REVISION,
    *,
    batched_review: bool = True,
) -> ReviewSnapshot:
    hostname = "github.com" if forge is ForgeType.GITHUB else "gitlab.example.com"
    ref = ReviewRef(RepositoryRef(hostname, "acme/widgets"), 42)
    detail = MRDetail(
        forge_host=ForgeHost(hostname, forge, ""),
        repo_path="acme/widgets",
        local_path="",
        number=42,
        title="Review",
        author=User("alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        web_url="",
        head_sha=revision.head_sha if revision else "",
        base_sha=revision.base_sha if revision else "",
        start_sha=revision.start_sha if revision else None,
    )
    if revision is None:
        error = ServiceError(
            ServiceErrorCode.REVISION_UNAVAILABLE, "Revision unavailable."
        )
        return ReviewSnapshot(
            ref,
            detail,
            None,
            ForgeCapabilities(batched_review, True, True, True, True),
            error,
        )
    return ReviewSnapshot(
        ref,
        detail,
        revision,
        ForgeCapabilities(batched_review, True, True, True, True),
    )


def _anchor(revision: ReviewRevision = REVISION) -> InlineAnchor:
    return InlineAnchor(
        revision,
        "old.py",
        "new.py",
        None,
        11,
        DiffSide.NEW,
        context_fingerprint(("context", "new")),
    )


def _content(
    *comments: GeneralDraftComment | InlineDraftComment | ReplyDraftComment,
    body: str = "",
    verdict: DraftVerdict | None = None,
) -> DraftContent:
    return DraftContent(body, verdict, tuple(comments))


def _client(**overrides: object) -> SimpleNamespace:
    methods: dict[str, object] = {
        "add_comment": AsyncMock(
            return_value=ForgeMutationResult("general-remote", "general-remote")
        ),
        "create_inline_comment": AsyncMock(
            return_value=ForgeMutationResult(
                "inline-remote", "inline-note", "inline-thread"
            )
        ),
        "reply_to_discussion": AsyncMock(
            return_value=ForgeMutationResult("reply-remote", "reply-note", "thread-9")
        ),
        "submit_review": AsyncMock(return_value=ForgeMutationResult("review-remote")),
        "invalidate_review_reads": AsyncMock(return_value=True),
        "supports_thread_resolution": True,
        "supports_batched_review": True,
    }
    methods.update(overrides)
    return SimpleNamespace(**methods)


def _discussion() -> Discussion:
    root = ForgeInlineComment(
        "root-7", User("alice"), "root", datetime.now(UTC), "new.py"
    )
    return Discussion("thread-9", True, root, resolvable=True)


def _mutation_service(
    client: SimpleNamespace,
    snapshot: ReviewSnapshot,
    *,
    timeout: float = 0.1,
    emit_change: Callable[[ServiceEventKind, ReviewRef, ReviewRevision | None], None]
    | None = None,
) -> ReviewMutationService:
    if snapshot.detail.forge_host.forge_type is ForgeType.GITHUB:
        change = {
            "filename": "new.py",
            "previous_filename": "old.py",
            "status": "modified",
            "patch": PATCH,
        }
    else:
        change = {"old_path": "old.py", "new_path": "new.py", "diff": PATCH}
    return ReviewMutationService(
        get_client=AsyncMock(return_value=client),
        get_review=AsyncMock(return_value=snapshot),
        get_diff=AsyncMock(
            return_value=RawDiffSnapshot(snapshot.ref, REVISION, (change,))
        ),
        get_discussions=AsyncMock(return_value=(_discussion(),)),
        emit_change=emit_change or Mock(),
        timeout=timeout,
    )


async def _services(
    db_path: Path,
    content: DraftContent,
    *,
    forge: ForgeType = ForgeType.GITHUB,
    client: SimpleNamespace | None = None,
    snapshot: ReviewSnapshot | None = None,
    emit_change: Callable[[ServiceEventKind, ReviewRef, ReviewRevision | None], None]
    | None = None,
) -> tuple[
    DraftStore,
    ReviewSubmissionService,
    SimpleNamespace,
    ReviewSnapshot,
    object,
]:
    current = snapshot or _snapshot(forge)
    actual_client = client or _client()
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(current.ref, REVISION, content)
    mutations = _mutation_service(actual_client, current, emit_change=emit_change)
    service = ReviewSubmissionService(
        store=store,
        mutations=mutations,
        get_review=AsyncMock(return_value=current),
    )
    return store, service, actual_client, current, draft


@pytest.mark.asyncio
async def test_github_inline_content_uses_one_native_revision_bound_batch(
    tmp_path: Path,
) -> None:
    inline = InlineDraftComment(uuid4(), "inline", _anchor())
    store, service, client, _snapshot_value, draft = await _services(
        tmp_path / "drafts.db",
        _content(inline, body="summary", verdict=DraftVerdict.APPROVE),
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert progress.atomic is True
    assert [(step.id, step.kind, step.comment_ids) for step in progress.steps] == [
        ("batch", SubmissionStepKind.GITHUB_REVIEW, (inline.id,))
    ]
    client.submit_review.assert_awaited_once_with(
        "acme/widgets",
        42,
        ReviewDecision.APPROVED,
        "summary",
        [
            {
                "path": "new.py",
                "line": 11,
                "side": "RIGHT",
                "body": "inline",
            }
        ],
        head_sha="head-1",
    )
    assert progress.receipts[0].remote_id == "review-remote"
    await store.close()


@pytest.mark.asyncio
async def test_github_mixed_content_preserves_order_then_one_final_review(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    async def general(*_args: object) -> ForgeMutationResult:
        order.append("general")
        return ForgeMutationResult("general", "general")

    async def inline(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("inline")
        return ForgeMutationResult("inline", "inline", "inline-thread")

    async def reply(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("reply")
        return ForgeMutationResult("reply", "reply", "thread-9")

    async def review(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("review")
        return ForgeMutationResult("review")

    client = _client(
        add_comment=AsyncMock(side_effect=general),
        create_inline_comment=AsyncMock(side_effect=inline),
        reply_to_discussion=AsyncMock(side_effect=reply),
        submit_review=AsyncMock(side_effect=review),
    )
    comments = (
        GeneralDraftComment(uuid4(), "general"),
        InlineDraftComment(uuid4(), "inline", _anchor()),
        ReplyDraftComment(uuid4(), "reply", "thread-9"),
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(*comments, body="summary", verdict=DraftVerdict.REQUEST_CHANGES),
        client=client,
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert progress.atomic is False
    assert order == ["general", "inline", "reply", "review"]
    client.add_comment.assert_awaited_once_with("acme/widgets", 42, "general")
    client.submit_review.assert_awaited_once_with(
        "acme/widgets",
        42,
        ReviewDecision.CHANGES_REQUESTED,
        "summary",
        None,
        head_sha="head-1",
    )
    await store.close()


@pytest.mark.asyncio
async def test_github_mixed_comment_only_does_not_create_empty_review(
    tmp_path: Path,
) -> None:
    comments = (
        GeneralDraftComment(uuid4(), "general"),
        InlineDraftComment(uuid4(), "inline", _anchor()),
    )
    store, service, client, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(*comments, verdict=DraftVerdict.COMMENT),
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert [step.kind for step in progress.steps] == [
        SubmissionStepKind.GENERAL_COMMENT,
        SubmissionStepKind.INLINE_COMMENT,
    ]
    client.submit_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_gitlab_known_comment_rejection_pauses_before_verdict_and_explicit_resume(
    tmp_path: Path,
) -> None:
    inline = AsyncMock(side_effect=AuthError("secret"))
    client = _client(create_inline_comment=inline)
    snapshot = _snapshot(ForgeType.GITLAB)
    comment = InlineDraftComment(uuid4(), "inline", _anchor())
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(comment, verdict=DraftVerdict.APPROVE),
        client=client,
        snapshot=snapshot,
    )

    paused = await service.start(draft.id, draft.version)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.code == "authentication_failed"
    client.submit_review.assert_not_awaited()
    inline.side_effect = None
    inline.return_value = ForgeMutationResult("inline", "inline", "thread")

    submitted = await service.resume(paused.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert inline.await_count == 2
    client.submit_review.assert_awaited_once()
    recovered = await store.get_attempt(paused.attempt_id)
    assert [
        (item.ordinal, item.step_id) for item in recovered.retry_authorizations
    ] == [(1, paused.failure.step_id)]
    await store.close()


@pytest.mark.asyncio
async def test_gitlab_request_changes_fails_preflight_without_remote_write(
    tmp_path: Path,
) -> None:
    client = _client()
    snapshot = _snapshot(ForgeType.GITLAB)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(
            GeneralDraftComment(uuid4(), "general"),
            verdict=DraftVerdict.REQUEST_CHANGES,
        ),
        client=client,
        snapshot=snapshot,
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code is ServiceErrorCode.UNSUPPORTED
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    client.add_comment.assert_not_awaited()
    client.submit_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_whole_plan_anchor_preflight_precedes_first_remote_write(
    tmp_path: Path,
) -> None:
    general = GeneralDraftComment(uuid4(), "general first")
    invalid_anchor = InlineAnchor(
        REVISION,
        "old.py",
        "new.py",
        None,
        999,
        DiffSide.NEW,
        context_fingerprint(("missing",)),
    )
    inline = InlineDraftComment(uuid4(), "invalid inline", invalid_anchor)
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(general, inline), client=client
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code is ServiceErrorCode.INVALID_INPUT
    client.add_comment.assert_not_awaited()
    client.create_inline_comment.assert_not_awaited()
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    await store.close()


@pytest.mark.asyncio
async def test_get_reconstructs_durable_plan_without_network_read(
    tmp_path: Path,
) -> None:
    comment = GeneralDraftComment(uuid4(), "comment")
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(comment)
    )
    submitted = await service.start(draft.id, draft.version)
    service._get_review = AsyncMock(
        side_effect=AssertionError("unexpected network read")
    )

    current = await service.get(submitted.attempt_id)

    assert current.steps == submitted.steps
    assert current.atomic == submitted.atomic
    assert current.plan_available is True
    service._get_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_get_distinguishes_recovered_attempt_without_a_plan(
    tmp_path: Path,
) -> None:
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
    )
    attempt = await store.lock_submission(draft.id, draft.version)
    service._get_review = AsyncMock(
        side_effect=AssertionError("unexpected network read")
    )

    current = await service.get(attempt.id)

    assert current.steps == ()
    assert current.plan_available is False
    service._get_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(revision=None),
        _snapshot(revision=ReviewRevision("new-head", "base-1", "start-1")),
        _snapshot(ForgeType.GITLAB, revision=ReviewRevision("head-1", "base-1", None)),
    ],
)
async def test_incomplete_or_changed_revision_unlocks_without_dispatch(
    tmp_path: Path, snapshot: ReviewSnapshot
) -> None:
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / f"{snapshot.ref.repository.hostname}.db",
        _content(GeneralDraftComment(uuid4(), "general")),
        client=client,
        snapshot=snapshot,
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code in {
        ServiceErrorCode.REVISION_UNAVAILABLE,
        ServiceErrorCode.REVISION_CHANGED,
    }
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    client.add_comment.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_unknown_step_requires_reconciliation_and_never_repeats_receipt(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    add = AsyncMock(
        side_effect=[
            ForgeMutationResult("first", "first"),
            NetworkError("timeout"),
            ForgeMutationResult("second", "second"),
        ]
    )
    client = _client(add_comment=add)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(first, second), client=client
    )

    unknown = await service.start(draft.id, draft.version)

    assert unknown.outcome is SubmissionOutcome.UNKNOWN
    assert unknown.completed_step_ids == (f"comment:{first.id.hex}",)
    assert unknown.unknown_step_ids == (f"comment:{second.id.hex}",)
    durable = await service.get(unknown.attempt_id)
    assert durable.steps == unknown.steps
    assert durable.resync_required is True
    assert durable.plan_available is True
    with pytest.raises(ServiceError) as raised:
        await service.resume(unknown.attempt_id)
    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert add.await_count == 2

    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETRY_REMAINING
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 3
    assert [call.args[2] for call in add.await_args_list] == [
        "first",
        "second",
        "second",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_return_editable_retains_ambiguous_remainder_and_frozen_evidence(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    add = AsyncMock(
        side_effect=[ForgeMutationResult("first", "first"), NetworkError("timeout")]
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(first, second, body="summary", verdict=DraftVerdict.APPROVE),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)

    editable = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )
    saved = await store.get_draft(draft.id)
    historical = await store.get_attempt(unknown.attempt_id)

    assert editable.outcome is SubmissionOutcome.EDITABLE
    assert saved.comments == (second,)
    assert saved.body == "summary"
    assert saved.verdict is DraftVerdict.APPROVE
    assert historical.snapshot.comments == (first, second)
    assert historical.receipts[0].step_id == f"comment:{first.id.hex}"
    assert historical.unknown_outcomes[0].step_id == f"comment:{second.id.hex}"
    await store.close()


@pytest.mark.asyncio
async def test_mark_submitted_records_explicit_decision_without_replay(
    tmp_path: Path,
) -> None:
    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)

    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.MARK_SUBMITTED
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 1
    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_confirmed_remote_result_with_receipt_write_failure_becomes_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, client, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
    )

    async def fail_receipt(*_args: object, **_kwargs: object) -> object:
        raise DraftStoreError("disk full")

    monkeypatch.setattr(store, "record_receipt", fail_receipt)
    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    assert progress.failure and progress.failure.code == "storage_failed"
    assert progress.unknown_step_ids == (progress.steps[0].id,)
    client.add_comment.assert_awaited_once()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_failure", ["cache", "event"])
async def test_known_resync_requirement_survives_completion_get_and_reopen(
    tmp_path: Path, refresh_failure: str
) -> None:
    path = tmp_path / "drafts.db"
    result = ForgeMutationResult(
        "general-remote",
        "general-remote",
        cache_invalidated=refresh_failure != "cache",
    )
    client = _client(add_comment=AsyncMock(return_value=result))
    emit_change = (
        Mock(side_effect=RuntimeError("event delivery failed"))
        if refresh_failure == "event"
        else Mock()
    )
    store, service, _, snapshot, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
        emit_change=emit_change,
    )

    submitted = await service.start(draft.id, draft.version)
    current = await service.get(submitted.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert submitted.resync_required is True
    assert current.resync_required is True
    assert current.receipts[0].resync_required is True
    await store.close()

    reopened_store = DraftStore(path)
    await reopened_store.open()
    reopened_service = ReviewSubmissionService(
        store=reopened_store,
        mutations=_mutation_service(_client(), snapshot),
        get_review=AsyncMock(side_effect=AssertionError("unexpected network read")),
    )

    reopened = await reopened_service.get(submitted.attempt_id)

    assert reopened.outcome is SubmissionOutcome.SUBMITTED
    assert reopened.resync_required is True
    assert reopened.receipts[0].resync_required is True
    reopened_service._get_review.assert_not_awaited()
    await reopened_store.close()


@pytest.mark.asyncio
async def test_known_resync_requirement_survives_later_rejection(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    client = _client(
        add_comment=AsyncMock(
            side_effect=[
                ForgeMutationResult(
                    "first-remote", "first-remote", cache_invalidated=False
                ),
                AuthError("rejected"),
            ]
        )
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(first, second), client=client
    )

    paused = await service.start(draft.id, draft.version)
    current = await service.get(paused.attempt_id)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.code == "authentication_failed"
    assert paused.resync_required is True
    assert current.resync_required is True
    assert current.receipts[0].resync_required is True
    await store.close()


@pytest.mark.asyncio
async def test_failed_final_transition_retains_ownership_for_same_session_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    original = store._transition_owned
    failed = False

    async def fail_first_completion(
        attempt_id: object, target: DraftState, **kwargs: object
    ) -> object:
        nonlocal failed
        if target is DraftState.SUBMITTED and not failed:
            failed = True
            raise DraftStoreError("temporary final write failure")
        return await original(attempt_id, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_transition_owned", fail_first_completion)

    paused = await service.start(draft.id, draft.version)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.retryable is True
    assert paused.attempt_id in store._held_attempt_locks

    submitted = await service.resume(paused.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    client.add_comment.assert_awaited_once()
    assert paused.attempt_id not in store._held_attempt_locks
    await store.close()


@pytest.mark.asyncio
async def test_restart_recovers_failed_final_transition_without_remote_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    client = _client()
    store, service, _, snapshot, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    original = store._transition_owned
    failed = False

    async def fail_first_completion(
        attempt_id: object, target: DraftState, **kwargs: object
    ) -> object:
        nonlocal failed
        if target is DraftState.SUBMITTED and not failed:
            failed = True
            raise DraftStoreError("temporary final write failure")
        return await original(attempt_id, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_transition_owned", fail_first_completion)
    paused = await service.start(draft.id, draft.version)
    await store.close()

    restarted_store = DraftStore(path)
    await restarted_store.open()
    recovered = await restarted_store.recover_incomplete_attempts()
    restarted_service = ReviewSubmissionService(
        store=restarted_store,
        mutations=_mutation_service(client, snapshot),
        get_review=AsyncMock(return_value=snapshot),
    )

    assert recovered[0].id == paused.attempt_id
    assert recovered[0].state is DraftState.UNKNOWN
    submitted = await restarted_service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETRY_REMAINING
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    client.add_comment.assert_awaited_once()
    await restarted_store.close()


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_returns_durable_unknown_without_replay(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def hang(*_args: object) -> ForgeMutationResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = _client(add_comment=AsyncMock(side_effect=hang))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    task = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    task.cancel()
    progress = await task

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    assert client.add_comment.await_count == 1
    with pytest.raises(ServiceError):
        await service.resume(progress.attempt_id)
    assert client.add_comment.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_submission_and_rejects_new_work(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def hang(*_args: object) -> ForgeMutationResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=AsyncMock(side_effect=hang)),
    )
    owner = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    await service.close()
    progress = await owner

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    with pytest.raises(ServiceError) as raised:
        await service.get(progress.attempt_id)
    assert raised.value.code is ServiceErrorCode.CLOSED
    assert service._active_tasks == set()
    await store.close()


@pytest.mark.asyncio
async def test_close_retries_cancellation_of_submission_owner(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class FirstCancelResistantMutations:
        async def validate(self, _command: object) -> None:
            return None

        async def execute(self, _command: object) -> object:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = DraftStore(tmp_path / "drafts.db")
    await store.open()
    draft = await store.create_draft(
        REF, REVISION, _content(GeneralDraftComment(uuid4(), "comment"))
    )
    service = ReviewSubmissionService(
        store=store,
        mutations=FirstCancelResistantMutations(),  # type: ignore[arg-type]
        get_review=AsyncMock(return_value=_snapshot()),
        close_timeout=0.01,
    )
    owner = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    await service.close()

    with pytest.raises(asyncio.CancelledError):
        await owner
    attempt = (await store.list_recovery_attempts())[0]
    assert attempt.unknown_outcomes[0].step_id.startswith("comment:")
    assert service._active_tasks == set()
    await store.close()


@pytest.mark.asyncio
async def test_failed_close_can_finish_after_uncooperative_owner_settles(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class ResistantMutations:
        async def validate(self, _command: object) -> None:
            return None

        async def execute(self, command: object) -> MutationOutcome:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            operation_id = command.operation_id  # type: ignore[attr-defined]
            return MutationOutcome(
                operation_id,
                MutationStatus.KNOWN,
                MutationReceipt(operation_id, "known"),
            )

    store = DraftStore(tmp_path / "drafts.db")
    await store.open()
    draft = await store.create_draft(
        REF, REVISION, _content(GeneralDraftComment(uuid4(), "comment"))
    )
    service = ReviewSubmissionService(
        store=store,
        mutations=ResistantMutations(),  # type: ignore[arg-type]
        get_review=AsyncMock(return_value=_snapshot()),
        close_timeout=0.01,
    )
    owner = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    with pytest.raises(RuntimeError, match="did not stop"):
        await service.close()
    assert owner in service._active_tasks

    release.set()
    assert (await owner).outcome is SubmissionOutcome.SUBMITTED
    await service.close()
    assert service._active_tasks == set()
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_start_creates_one_attempt_and_one_remote_call(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_comment(*_args: object) -> ForgeMutationResult:
        entered.set()
        await release.wait()
        return ForgeMutationResult("known", "known")

    client = _client(add_comment=AsyncMock(side_effect=blocked_comment))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    first = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()
    second = asyncio.create_task(service.start(draft.id, draft.version))
    result = await asyncio.gather(second, return_exceptions=True)
    release.set()
    submitted = await first

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert isinstance(result[0], (DraftConflictError, DraftStateError))
    assert client.add_comment.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_explicit_resume_advances_rejected_step_once(
    tmp_path: Path,
) -> None:
    add = AsyncMock(side_effect=AuthError("denied"))
    client = _client(add_comment=add)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    paused = await service.start(draft.id, draft.version)
    add.side_effect = None
    add.return_value = ForgeMutationResult("known", "known")

    first, second = await asyncio.gather(
        service.resume(paused.attempt_id), service.resume(paused.attempt_id)
    )

    assert first.outcome is SubmissionOutcome.SUBMITTED
    assert second.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 2
    recovered = await store.get_attempt(paused.attempt_id)
    assert len(recovered.retry_authorizations) == 1
    await store.close()
