"""Failure-oriented tests for revision-bound review mutation primitives."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from tongs.cache.cached_client import CachedForgeClient
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
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    ForgeCapabilities,
    RawDiffSnapshot,
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)
from tongs.services.review_mutations import (
    DiffAnchor,
    DiffSide,
    GeneralComment,
    InlineComment,
    MutationStatus,
    Reply,
    Resolve,
    ReviewMutationService,
    ReviewVerdict,
)

REVISION = ReviewRevision("head", "base", "start")
REF = ReviewRef(RepositoryRef("gitlab.example.com", "acme/widgets"), 42)
PATCH = {
    "old_path": "old.py",
    "new_path": "new.py",
    "diff": "@@ -10,2 +10,2 @@\n context\n-old\n+new",
}


def _snapshot(forge: ForgeType = ForgeType.GITLAB, revision=REVISION) -> ReviewSnapshot:
    host = ForgeHost(REF.repository.hostname, forge, "")
    detail = MRDetail(
        forge_host=host,
        repo_path=REF.repository.project_path,
        local_path="",
        number=REF.number,
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
        head_sha=revision.head_sha,
        base_sha=revision.base_sha,
        start_sha=revision.start_sha,
    )
    return ReviewSnapshot(
        REF,
        detail,
        revision,
        ForgeCapabilities(forge == ForgeType.GITHUB, True, True, True, True),
    )


def _discussion() -> Discussion:
    root = ForgeInlineComment(
        "root-7", User("alice"), "root", datetime.now(UTC), "new.py"
    )
    return Discussion("thread-9", True, root, resolvable=True)


def _service(client, *, forge=ForgeType.GITLAB, emit=None, ledger_size=10):
    get_review = AsyncMock(return_value=_snapshot(forge))
    get_diff = AsyncMock(return_value=RawDiffSnapshot(REF, REVISION, (PATCH,)))
    get_discussions = AsyncMock(return_value=(_discussion(),))
    emitter = emit or Mock()
    service = ReviewMutationService(
        get_client=AsyncMock(return_value=client),
        get_review=get_review,
        get_diff=get_diff,
        get_discussions=get_discussions,
        emit_change=emitter,
        timeout=0.01,
        ledger_size=ledger_size,
    )
    return service, get_review, get_diff, get_discussions, emitter


def _client(**methods):
    defaults = {
        "add_comment": AsyncMock(return_value=ForgeMutationResult("note-1", "note-1")),
        "create_inline_comment": AsyncMock(
            return_value=ForgeMutationResult("thread-1", "note-2", "thread-1")
        ),
        "reply_to_discussion": AsyncMock(
            return_value=ForgeMutationResult("note-3", "note-3", "thread-9")
        ),
        "resolve_discussion": AsyncMock(
            return_value=ForgeMutationResult("thread-9", discussion_id="thread-9")
        ),
        "submit_review": AsyncMock(return_value=ForgeMutationResult("review-1")),
        "invalidate_review_reads": AsyncMock(return_value=True),
    }
    defaults.update(methods)
    return type("Client", (), defaults)()


@pytest.mark.asyncio
async def test_known_general_comment_is_replayed_from_operation_ledger() -> None:
    client = _client()
    service, *_ = _service(client)
    command = GeneralComment("desktop:quick:1", REF, "Looks good")

    first = await service.execute(command)
    second = await service.execute(command)

    assert first == second
    assert first.status == MutationStatus.KNOWN
    assert first.receipt and first.receipt.comment_id == "note-1"
    client.add_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_operation_id_cannot_be_rebound_to_different_payload() -> None:
    service, *_ = _service(_client())
    await service.execute(GeneralComment("same-id", REF, "first"))

    with pytest.raises(ServiceError, match="different mutation") as raised:
        await service.execute(GeneralComment("same-id", REF, "second"))
    assert raised.value.code == ServiceErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_definite_auth_rejection_is_safe_and_repeatable() -> None:
    client = _client(add_comment=AsyncMock(side_effect=AuthError("secret response")))
    service, *_ = _service(client)
    command = GeneralComment("auth-1", REF, "body")

    for _ in range(2):
        with pytest.raises(ServiceError) as raised:
            await service.execute(command)
        assert raised.value.code == ServiceErrorCode.AUTHENTICATION_FAILED
        assert "secret" not in str(raised.value)
    client.add_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_failure_after_dispatch_is_unknown_and_not_replayed() -> None:
    client = _client(add_comment=AsyncMock(side_effect=NetworkError("timeout")))
    service, *_rest, emitter = _service(client)
    command = GeneralComment("unknown-1", REF, "body")

    outcome = await service.execute(command)
    repeated = await service.execute(command)

    assert outcome == repeated
    assert outcome.status == MutationStatus.UNKNOWN
    assert outcome.resync_required is True
    client.add_comment.assert_awaited_once()
    client.invalidate_review_reads.assert_awaited_once_with("acme/widgets", 42)
    emitter.assert_called_with(ServiceEventKind.RESYNC_REQUIRED, REF, None)


@pytest.mark.asyncio
async def test_timeout_is_unknown_without_waiting_for_remote_replay() -> None:
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    client = _client(add_comment=AsyncMock(side_effect=hang))
    service, *_ = _service(client)
    outcome = await service.execute(GeneralComment("timeout-1", REF, "body"))
    assert outcome.status == MutationStatus.UNKNOWN
    assert outcome.reason == "TimeoutError"


@pytest.mark.asyncio
async def test_caller_cancellation_after_dispatch_is_retained_as_unknown() -> None:
    entered = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client = _client(add_comment=AsyncMock(side_effect=hang))
    service, *_ = _service(client)
    task = asyncio.create_task(service.execute(GeneralComment("cancel-1", REF, "body")))
    await entered.wait()
    task.cancel()

    outcome = await task

    assert outcome.status == MutationStatus.UNKNOWN
    assert outcome.reason == "CancelledError"
    assert outcome.resync_required is True


@pytest.mark.asyncio
async def test_malformed_post_write_receipt_is_unknown() -> None:
    client = _client(add_comment=AsyncMock(return_value=None))
    service, *_ = _service(client)

    outcome = await service.execute(GeneralComment("receipt-1", REF, "body"))

    assert outcome.status == MutationStatus.UNKNOWN
    assert outcome.reason == "invalid_receipt"
    client.invalidate_review_reads.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_receipt_survives_cache_and_event_failure() -> None:
    client = _client(
        add_comment=AsyncMock(
            return_value=ForgeMutationResult(
                "note-1", "note-1", cache_invalidated=False
            )
        )
    )
    emitter = Mock(side_effect=RuntimeError("session closed"))
    service, *_ = _service(client, emit=emitter)

    outcome = await service.execute(GeneralComment("known-1", REF, "body"))

    assert outcome.status == MutationStatus.KNOWN
    assert outcome.receipt and outcome.receipt.remote_id == "note-1"
    assert outcome.resync_required is True


@pytest.mark.asyncio
async def test_composed_cache_cleanup_cannot_hide_confirmed_receipt() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def cancellation_resistant_invalidation(_prefix: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    inner = _client()
    cache = Mock(
        invalidate_prefix=AsyncMock(side_effect=cancellation_resistant_invalidation)
    )
    wrapped = CachedForgeClient(inner, cache, REF.repository.hostname)
    service, *_ = _service(wrapped)

    outcome = await asyncio.wait_for(
        service.execute(GeneralComment("known-cache-1", REF, "body")), timeout=0.05
    )
    await entered.wait()

    assert outcome.status == MutationStatus.KNOWN
    assert outcome.receipt and outcome.receipt.remote_id == "note-1"
    assert outcome.resync_required is True
    assert calls == 2
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unknown_is_retained_before_cancellation_resistant_refresh() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_resistant_refresh(*_args) -> bool:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return True

    client = _client(
        add_comment=AsyncMock(side_effect=NetworkError("timeout")),
        invalidate_review_reads=AsyncMock(side_effect=cancellation_resistant_refresh),
    )
    service, *_ = _service(client)
    command = GeneralComment("unknown-refresh-1", REF, "body")

    outcome = await asyncio.wait_for(service.execute(command), timeout=0.05)
    repeated = await service.execute(command)

    assert entered.is_set()
    assert outcome == repeated
    assert outcome.status == MutationStatus.UNKNOWN
    client.add_comment.assert_awaited_once()
    release.set()
    await asyncio.sleep(0)
    await service.close()
    assert not service._refresh_tasks


@pytest.mark.asyncio
async def test_outer_cancellation_during_unknown_refresh_owns_cleanup() -> None:
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def blocked_refresh(*_args) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    client = _client(
        add_comment=AsyncMock(side_effect=NetworkError("timeout")),
        invalidate_review_reads=AsyncMock(side_effect=blocked_refresh),
    )
    service, *_ = _service(client)
    command = GeneralComment("unknown-cancel-refresh-1", REF, "body")
    task = asyncio.create_task(service.execute(command))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await exited.wait()

    repeated = await service.execute(command)
    assert repeated.status == MutationStatus.UNKNOWN
    client.add_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_comment_validates_real_line_and_passes_captured_revision() -> (
    None
):
    client = _client()
    service, get_review, *_ = _service(client)
    command = InlineComment(
        "inline-1",
        REF,
        REVISION,
        DiffAnchor("old.py", "new.py", 11, DiffSide.RIGHT),
        "Please rename",
    )

    outcome = await service.execute(command)

    assert outcome.status == MutationStatus.KNOWN
    assert get_review.await_count == 2
    kwargs = client.create_inline_comment.await_args.kwargs
    assert kwargs["head_sha"] == "head"
    assert kwargs["base_sha"] == "base"
    assert kwargs["start_sha"] == "start"
    assert kwargs["old_path"] == "old.py"
    assert kwargs["new_path"] == "new.py"


@pytest.mark.asyncio
async def test_header_padding_or_truncated_diff_cannot_be_manufactured_anchor() -> None:
    client = _client()
    service, *_ = _service(client)
    command = InlineComment(
        "inline-bad",
        REF,
        REVISION,
        DiffAnchor("old.py", "new.py", 99, DiffSide.RIGHT),
        "body",
    )

    with pytest.raises(ServiceError) as raised:
        await service.execute(command)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    client.create_inline_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_revision_rejects_before_dispatch() -> None:
    client = _client()
    service, get_review, *_ = _service(client)
    get_review.return_value = _snapshot(revision=ReviewRevision("new", "base", "start"))

    with pytest.raises(ServiceError) as raised:
        await service.execute(
            InlineComment(
                "stale-1",
                REF,
                REVISION,
                DiffAnchor("old.py", "new.py", 11, DiffSide.RIGHT),
                "body",
            )
        )
    assert raised.value.code == ServiceErrorCode.REVISION_CHANGED
    client.create_inline_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_uses_verified_top_level_root_comment_identity() -> None:
    client = _client()
    service, *_ = _service(client)

    await service.execute(Reply("reply-1", REF, REVISION, "thread-9", "reply"))

    assert client.reply_to_discussion.await_args.kwargs["root_comment_id"] == "root-7"


@pytest.mark.asyncio
async def test_unknown_discussion_is_rejected_before_dispatch() -> None:
    client = _client()
    service, *_rest = _service(client)
    _rest[2].return_value = ()

    with pytest.raises(ServiceError) as raised:
        await service.execute(Resolve("resolve-1", REF, REVISION, "foreign"))
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    client.resolve_discussion.assert_not_awaited()


@pytest.mark.asyncio
async def test_gitlab_request_changes_and_multistep_approval_are_explicitly_unsupported() -> (
    None
):
    client = _client()
    service, *_ = _service(client)

    for command in (
        ReviewVerdict(
            "verdict-1", REF, REVISION, ReviewDecision.CHANGES_REQUESTED, "fix"
        ),
        ReviewVerdict("verdict-2", REF, REVISION, ReviewDecision.APPROVED, "ship"),
    ):
        with pytest.raises(ServiceError) as raised:
            await service.execute(command)
        assert raised.value.code == ServiceErrorCode.UNSUPPORTED
    client.submit_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_ledger_never_evicts_an_unresolved_operation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await release.wait()
        return ForgeMutationResult("one")

    client = _client(add_comment=AsyncMock(side_effect=block))
    service, *_ = _service(client, ledger_size=1)
    first = asyncio.create_task(
        service.execute(GeneralComment("pending-1", REF, "one"))
    )
    await entered.wait()
    with pytest.raises(ServiceError, match="ledger is full"):
        await service.execute(GeneralComment("pending-2", REF, "two"))
    release.set()
    await first


@pytest.mark.asyncio
async def test_full_ledger_retains_terminal_binding_and_never_redispatches() -> None:
    client = _client()
    service, *_ = _service(client, ledger_size=1)
    first = GeneralComment("retained-1", REF, "one")
    expected = await service.execute(first)

    with pytest.raises(ServiceError, match="ledger is full"):
        await service.execute(GeneralComment("rejected-2", REF, "two"))
    repeated = await service.execute(first)

    assert repeated == expected
    client.add_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_dispatch_cancellation_releases_reservation_for_safe_retry() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    client = _client()
    service, get_review, *_ = _service(client, ledger_size=1)

    async def block_review(_review):
        entered.set()
        await release.wait()
        return _snapshot()

    get_review.side_effect = block_review
    command = InlineComment(
        "pre-cancel-1",
        REF,
        REVISION,
        DiffAnchor("old.py", "new.py", 11, DiffSide.RIGHT),
        "body",
    )
    task = asyncio.create_task(service.execute(command))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    get_review.side_effect = None
    get_review.return_value = _snapshot()
    outcome = await service.execute(command)

    assert outcome.status == MutationStatus.KNOWN
    client.create_inline_comment.assert_awaited_once()
