"""Production adapter coverage for review, draft, and submission operations."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.desktop.protocol import review_operations as review_operations_module
from tongs.desktop.protocol.messages import JsonObject, ProtocolError, ProtocolErrorCode
from tongs.desktop.protocol.review_operations import (
    REVIEW_CAPABILITY,
    REVIEW_METHODS,
    ReviewOperations,
)
from tongs.desktop.protocol.server import DesktopSidecarServer, RequestContext
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.errors import NetworkError
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.models import (
    CIStatus,
    Discussion,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    MRDetail,
    MRState,
    SourceCleanupStatus,
    User,
)
from tongs.forges.models import InlineComment as ForgeInlineComment
from tongs.plugins.desktop import DesktopCancellation
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ApplicationSession,
    ForgeCapabilities,
    MRAction,
    MRActionOutcome,
    MRActionReceipt,
    MRActionService,
    RawDiffSnapshot,
    RepositoryRef,
    ReviewActionTarget,
    ReviewMutationService,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ReviewSubmissionService,
    ServiceError,
    ServiceErrorCode,
)
from tongs.state.drafts import (
    DraftContent,
    DraftStore,
    GeneralDraftComment,
    ReconciliationResolution,
    context_fingerprint,
)

REPOSITORY = RepositoryRef("github.com", "acme/widgets")
REVIEW = ReviewRef(REPOSITORY, 42)
OTHER_REVIEW = ReviewRef(RepositoryRef("github.com", "acme/other"), REVIEW.number)
CROSS_FORGE_REVIEW = ReviewRef(
    RepositoryRef("gitlab.example.com", "acme/widgets"), REVIEW.number
)
REVISION = ReviewRevision("head-42", "base-42", None)
CHANGED_REVISION = ReviewRevision("head-43", "base-42", None)
PATCH = "@@ -10,2 +10,2 @@\n context\n-old\n+new"


def _snapshot(
    review: ReviewRef = REVIEW,
    *,
    revision: ReviewRevision = REVISION,
    state: MRState = MRState.OPEN,
) -> ReviewSnapshot:
    forge = (
        ForgeType.GITLAB
        if review.repository.hostname.startswith("gitlab")
        else ForgeType.GITHUB
    )
    detail = MRDetail(
        forge_host=ForgeHost(review.repository.hostname, forge, ""),
        repo_path=review.repository.project_path,
        local_path="",
        number=review.number,
        title="Review",
        author=User("alice"),
        state=state,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        web_url="https://example.invalid/review/42",
        head_sha=revision.head_sha,
        base_sha=revision.base_sha,
        start_sha=revision.start_sha,
    )
    return ReviewSnapshot(
        review,
        detail,
        revision,
        ForgeCapabilities(True, True, True, True, True),
    )


def _discussion() -> Discussion:
    root = ForgeInlineComment(
        "root-7",
        User("alice"),
        "root",
        datetime.now(UTC),
        "new.py",
        old_line=10,
        new_line=11,
    )
    return Discussion("thread-9", True, root, resolvable=True)


def _client(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "add_comment": AsyncMock(
            return_value=ForgeMutationResult("general-remote", "general-note")
        ),
        "create_inline_comment": AsyncMock(
            return_value=ForgeMutationResult("inline-remote", "inline-note", "thread-9")
        ),
        "reply_to_discussion": AsyncMock(
            return_value=ForgeMutationResult("reply-remote", "reply-note", "thread-9")
        ),
        "resolve_discussion": AsyncMock(
            return_value=ForgeMutationResult("resolve-remote", discussion_id="thread-9")
        ),
        "submit_review": AsyncMock(
            return_value=ForgeMutationResult("review-remote", "review-note")
        ),
        "merge_mr": AsyncMock(
            return_value=ForgeMergeResult(
                "merge-remote", "merge-sha", SourceCleanupStatus.CONFIRMED
            )
        ),
        "close_mr": AsyncMock(return_value=ForgeMutationResult("close-remote")),
        "reopen_mr": AsyncMock(return_value=ForgeMutationResult("reopen-remote")),
        "unapprove_mr": AsyncMock(return_value=ForgeMutationResult("unapprove-remote")),
        "invalidate_review_reads": AsyncMock(return_value=True),
        "supports_unapprove": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Session:
    def __init__(self, store: DraftStore, client: SimpleNamespace) -> None:
        self.config = Config()
        self.drafts = store
        self.client = client
        self.snapshots = {
            REVIEW: _snapshot(),
            OTHER_REVIEW: _snapshot(OTHER_REVIEW),
            CROSS_FORGE_REVIEW: _snapshot(
                CROSS_FORGE_REVIEW,
                revision=ReviewRevision("head-42", "base-42", "start-42"),
            ),
        }
        self.review_mutations = ReviewMutationService(
            get_client=self._get_client,
            get_review=self.get_review,
            get_diff=self._get_diff,
            get_discussions=self._get_discussions,
            emit_change=Mock(),
            timeout=0.1,
        )
        self.mr_actions = MRActionService(
            get_client=self._get_client,
            get_review=self.get_review,
            emit_change=Mock(),
        )
        self.review_submissions = ReviewSubmissionService(
            store=store,
            mutations=self.review_mutations,
            get_review=self.get_review,
        )

    async def start(self) -> _Session:
        return self

    async def events(self):
        if False:
            yield None

    async def get_review(self, ref: ReviewRef) -> ReviewSnapshot:
        try:
            return self.snapshots[ref]
        except KeyError:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The review was not issued by this session.",
            ) from None

    async def _get_client(self, review: ReviewRef, _operation: str) -> ForgeClient:
        await self.get_review(review)
        return cast(ForgeClient, self.client)

    async def _get_diff(self, review: ReviewRef) -> RawDiffSnapshot:
        snapshot = await self.get_review(review)
        assert snapshot.revision is not None
        return RawDiffSnapshot(
            review,
            snapshot.revision,
            (
                {
                    "filename": "new.py",
                    "previous_filename": "old.py",
                    "status": "modified",
                    "patch": PATCH,
                },
            ),
        )

    async def _get_discussions(self, review: ReviewRef) -> tuple[Discussion, ...]:
        await self.get_review(review)
        return (_discussion(),)

    async def close(self) -> None:
        await asyncio.wait_for(self.review_submissions.close(), 2)
        await asyncio.wait_for(self.mr_actions.close(), 2)
        await asyncio.wait_for(self.review_mutations.close(), 2)
        await asyncio.wait_for(self.drafts.close(), 2)


@pytest_asyncio.fixture
async def setup(tmp_path: Path):
    store = DraftStore(tmp_path / "data" / "drafts.db")
    await store.open()
    session = _Session(store, _client())
    handles = HandleRegistry()
    review = handles.issue(HandleKind.REVIEW, REVIEW)
    other = handles.issue(HandleKind.REVIEW, OTHER_REVIEW)
    cross_forge = handles.issue(HandleKind.REVIEW, CROSS_FORGE_REVIEW)
    repository = handles.issue(HandleKind.REPOSITORY, REPOSITORY)
    operations = ReviewOperations(session=session, handles=handles)
    try:
        yield (
            operations,
            session,
            {
                "review": review,
                "other": other,
                "cross_forge": cross_forge,
                "repository": repository,
            },
        )
    finally:
        await session.close()


def _context(request_id: str = "request") -> RequestContext:
    return RequestContext(request_id, DesktopCancellation())


def _revision_wire(revision: ReviewRevision = REVISION) -> JsonObject:
    return {
        "head_sha": revision.head_sha,
        "base_sha": revision.base_sha,
        "start_sha": revision.start_sha,
    }


def _mutation_anchor() -> JsonObject:
    return {
        "old_path": "old.py",
        "new_path": "new.py",
        "line": 11,
        "side": "RIGHT",
    }


def _draft_anchor(
    revision: ReviewRevision = REVISION, *, stale: bool = False
) -> JsonObject:
    return {
        "revision": _revision_wire(revision),
        "old_path": "old.py",
        "new_path": "new.py",
        "old_line": 10,
        "new_line": 11,
        "side": "new",
        "context_fingerprint": context_fingerprint(("context", "new")),
        "start_line": None,
        "start_side": None,
        "stale": stale,
    }


def _draft_content(comment_id: UUID | None = None) -> JsonObject:
    return {
        "body": "summary",
        "verdict": "request_changes",
        "comments": [
            {
                "id": str(comment_id or uuid4()),
                "kind": "inline",
                "body": "inline body",
                "anchor": _draft_anchor(),
            }
        ],
    }


@pytest.mark.asyncio
async def test_registry_is_exact_and_classifies_every_write(setup) -> None:
    operations, _session, _handles = setup

    assert REVIEW_CAPABILITY == "review_mutations"
    assert tuple(sorted(operations.handlers)) == REVIEW_METHODS
    reads = {
        "drafts.get",
        "drafts.list",
        "review_actions.capabilities",
        "review_actions.receipt",
        "review_mutations.capabilities",
        "review_submissions.list",
        "review_submissions.status",
    }
    assert {
        method
        for method, (_handler, mutation) in operations.handlers.items()
        if not mutation
    } == reads


@pytest.mark.asyncio
async def test_capabilities_use_only_admitted_handle(setup) -> None:
    operations, _session, handles = setup

    mutation = await operations.mutation_capabilities(
        {"review": handles["review"]}, _context()
    )
    actions = await operations.action_capabilities(
        {"review": handles["review"]}, _context()
    )

    assert mutation["review"] == handles["review"]
    assert mutation["capabilities"]["atomic_review_batch"] is True
    assert actions["capabilities"] == {
        "merge": True,
        "close": True,
        "reopen": False,
        "unapprove": True,
    }
    assert "github.com" not in json.dumps((mutation, actions))


@pytest.mark.asyncio
async def test_quick_comment_repeat_coalesces_and_conflict_does_not_replay(
    setup,
) -> None:
    operations, session, handles = setup
    params: JsonObject = {
        "operation_id": "quick:42:1",
        "review": handles["review"],
        "body": "Looks good",
    }

    first = await operations.comment(params, _context("transport-one"))
    repeated = await operations.comment(params, _context("transport-two"))
    with pytest.raises(ServiceError) as conflict:
        await operations.comment(
            {**params, "body": "different"}, _context("transport-three")
        )

    assert first == repeated
    assert first["operation_id"] == "quick:42:1"
    assert first["outcome"] == "known"
    assert conflict.value.code is ServiceErrorCode.CONFLICT
    session.client.add_comment.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "client_method"),
    [
        (
            "inline_comment",
            {
                "operation_id": "inline:1",
                "revision": _revision_wire(),
                "anchor": _mutation_anchor(),
                "body": "inline",
            },
            "create_inline_comment",
        ),
        (
            "reply",
            {
                "operation_id": "reply:1",
                "revision": _revision_wire(),
                "discussion_id": "thread-9",
                "body": "reply",
            },
            "reply_to_discussion",
        ),
        (
            "resolve",
            {
                "operation_id": "resolve:1",
                "revision": _revision_wire(),
                "discussion_id": "thread-9",
                "resolved": True,
            },
            "resolve_discussion",
        ),
        (
            "verdict",
            {
                "operation_id": "verdict:1",
                "revision": _revision_wire(),
                "verdict": "approved",
                "body": "",
                "inline_comments": [],
            },
            "submit_review",
        ),
    ],
)
async def test_revision_bound_quick_operations_dispatch_typed_commands(
    setup, method: str, params: JsonObject, client_method: str
) -> None:
    operations, session, handles = setup
    params["review"] = handles["review"]

    result = await getattr(operations, method)(params, _context())

    assert result["outcome"] == "known"
    getattr(session.client, client_method).assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_revision_and_hostile_payloads_fail_before_write(setup) -> None:
    operations, session, handles = setup
    payload: JsonObject = {
        "operation_id": "stale:1",
        "review": handles["review"],
        "revision": _revision_wire(CHANGED_REVISION),
        "anchor": _mutation_anchor(),
        "body": "body",
    }

    with pytest.raises(ServiceError) as stale:
        await operations.inline_comment(payload, _context())
    with pytest.raises(ProtocolError) as wrong_kind:
        await operations.comment(
            {
                "operation_id": "wrong-kind",
                "review": handles["repository"],
                "body": "body",
            },
            _context(),
        )
    with pytest.raises(ProtocolError):
        await operations.comment(
            {
                "operation_id": True,
                "review": handles["review"],
                "body": "body",
            },
            _context(),
        )
    with pytest.raises(ProtocolError):
        await operations.comment(
            {
                "operation_id": "oversized",
                "review": handles["review"],
                "body": "é" * 32_769,
            },
            _context(),
        )
    with pytest.raises(ProtocolError):
        await operations.comment(
            {
                "operation_id": "extra",
                "review": handles["review"],
                "body": "body",
                "hostname": "attacker.invalid",
            },
            _context(),
        )

    assert stale.value.code is ServiceErrorCode.REVISION_CHANGED
    assert wrong_kind.value.code is ProtocolErrorCode.WRONG_HANDLE_KIND
    session.client.create_inline_comment.assert_not_awaited()
    session.client.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_session_handle_and_predispatch_cancel_never_write(setup) -> None:
    operations, session, handles = setup
    foreign = HandleRegistry().issue(HandleKind.REVIEW, REVIEW)
    context = _context()
    context.cancellation.cancel()

    with pytest.raises(ProtocolError) as cross_session:
        await operations.comment(
            {"operation_id": "cross", "review": foreign, "body": "body"},
            _context(),
        )
    with pytest.raises(ProtocolError) as cancelled:
        await operations.comment(
            {
                "operation_id": "cancelled",
                "review": handles["review"],
                "body": "body",
            },
            context,
        )

    assert cross_session.value.code is ProtocolErrorCode.INVALID_HANDLE
    assert cancelled.value.details == {"outcome": "not_dispatched"}
    session.client.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_quick_write_is_safe_retained_and_not_replayed(setup) -> None:
    operations, session, handles = setup
    session.client.add_comment.side_effect = NetworkError("private upstream response")
    params: JsonObject = {
        "operation_id": "unknown:1",
        "review": handles["review"],
        "body": "body",
    }

    first = await operations.comment(params, _context())
    repeated = await operations.comment(params, _context())

    assert first == repeated
    assert first["outcome"] == "unknown"
    assert first["resync_required"] is True
    assert "private" not in json.dumps(first)
    assert session.client.add_comment.await_count == 1


@pytest.mark.asyncio
async def test_cancel_after_quick_dispatch_returns_retained_unknown(setup) -> None:
    operations, session, handles = setup
    entered = asyncio.Event()

    async def hang(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    session.client.add_comment.side_effect = hang
    context = _context()
    task = asyncio.create_task(
        operations.comment(
            {
                "operation_id": "cancel-after",
                "review": handles["review"],
                "body": "body",
            },
            context,
        )
    )
    await entered.wait()

    context.cancellation.cancel()
    result = await task

    assert result["outcome"] == "unknown"
    repeated = await operations.comment(
        {
            "operation_id": "cancel-after",
            "review": handles["review"],
            "body": "body",
        },
        _context(),
    )
    assert repeated == result
    assert session.client.add_comment.await_count == 1


@pytest.mark.asyncio
async def test_merge_retains_cleanup_evidence_and_receipt_authority(setup) -> None:
    operations, session, handles = setup
    params: JsonObject = {
        "operation_id": "merge:42",
        "review": handles["review"],
        "revision": _revision_wire(),
        "squash": True,
        "source_cleanup": {"branch": "feature"},
    }

    result = await operations.merge_review(params, _context())
    receipt = await operations.action_receipt(
        {
            "operation_id": "merge:42",
            "review": handles["review"],
            "revision": _revision_wire(),
            "action": "merge",
        },
        _context(),
    )
    with pytest.raises(ServiceError) as cross_project:
        await operations.action_receipt(
            {
                "operation_id": "merge:42",
                "review": handles["other"],
                "revision": _revision_wire(),
                "action": "merge",
            },
            _context(),
        )

    assert result["merge_sha"] == "merge-sha"
    assert result["source_cleanup"] == "confirmed"
    assert receipt["receipt"] == result
    assert cross_project.value.code is ServiceErrorCode.CONFLICT
    assert "github.com" not in json.dumps(result)
    session.client.merge_mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_unknown_keeps_merge_and_cleanup_unclaimed(setup) -> None:
    operations, session, handles = setup
    session.client.merge_mr.side_effect = NetworkError("private")

    result = await operations.merge_review(
        {
            "operation_id": "merge:unknown",
            "review": handles["review"],
            "revision": _revision_wire(),
        },
        _context(),
    )

    assert result["outcome"] == "unknown"
    assert result["merge_sha"] is None
    assert result["source_cleanup"] == "not_requested"
    assert result["resync_required"] is True
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancel_fallback_rejects_receipt_for_different_action(setup) -> None:
    operations, session, handles = setup
    entered = asyncio.Event()

    class _ConflictingActions:
        async def execute(self, _command):
            entered.set()
            await asyncio.Event().wait()

        async def receipt(self, operation_id: str) -> MRActionReceipt:
            return MRActionReceipt(
                operation_id,
                MRAction.MERGE,
                ReviewActionTarget(REVIEW, REVISION, MRState.OPEN),
                MRActionOutcome.KNOWN,
                remote_id="different-action",
                merge_sha="different-sha",
            )

        async def close(self) -> None:
            return None

    session.mr_actions = _ConflictingActions()
    context = _context()
    running = asyncio.create_task(
        operations.close_review(
            {
                "operation_id": "contended-id",
                "review": handles["review"],
                "revision": _revision_wire(),
            },
            context,
        )
    )
    await entered.wait()

    context.cancellation.cancel()
    with pytest.raises(ServiceError) as conflict:
        await running

    assert conflict.value.code is ServiceErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_draft_roundtrip_conflict_stale_anchor_and_identity(setup) -> None:
    operations, session, handles = setup
    comment_id = uuid4()
    created = await operations.create_draft(
        {
            "review": handles["review"],
            "revision": _revision_wire(),
            "content": _draft_content(comment_id),
        },
        _context(),
    )
    session.snapshots[REVIEW] = _snapshot(revision=CHANGED_REVISION)
    loaded = await operations.get_draft(
        {"review": handles["review"], "draft_id": created["id"]}, _context()
    )

    assert loaded["comments"][0]["anchor"]["stale"] is True
    content = cast(JsonObject, created.copy())
    content = {
        "body": "updated",
        "verdict": created["verdict"],
        "comments": loaded["comments"],
    }
    saved = await operations.save_draft(
        {
            "review": handles["review"],
            "draft_id": created["id"],
            "expected_version": created["version"],
            "content": content,
        },
        _context(),
    )
    assert saved["version"] == 2
    assert saved["comments"][0]["anchor"]["revision"] == _revision_wire()
    assert saved["comments"][0]["anchor"]["stale"] is True

    with pytest.raises(ServiceError) as conflict:
        await operations.save_draft(
            {
                "review": handles["review"],
                "draft_id": created["id"],
                "expected_version": 1,
                "content": content,
            },
            _context(),
        )
    assert conflict.value.code is ServiceErrorCode.CONFLICT
    assert dict(conflict.value.details)["current_version"] == "2"

    changed = cast(JsonObject, json.loads(json.dumps(content)))
    comments = cast(list[JsonObject], changed["comments"])
    anchor = cast(JsonObject, comments[0]["anchor"])
    anchor["new_line"] = 12
    with pytest.raises(ProtocolError, match="cannot change"):
        await operations.save_draft(
            {
                "review": handles["review"],
                "draft_id": created["id"],
                "expected_version": 2,
                "content": changed,
            },
            _context(),
        )


@pytest.mark.asyncio
async def test_draft_and_attempt_ids_never_authorize_another_review(setup) -> None:
    operations, session, handles = setup
    draft = await session.drafts.create_draft(
        OTHER_REVIEW, REVISION, DraftContent(body="other")
    )
    cross_forge = await session.drafts.create_draft(
        CROSS_FORGE_REVIEW,
        ReviewRevision("head-42", "base-42", "start-42"),
        DraftContent(body="cross forge"),
    )

    for draft_id in (draft.id, cross_forge.id):
        with pytest.raises(ServiceError) as raised:
            await operations.get_draft(
                {"review": handles["review"], "draft_id": str(draft_id)},
                _context(),
            )
        assert raised.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED

    attempt = await session.drafts.lock_submission(draft.id, draft.version)
    await session.drafts.mark_attempt_unknown(
        attempt.id, step_id="step", reason="interrupted"
    )
    with pytest.raises(ServiceError) as attempt_error:
        await operations.submission_status(
            {"review": handles["review"], "attempt_id": str(attempt.id)},
            _context(),
        )
    assert attempt_error.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED


@pytest.mark.asyncio
async def test_draft_listing_is_bounded_and_review_scoped(setup) -> None:
    operations, session, handles = setup
    for index in range(3):
        await session.drafts.create_draft(
            REVIEW, REVISION, DraftContent(body=f"draft {index}")
        )
    await session.drafts.create_draft(
        OTHER_REVIEW, REVISION, DraftContent(body="other")
    )

    first = await operations.list_drafts(
        {"review": handles["review"], "max_items": 2}, _context()
    )
    second = await operations.list_drafts(
        {
            "review": handles["review"],
            "cursor": first["next_cursor"],
            "max_items": 2,
        },
        _context(),
    )

    assert len(first["drafts"]) == 2
    assert len(second["drafts"]) == 1
    assert all(item["review"] == handles["review"] for item in first["drafts"])
    assert "acme/other" not in json.dumps((first, second))


@pytest.mark.asyncio
async def test_submission_status_and_reconciliation_are_durable_and_scoped(
    setup,
) -> None:
    operations, session, handles = setup
    draft = await session.drafts.create_draft(
        REVIEW,
        REVISION,
        DraftContent(comments=(GeneralDraftComment(uuid4(), "general"),)),
    )
    session.client.add_comment.side_effect = NetworkError("private upstream")

    started = await operations.start_submission(
        {
            "review": handles["review"],
            "draft_id": str(draft.id),
            "expected_version": draft.version,
        },
        _context(),
    )
    status = await operations.submission_status(
        {
            "review": handles["review"],
            "attempt_id": started["attempt_id"],
        },
        _context(),
    )
    listed = await operations.list_submissions(
        {"review": handles["review"], "max_items": 10}, _context()
    )
    with pytest.raises(ServiceError) as cross_review:
        await operations.reconcile_submission(
            {
                "review": handles["other"],
                "attempt_id": started["attempt_id"],
                "resolution": "return_editable",
            },
            _context(),
        )
    reconciled = await operations.reconcile_submission(
        {
            "review": handles["review"],
            "attempt_id": started["attempt_id"],
            "resolution": ReconciliationResolution.RETURN_EDITABLE.value,
        },
        _context(),
    )

    assert started["outcome"] == status["outcome"] == "unknown"
    assert listed["attempts"] == [status]
    assert cross_review.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED
    assert reconciled["outcome"] == "editable"
    assert "private" not in json.dumps((started, status, listed, reconciled))
    assert session.client.add_comment.await_count == 1


@pytest.mark.asyncio
async def test_reconnect_rediscovers_unknown_submission_without_attempt_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "drafts.db"
    first_store = DraftStore(path)
    await first_store.open()
    first_session = _Session(first_store, _client())
    draft = await first_store.create_draft(
        REVIEW,
        REVISION,
        DraftContent(comments=(GeneralDraftComment(uuid4(), "pending"),)),
    )
    attempt = await first_store.lock_submission(draft.id, draft.version)
    await first_store.mark_attempt_unknown(
        attempt.id, step_id="comment:0", reason="connection_lost"
    )
    await first_session.close()

    second_store = DraftStore(path)
    await second_store.open()
    second_session = _Session(second_store, _client())
    handles = HandleRegistry()
    review_handle = handles.issue(HandleKind.REVIEW, REVIEW)
    operations = ReviewOperations(session=second_session, handles=handles)
    try:
        recovered = await operations.list_submissions(
            {"review": review_handle, "max_items": 1}, _context()
        )
        recovered_attempt = recovered["attempts"][0]
        status = await operations.submission_status(
            {
                "review": review_handle,
                "attempt_id": recovered_attempt["attempt_id"],
            },
            _context(),
        )
    finally:
        await second_session.close()

    assert recovered["next_cursor"] is None
    assert recovered_attempt["attempt_id"] == str(attempt.id)
    assert recovered_attempt["outcome"] == "unknown"
    assert status == recovered_attempt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"body": "x", "verdict": None, "comments": True},
        {"body": "x", "verdict": "ship_it", "comments": []},
        {"body": "x", "comments": [{"id": True, "kind": "general", "body": "x"}]},
        {
            "body": "x",
            "comments": [
                {
                    "id": "00000000-0000-0000-0000-000000000000",
                    "kind": "inline",
                    "body": "x",
                    "anchor": {**_draft_anchor(), "new_line": 1.0},
                }
            ],
        },
    ],
)
async def test_malformed_nested_drafts_fail_before_storage(
    setup, payload: JsonObject
) -> None:
    operations, session, handles = setup

    with pytest.raises(ProtocolError):
        await operations.create_draft(
            {
                "review": handles["review"],
                "revision": _revision_wire(),
                "content": payload,
            },
            _context(),
        )

    assert await session.drafts.list_drafts(review=REVIEW) == ()


class _WireWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def response(self, request_id: str) -> dict[str, object]:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), 2)
            if frame.get("type") == "response" and frame.get("id") == request_id:
                return frame


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    return (
        json.dumps(
            {
                "v": 1,
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        + b"\n"
    )


def _github_pr() -> dict[str, object]:
    return {
        "id": 420,
        "number": REVIEW.number,
        "title": "Review",
        "body": "description",
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": REVISION.head_sha},
        "base": {"ref": "main", "sha": REVISION.base_sha},
        "created_at": "2026-09-07T12:00:00Z",
        "updated_at": "2026-09-07T12:00:00Z",
        "html_url": "https://github.com/acme/widgets/pull/42",
        "comments": 0,
        "review_comments": 0,
        "mergeable_state": "clean",
        "labels": [],
        "requested_reviewers": [],
        "assignees": [],
        "changed_files": 1,
    }


@pytest.mark.asyncio
async def test_eof_settles_dispatched_quick_write_unknown_without_replay(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def hang(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    client = _client(add_comment=AsyncMock(side_effect=hang))
    store = DraftStore(tmp_path / "data" / "drafts.db")
    await store.open()
    session = _Session(store, client)
    server = DesktopSidecarServer(
        session=cast(ApplicationSession, session),
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
        shutdown_timeout=1,
    )
    reader = asyncio.StreamReader()
    writer = _WireWriter()
    running = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [REVIEW_CAPABILITY],
            },
        )
    )
    await writer.response("handshake")
    review_handle = server._handles.issue(HandleKind.REVIEW, REVIEW)
    reader.feed_data(
        _frame(
            "eof-comment",
            "review_mutations.comment",
            {
                "operation_id": "eof:quick:1",
                "review": review_handle,
                "body": "may reach the forge",
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 2)

    reader.feed_eof()
    await asyncio.wait_for(running, 3)

    record = session.review_mutations._ledger["eof:quick:1"]
    assert record.outcome is not None
    assert record.outcome.status.value == "unknown"
    assert record.outcome.resync_required is True
    assert client.add_comment.await_count == 1


class _TransportRegistry:
    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [REPOSITORY.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        if hostname == REPOSITORY.hostname:
            return ForgeHost(hostname, ForgeType.GITHUB, "https://api.github.com")
        return None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return self.client

    async def close_all(self) -> None:
        await self.client.close()


@pytest.mark.asyncio
async def test_production_ndjson_roundtrip_uses_real_session_and_mock_transport(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/repos/acme/widgets/pulls":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=[_github_pr()] if page == 1 else [])
        if request.url.path == "/repos/acme/widgets/pulls/42":
            return httpx.Response(200, json=_github_pr())
        if request.url.path == "/repos/acme/widgets/pulls/42/reviews":
            return httpx.Response(200, json=[])
        if request.url.path == "/repos/acme/widgets/commits/head-42/check-runs":
            return httpx.Response(200, json={"check_runs": []})
        if request.url.path == "/repos/acme/widgets/issues/42/comments":
            assert request.method == "POST"
            assert json.loads(request.content) == {"body": "NDJSON comment"}
            return httpx.Response(201, json={"id": 991})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    client = GitHubClient(
        ForgeHost(REPOSITORY.hostname, ForgeType.GITHUB, "https://api.github.com"),
        http,
    )
    cache_path = tmp_path / "cache" / "cache.db"
    draft_path = tmp_path / "data" / "drafts.db"
    session = ApplicationSession(
        config=Config(),
        cache=CacheStore(db_path=cache_path),
        draft_store=DraftStore(draft_path),
        forge_registry=cast(object, _TransportRegistry(client)),  # type: ignore[arg-type]
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    reader = asyncio.StreamReader()
    writer = _WireWriter()
    running = asyncio.create_task(server.run(reader, writer))
    try:
        reader.feed_data(
            _frame(
                "handshake",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": version("tongs"),
                    "capabilities": [REVIEW_CAPABILITY],
                },
            )
        )
        handshake = await writer.response("handshake")
        result = cast(dict[str, object], handshake["result"])
        assert result["accepted_capabilities"] == [REVIEW_CAPABILITY]
        assert result["methods"] == [*sorted(server._operations), "shutdown"]
        for method, (_handler, mutation) in ReviewOperations(
            session=session, handles=server._handles
        ).handlers.items():
            assert server._operations[method].mutation is mutation

        reader.feed_data(
            _frame(
                "repository",
                "repositories.open",
                {
                    "hostname": REPOSITORY.hostname,
                    "project_path": REPOSITORY.project_path,
                },
            )
        )
        repository = await writer.response("repository")
        repository_handle = repository["result"]["handle"]  # type: ignore[index]
        reader.feed_data(
            _frame(
                "reviews",
                "reviews.list",
                {
                    "scope": "all_open",
                    "repository": repository_handle,
                },
            )
        )
        reviews = await writer.response("reviews")
        review_handle = reviews["result"]["items"][0]["handle"]  # type: ignore[index]

        reader.feed_data(
            _frame(
                "transport-comment",
                "review_mutations.comment",
                {
                    "operation_id": "application-comment",
                    "review": review_handle,
                    "body": "NDJSON comment",
                },
            )
        )
        comment = await writer.response("transport-comment")
        assert comment["result"]["operation_id"] == "application-comment"  # type: ignore[index]
        assert comment["result"]["outcome"] == "known"  # type: ignore[index]

        reader.feed_data(
            _frame(
                "draft",
                "drafts.create",
                {
                    "review": review_handle,
                    "revision": _revision_wire(),
                },
            )
        )
        draft = await writer.response("draft")
        assert draft["result"]["review"] == review_handle  # type: ignore[index]
        assert draft["result"]["state"] == "editable"  # type: ignore[index]

        reader.feed_data(_frame("shutdown", "shutdown", {}))
        assert (await writer.response("shutdown"))["result"] == {"accepted": True}
        await asyncio.wait_for(running, 2)
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 2)

    assert requests.count(("POST", "/repos/acme/widgets/issues/42/comments")) == 1
    assert cache_path.is_file()
    assert draft_path.is_file()
    assert oct(cache_path.stat().st_mode & 0o777) == "0o600"
    assert oct(draft_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_exact_e_p_child_binds_candidate_and_registered_review_methods(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).parents[3]
        / "src"
        / "tongs"
        / "desktop"
        / "protocol"
        / "review_operations.py"
    ).resolve()
    assert Path(review_operations_module.__file__).resolve() == source

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path / "must-not-bind")
    environment["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    environment["XDG_DATA_HOME"] = str(tmp_path / "data")
    binding = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-P",
        "-c",
        (
            "from pathlib import Path;"
            "import tongs.desktop.protocol.review_operations as module;"
            "print(Path(module.__file__).resolve())"
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/tmp",
        env=environment,
    )
    stdout, stderr = await asyncio.wait_for(binding.communicate(), 10)
    assert binding.returncode == 0, stderr.decode()
    assert Path(stdout.decode().strip()) == source

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-P",
        "-m",
        "tongs.desktop.sidecar",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/tmp",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdin.write(
            _frame(
                "unsupported",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": version("tongs"),
                    "capabilities": ["review_mutations.v2"],
                },
            )
        )
        await process.stdin.drain()
        unsupported = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert unsupported["error"]["code"] == "unsupported_protocol"

        process.stdin.write(
            _frame(
                "handshake",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": version("tongs"),
                    "capabilities": [REVIEW_CAPABILITY],
                },
            )
        )
        await process.stdin.drain()
        handshake = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert REVIEW_CAPABILITY in handshake["result"]["accepted_capabilities"]
        assert set(REVIEW_METHODS) <= set(handshake["result"]["methods"])

        process.stdin.write(_frame("shutdown", "shutdown", {}))
        await process.stdin.drain()
        shutdown = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert shutdown["result"] == {"accepted": True}
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
