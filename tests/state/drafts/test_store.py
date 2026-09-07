"""Failure-oriented tests for durable review draft storage."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
import stat
import sys
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite
import pytest
from platformdirs import user_cache_dir

from tongs.services import RepositoryRef, ReviewRef, ReviewRevision
from tongs.state.drafts import (
    DiffSide,
    DraftAttemptOwnedError,
    DraftConflictError,
    DraftContent,
    DraftCorruptionError,
    DraftNotFoundError,
    DraftNotOpenError,
    DraftPermissionError,
    DraftReceiptConflictError,
    DraftSchemaError,
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
    default_draft_db_path,
)
from tongs.state.drafts import store as store_module
from tongs.state.drafts._locks import AttemptLock

REVIEW = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 17)
REVISION = ReviewRevision("head-1", "base-1", "start-1")


def make_content(label: str = "draft") -> DraftContent:
    anchor = InlineAnchor(
        REVISION,
        "src/old.py",
        "src/new.py",
        10,
        11,
        DiffSide.NEW,
        context_fingerprint(("before", "selected", "after")),
        start_line=9,
        start_side=DiffSide.OLD,
    )
    return DraftContent(
        body=f"{label} body",
        verdict=DraftVerdict.REQUEST_CHANGES,
        comments=(
            GeneralDraftComment(uuid4(), f"{label} general"),
            InlineDraftComment(uuid4(), f"{label} inline", anchor),
            ReplyDraftComment(uuid4(), f"{label} reply", "thread-9"),
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "application-data" / "drafts.db"


@pytest.mark.asyncio
async def test_round_trip_restart_preserves_ordered_stable_content(
    db_path: Path,
) -> None:
    content = make_content()
    first = DraftStore(db_path)
    await first.open()
    created = await first.create_draft(REVIEW, REVISION, content)
    await first.close()

    second = DraftStore(db_path)
    await second.open()
    recovered = await second.get_draft(created.id)
    await second.close()

    assert recovered == created
    assert tuple(comment.id for comment in recovered.comments) == tuple(
        comment.id for comment in content.comments
    )


@pytest.mark.asyncio
async def test_two_restarted_consumers_share_the_application_data_path(
    db_path: Path,
) -> None:
    first = DraftStore(db_path)
    await first.open()
    draft = await first.create_draft(REVIEW, REVISION)
    await first.close()

    second = DraftStore(db_path)
    await second.open()
    saved = await second.save_draft(
        draft.id, draft.version, make_content("second"), current_revision=REVISION
    )
    await second.close()

    third = DraftStore(db_path)
    await third.open()
    assert await third.get_draft(draft.id) == saved
    await third.close()


def test_default_database_is_separate_from_evictable_cache() -> None:
    assert default_draft_db_path() != Path(user_cache_dir("tongs")) / "cache.db"
    assert default_draft_db_path().name == "drafts.db"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
async def test_database_and_sidecars_are_private(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    await store.create_draft(REVIEW, REVISION)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        assert sidecar.exists()
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    attempt = await store.lock_submission((await store.list_drafts())[0].id, 1)
    lock_file = lock_dir / f"{attempt.id}.lock"
    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    await store.close()
    assert lock_file.exists()


@pytest.mark.asyncio
async def test_save_marks_anchor_stale_without_retargeting(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    caller = make_content()

    saved = await store.save_draft(
        draft.id,
        draft.version,
        caller,
        current_revision=ReviewRevision("head-1", "different-base", "start-1"),
    )

    inline = saved.comments[1]
    assert isinstance(inline, InlineDraftComment)
    assert inline.anchor.stale is True
    assert inline.anchor.revision == REVISION
    assert saved.version == 2
    await store.close()


@pytest.mark.asyncio
async def test_cas_contention_preserves_winner_and_both_caller_payloads(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    one, two = DraftStore(db_path), DraftStore(db_path)
    await asyncio.gather(one.open(), two.open())
    first_content, second_content = make_content("one"), make_content("two")

    results = await asyncio.gather(
        one.save_draft(
            draft.id, draft.version, first_content, current_revision=REVISION
        ),
        two.save_draft(
            draft.id, draft.version, second_content, current_revision=REVISION
        ),
        return_exceptions=True,
    )

    winner = next(result for result in results if not isinstance(result, Exception))
    conflict = next(
        result for result in results if isinstance(result, DraftConflictError)
    )
    assert conflict.current == winner
    assert conflict.caller_content in {first_content, second_content}
    assert conflict.caller_content != winner.content
    assert await one.get_draft(draft.id) == winner
    await asyncio.gather(one.close(), two.close())


@pytest.mark.asyncio
async def test_same_store_serializes_concurrent_create_and_edit(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    created = await asyncio.gather(
        store.create_draft(REVIEW, REVISION),
        store.create_draft(REVIEW, REVISION),
    )
    assert len(await store.list_drafts()) == 2

    one, two = make_content("one"), make_content("two")
    results = await asyncio.gather(
        store.save_draft(
            created[0].id, created[0].version, one, current_revision=REVISION
        ),
        store.save_draft(
            created[0].id, created[0].version, two, current_revision=REVISION
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, DraftConflictError) for result in results) == 1
    await store.close()


@pytest.mark.asyncio
async def test_discard_requires_current_version_and_preserves_conflicting_draft(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    saved = await store.save_draft(
        draft.id, draft.version, make_content(), current_revision=REVISION
    )

    with pytest.raises(DraftConflictError) as raised:
        await store.discard_draft(saved.id, draft.version)
    assert raised.value.current == saved
    assert await store.get_draft(saved.id) == saved
    assert await store.discard_draft(saved.id, saved.version) == saved
    with pytest.raises(DraftNotFoundError):
        await store.get_draft(saved.id)
    await store.close()


@pytest.mark.asyncio
async def test_submission_freezes_version_and_receipts_are_insert_once(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, make_content())
    attempt = await store.lock_submission(draft.id, draft.version)

    assert attempt.frozen_version == draft.version
    assert attempt.snapshot == draft
    assert (await store.get_draft(draft.id)).version == draft.version + 1
    first = await store.record_receipt(attempt.id, "comment:1", "remote-1")
    duplicate = await store.record_receipt(attempt.id, "comment:1", "remote-1")
    assert duplicate.receipts == first.receipts
    with pytest.raises(DraftReceiptConflictError):
        await store.record_receipt(attempt.id, "comment:1", "different")
    assert (await store.get_attempt(attempt.id)).receipts == first.receipts
    await store.close()


@pytest.mark.asyncio
async def test_same_store_serializes_concurrent_receipts(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)

    await asyncio.gather(
        store.record_receipt(attempt.id, "comment:1", "remote-1"),
        store.record_receipt(attempt.id, "comment:2", "remote-2"),
    )

    persisted = await store.get_attempt(attempt.id)
    assert tuple(receipt.step_id for receipt in persisted.receipts) == (
        "comment:1",
        "comment:2",
    )
    await store.close()


@pytest.mark.asyncio
async def test_unknown_attempt_is_noneditable_and_nonreplayable_until_reconciled(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)
    unknown = await store.mark_attempt_unknown(attempt.id)

    with pytest.raises(DraftAttemptOwnedError):
        await store.record_receipt(attempt.id, "body", "remote")
    current = await store.get_draft(draft.id)
    with pytest.raises(DraftStateError):
        await store.save_draft(
            draft.id, current.version, make_content(), current_revision=REVISION
        )
    assert unknown.state == DraftState.UNKNOWN
    assert await store.list_recovery_attempts() == (unknown,)

    resumed = await store.reconcile_attempt(
        attempt.id, ReconciliationResolution.RETRY_REMAINING
    )
    assert resumed.id == attempt.id
    completed = await store.complete_submission(attempt.id)
    assert completed.state == DraftState.SUBMITTED
    assert (await store.get_draft(draft.id)).state == DraftState.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_confirmed_receipt_survives_owner_exit_and_is_not_duplicated(
    db_path: Path,
) -> None:
    owner = DraftStore(db_path)
    await owner.open()
    draft = await owner.create_draft(REVIEW, REVISION)
    attempt = await owner.lock_submission(draft.id, draft.version)
    await owner.record_receipt(attempt.id, "comment:stable", "remote-1")
    await owner.close()

    restarted = DraftStore(db_path)
    await restarted.open()
    recovered = await restarted.recover_incomplete_attempts()
    assert len(recovered) == 1
    assert tuple(receipt.step_id for receipt in recovered[0].receipts) == (
        "comment:stable",
    )
    resumed = await restarted.reconcile_attempt(
        attempt.id, ReconciliationResolution.RETRY_REMAINING
    )
    assert resumed.id == attempt.id
    duplicate = await restarted.record_receipt(attempt.id, "comment:stable", "remote-1")
    assert len(duplicate.receipts) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_return_editable_reconciliation_invalidates_prelock_editors(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.mark_attempt_unknown(attempt.id)
    await store.reconcile_attempt(attempt.id, ReconciliationResolution.RETURN_EDITABLE)

    current = await store.get_draft(draft.id)
    assert current.state == DraftState.EDITABLE
    assert current.version == draft.version + 2
    with pytest.raises(DraftConflictError):
        await store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    await store.close()


@pytest.mark.asyncio
async def test_second_store_recovery_skips_live_owner_then_recovers_after_close(
    db_path: Path,
) -> None:
    owner = DraftStore(db_path)
    observer = DraftStore(db_path)
    await owner.open()
    draft = await owner.create_draft(REVIEW, REVISION)
    attempt = await owner.lock_submission(draft.id, draft.version)
    await observer.open()

    assert await observer.recover_incomplete_attempts() == ()
    with pytest.raises(DraftAttemptOwnedError):
        await observer.reconcile_attempt(
            attempt.id, ReconciliationResolution.RETURN_EDITABLE
        )
    assert (await observer.get_attempt(attempt.id)).state == DraftState.SUBMITTING

    await owner.close()
    recovered = await observer.recover_incomplete_attempts()
    assert tuple(item.id for item in recovered) == (attempt.id,)
    assert recovered[0].state == DraftState.UNKNOWN
    await observer.close()


def _child_lock_submission(
    db_path: str, draft_id: str, version: int, ready: object
) -> None:
    async def run() -> None:
        store = DraftStore(Path(db_path))
        await store.open()
        await store.lock_submission(UUID(draft_id), version)
        ready.set()  # type: ignore[attr-defined]
        await asyncio.Event().wait()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Fedora process-lock proof")
async def test_child_process_death_releases_attempt_for_unknown_recovery(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(
        target=_child_lock_submission,
        args=(str(db_path), str(draft.id), draft.version, ready),
    )
    child.start()
    assert await asyncio.to_thread(ready.wait, 10)
    observer = DraftStore(db_path)
    await observer.open()
    attempt = (await observer.list_drafts())[0]
    assert attempt.state == DraftState.SUBMITTING
    assert await observer.recover_incomplete_attempts() == ()

    child.terminate()
    await asyncio.to_thread(child.join, 10)
    assert child.is_alive() is False
    recovered = await observer.recover_incomplete_attempts()
    assert len(recovered) == 1
    assert recovered[0].state == DraftState.UNKNOWN
    await observer.close()


@pytest.mark.asyncio
async def test_concurrent_submission_lock_has_one_attempt_and_no_leaked_owner(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    one, two = DraftStore(db_path), DraftStore(db_path)
    await asyncio.gather(one.open(), two.open())

    results = await asyncio.gather(
        one.lock_submission(draft.id, draft.version),
        two.lock_submission(draft.id, draft.version),
        return_exceptions=True,
    )
    attempts = [result for result in results if not isinstance(result, Exception)]
    assert len(attempts) == 1
    assert (
        sum(
            isinstance(result, (DraftConflictError, DraftStateError))
            for result in results
        )
        == 1
    )
    await asyncio.gather(one.close(), two.close())

    recovery = DraftStore(db_path)
    await recovery.open()
    assert len(await recovery.recover_incomplete_attempts()) == 1
    await recovery.close()


@pytest.mark.asyncio
async def test_failed_duplicate_attempt_id_releases_extra_lock(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    first = await store.create_draft(REVIEW, REVISION)
    second = await store.create_draft(
        ReviewRef(REVIEW.repository, REVIEW.number + 1), REVISION
    )
    attempt_id = uuid4()
    first_attempt = await store.lock_submission(
        first.id, first.version, attempt_id=attempt_id
    )
    await store.complete_submission(first_attempt.id)

    with pytest.raises(DraftStoreError, match="lock failed"):
        await store.lock_submission(second.id, second.version, attempt_id=attempt_id)
    probe = AttemptLock.try_acquire(
        db_path.with_name(f"{db_path.name}.locks"), str(attempt_id)
    )
    assert probe is not None
    probe.release()
    replacement = await store.lock_submission(second.id, second.version)
    assert replacement.state == DraftState.SUBMITTING
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_lock_rolls_back_and_releases_attempt_owner(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    original = store_module._snapshot_to_json

    def cancel_after_lock(snapshot: object) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(store_module, "_snapshot_to_json", cancel_after_lock)
    with pytest.raises(asyncio.CancelledError):
        await store.lock_submission(draft.id, draft.version)
    monkeypatch.setattr(store_module, "_snapshot_to_json", original)

    assert (await store.get_draft(draft.id)).state == DraftState.EDITABLE
    attempt = await store.lock_submission(draft.id, draft.version)
    assert attempt.state == DraftState.SUBMITTING
    await store.close()


@pytest.mark.asyncio
async def test_close_waits_for_in_flight_transaction(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store._draft_in_transaction

    async def blocking_read(db: aiosqlite.Connection, draft_id: UUID) -> object:
        entered.set()
        await release.wait()
        return await original(db, draft_id)

    monkeypatch.setattr(store, "_draft_in_transaction", blocking_read)
    save = asyncio.create_task(
        store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    )
    await entered.wait()
    close = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert close.done() is False

    release.set()
    await save
    await close
    with pytest.raises(DraftNotOpenError):
        await store.get_draft(draft.id)


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_rejects_newer_schema(db_path: Path) -> None:
    first = DraftStore(db_path)
    await first.open()
    await first.close()
    second = DraftStore(db_path)
    await second.open()
    await second.close()
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        db.execute("PRAGMA user_version=99")

    with pytest.raises(DraftSchemaError, match="newer"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_declared_current_but_incomplete_schema_fails_on_open(
    db_path: Path,
) -> None:
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE drafts(id TEXT PRIMARY KEY)")
        db.execute("PRAGMA user_version=1")
    os.chmod(db_path, 0o600)

    with pytest.raises(DraftSchemaError, match="incomplete"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_without_destroying_existing_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy VALUES ('preserve-me')")
    os.chmod(db_path, 0o600)
    monkeypatch.setitem(
        store_module._MIGRATIONS,
        1,
        ("CREATE TABLE temporary(value TEXT)", "THIS IS NOT SQL"),
    )

    with pytest.raises(DraftStoreError):
        await DraftStore(db_path).open()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT value FROM legacy").fetchone() == ("preserve-me",)
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='temporary'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_corrupt_database_is_not_replaced(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    original = b"not a sqlite database"
    db_path.write_bytes(original)
    os.chmod(db_path, 0o600)

    with pytest.raises(DraftCorruptionError):
        await DraftStore(db_path).open()
    assert db_path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
async def test_permissive_existing_database_fails_closed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    db_path.touch(mode=0o644)

    with pytest.raises(DraftPermissionError, match="other users"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_database_creation_permission_error_is_explicit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = os.open

    def deny_database(path: object, flags: int, mode: int = 0o777) -> int:
        if Path(path) == db_path:
            raise PermissionError("denied")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny_database)
    with pytest.raises(DraftPermissionError, match="cannot open"):
        await DraftStore(db_path).open()
    assert db_path.exists() is False


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink policy")
async def test_attempt_lock_rejects_symlink_without_touching_target(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt_id = uuid4()
    target = db_path.parent / "unrelated"
    target.write_text("preserve")
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    lock_dir.mkdir(mode=0o700)
    (lock_dir / f"{attempt_id}.lock").symlink_to(target)

    with pytest.raises(DraftPermissionError):
        await store.lock_submission(draft.id, draft.version, attempt_id=attempt_id)
    assert target.read_text() == "preserve"
    assert (await store.get_draft(draft.id)).state == DraftState.EDITABLE
    await store.close()


@pytest.mark.asyncio
async def test_write_failure_preserves_last_valid_snapshot(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    assert store._db is not None
    await store._db.execute(
        """
        CREATE TRIGGER simulate_full_disk BEFORE UPDATE ON drafts
        BEGIN SELECT RAISE(FAIL, 'database or disk is full'); END
        """
    )

    with pytest.raises(DraftStoreError, match="write failed"):
        await store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    assert await store.get_draft(draft.id) == draft
    await store.close()
