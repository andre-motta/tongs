"""Failure-oriented tests for durable submission recovery records."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import tongs
from tongs.services import RepositoryRef, ReviewRef, ReviewRevision
from tongs.services import review_submission as review_submission_module
from tongs.state.drafts import (
    DiffSide,
    DraftContent,
    DraftState,
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
from tongs.state.drafts import store as store_module
from tongs.state.drafts.reconciliation import (
    ConfirmedSubmissionContent,
    editable_remainder,
)

REVIEW = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 17)
REVISION = ReviewRevision("head-1", "base-1", "start-1")


def _content() -> DraftContent:
    anchor = InlineAnchor(
        REVISION,
        "old.py",
        "new.py",
        None,
        11,
        DiffSide.NEW,
        context_fingerprint(("context", "new")),
    )
    return DraftContent(
        "summary",
        DraftVerdict.APPROVE,
        (
            GeneralDraftComment(uuid4(), "general"),
            InlineDraftComment(uuid4(), "inline", anchor),
            ReplyDraftComment(uuid4(), "reply", "thread-9"),
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "drafts.db"


def _child_begin_dispatch(
    db_path: str,
    draft_id: str,
    version: int,
    ready: object,
    source: object,
) -> None:
    async def run() -> None:
        import tongs
        from tongs.services import review_submission
        from tongs.state.drafts import store as store_module

        store = DraftStore(Path(db_path))
        await store.open()
        attempt = await store.lock_submission(UUID(draft_id), version)
        await store.begin_dispatch(attempt.id, "comment:child", "draft:child:1")
        source.send(  # type: ignore[attr-defined]
            (tongs.__file__, store_module.__file__, review_submission.__file__)
        )
        ready.set()  # type: ignore[attr-defined]
        await asyncio.Event().wait()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_v1_database_migrates_without_losing_attempt_or_receipt(
    db_path: Path,
) -> None:
    first = DraftStore(db_path)
    await first.open()
    draft = await first.create_draft(REVIEW, REVISION, _content())
    attempt = await first.lock_submission(draft.id, draft.version)
    attempt = await first.record_receipt(attempt.id, "legacy-step", "note-1")
    await first.close()

    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE submission_plans")
        db.execute("DROP TABLE submission_receipt_resync")
        db.execute("DROP TABLE submission_pending_dispatches")
        db.execute("DROP TABLE submission_unknown_outcomes")
        db.execute("DROP TABLE submission_retry_authorizations")
        db.execute("PRAGMA user_version=1")

    second = DraftStore(db_path)
    await second.open()
    migrated = await second.get_attempt(attempt.id)

    assert migrated.snapshot == attempt.snapshot
    assert migrated.receipts[0].step_id == attempt.receipts[0].step_id
    assert migrated.receipts[0].remote_id == attempt.receipts[0].remote_id
    assert migrated.receipts[0].recorded_at == attempt.receipts[0].recorded_at
    assert migrated.receipts[0].resync_required is True
    assert migrated.retry_authorizations == ()
    assert migrated.unknown_outcomes == ()
    assert migrated.pending_dispatch is None
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
    await second.close()


@pytest.mark.asyncio
async def test_retry_authorization_is_explicit_ordered_and_reopens(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)

    await store.authorize_retry(attempt.id, "comment:one")
    second = await store.authorize_retry(attempt.id, "comment:one")
    await store.close()

    reopened = DraftStore(db_path)
    await reopened.open()
    recovered = await reopened.get_attempt(attempt.id)
    assert [item.ordinal for item in recovered.retry_authorizations] == [1, 2]
    assert [item.step_id for item in recovered.retry_authorizations] == [
        "comment:one",
        "comment:one",
    ]
    assert recovered.retry_authorizations == second.retry_authorizations
    await reopened.close()


@pytest.mark.asyncio
async def test_confirmed_step_cannot_receive_retry_authorization(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.record_receipt(attempt.id, "comment:one", "note-1")

    with pytest.raises(DraftStoreError, match="confirmed"):
        await store.authorize_retry(attempt.id, "comment:one")
    await store.close()


@pytest.mark.asyncio
async def test_pending_dispatch_becomes_exact_unknown_after_process_recovery(
    db_path: Path,
) -> None:
    first = DraftStore(db_path)
    await first.open()
    draft = await first.create_draft(REVIEW, REVISION, _content())
    attempt = await first.lock_submission(draft.id, draft.version)
    pending = await first.begin_dispatch(attempt.id, "comment:two", "draft:op:2")
    assert pending.pending_dispatch is not None
    await first.close()

    second = DraftStore(db_path)
    await second.open()
    recovered = await second.recover_incomplete_attempts()

    assert len(recovered) == 1
    assert recovered[0].state is DraftState.UNKNOWN
    assert recovered[0].pending_dispatch is None
    assert [(item.step_id, item.reason) for item in recovered[0].unknown_outcomes] == [
        ("comment:two", "process_interrupted")
    ]
    await second.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-lock proof")
async def test_child_death_after_dispatch_journal_preserves_exact_unknown(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION, _content())
    await setup.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    parent_source, child_source = context.Pipe(duplex=False)
    child = context.Process(
        target=_child_begin_dispatch,
        args=(str(db_path), str(draft.id), draft.version, ready, child_source),
    )
    child.start()
    try:
        assert await asyncio.to_thread(ready.wait, 10)
        sources = await asyncio.to_thread(parent_source.recv)
        expected_sources = (
            tongs.__file__,
            store_module.__file__,
            review_submission_module.__file__,
        )
        assert tuple(Path(path).resolve() for path in sources) == tuple(
            Path(path).resolve() for path in expected_sources
        )
        observer = DraftStore(db_path)
        await observer.open()
        assert await observer.recover_incomplete_attempts() == ()

        child.terminate()
        await asyncio.to_thread(child.join, 10)
        assert child.is_alive() is False
        recovered = await observer.recover_incomplete_attempts()
        assert [
            (item.step_id, item.reason) for item in recovered[0].unknown_outcomes
        ] == [("comment:child", "process_interrupted")]
        await observer.close()
    finally:
        if child.is_alive():
            child.terminate()
            await asyncio.to_thread(child.join, 10)


@pytest.mark.asyncio
async def test_receipt_atomically_clears_matching_pending_dispatch(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.begin_dispatch(attempt.id, "comment:one", "draft:op:1")

    recorded = await store.record_receipt(
        attempt.id, "comment:one", "note-1", operation_id="draft:op:1"
    )

    assert recorded.pending_dispatch is None
    assert [(receipt.step_id, receipt.remote_id) for receipt in recorded.receipts] == [
        ("comment:one", "note-1")
    ]
    await store.close()


@pytest.mark.asyncio
async def test_known_rejection_clears_dispatch_but_keeps_attempt_active(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.begin_dispatch(attempt.id, "comment:one", "draft:op:1")

    rejected = await store.reject_dispatch(attempt.id, "comment:one", "draft:op:1")

    assert rejected.state is DraftState.SUBMITTING
    assert rejected.pending_dispatch is None
    await store.close()


@pytest.mark.asyncio
async def test_return_editable_atomically_keeps_only_unconfirmed_content(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    content = _content()
    draft = await store.create_draft(REVIEW, REVISION, content)
    attempt = await store.lock_submission(draft.id, draft.version)
    first_id = content.comments[0].id
    attempt = await store.record_receipt(
        attempt.id, f"comment:{first_id.hex}", "note-1"
    )
    attempt = await store.mark_attempt_unknown(
        attempt.id,
        step_id=f"comment:{content.comments[1].id.hex}",
        reason="timeout",
    )
    remainder = editable_remainder(
        attempt, ConfirmedSubmissionContent(frozenset({first_id}))
    )

    historical = await store.reconcile_attempt(
        attempt.id,
        ReconciliationResolution.RETURN_EDITABLE,
        editable_content=remainder,
    )
    editable = await store.get_draft(draft.id)

    assert historical.state is DraftState.UNKNOWN
    assert historical.snapshot.content == content
    assert historical.receipts == attempt.receipts
    assert (
        historical.reconciliations[-1].resolution
        is ReconciliationResolution.RETURN_EDITABLE
    )
    assert editable.state is DraftState.EDITABLE
    assert editable.body == content.body
    assert editable.verdict is content.verdict
    assert editable.comments == content.comments[1:]
    await store.close()


@pytest.mark.asyncio
async def test_return_editable_requires_explicit_remainder_after_receipt(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)
    attempt = await store.record_receipt(attempt.id, "comment:one", "note-1")
    attempt = await store.mark_attempt_unknown(attempt.id)

    with pytest.raises(DraftStoreError, match="explicit remaining"):
        await store.reconcile_attempt(
            attempt.id, ReconciliationResolution.RETURN_EDITABLE
        )
    await store.close()


@pytest.mark.asyncio
async def test_cancel_submission_removes_only_never_dispatched_attempt(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)

    restored = await store.cancel_submission(attempt.id)

    assert restored.state is DraftState.EDITABLE
    assert restored.content == draft.content
    with pytest.raises(DraftStoreError):
        await store.get_attempt(attempt.id)
    await store.close()


@pytest.mark.asyncio
async def test_cancel_submission_rejects_a_journaled_remote_step(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, _content())
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.begin_dispatch(attempt.id, "comment:one", "draft:op:1")

    with pytest.raises(DraftStoreError, match="never-dispatched"):
        await store.cancel_submission(attempt.id)

    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTING
    await store.close()
