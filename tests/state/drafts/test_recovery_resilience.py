"""Startup recovery keeps going past vanished, resolved, or damaged attempts."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest

from tongs.services import RepositoryRef, ReviewRef, ReviewRevision
from tongs.state.drafts import (
    DraftContent,
    DraftState,
    DraftStore,
    GeneralDraftComment,
    RecoveryWarning,
    SubmissionAttempt,
)
from tongs.state.drafts.models import RECOVERY_CORRUPT_ATTEMPT_MESSAGE

REVISION = ReviewRevision("head-1", "base-1", "start-1")
SECRET_BODY = "private review text that must never appear in a warning"


def _review(number: int) -> ReviewRef:
    return ReviewRef(RepositoryRef("github.com", "acme/widgets"), number)


def _content() -> DraftContent:
    return DraftContent(
        body=SECRET_BODY,
        comments=(
            GeneralDraftComment(UUID("00000000-0000-4000-8000-000000000001"), "c"),
        ),
    )


async def _abandoned_attempts(db_path: Path, count: int) -> list[SubmissionAttempt]:
    """Create attempts whose owning process has exited, as after a crash."""
    setup = DraftStore(db_path)
    await setup.open()
    attempts: list[SubmissionAttempt] = []
    for number in range(1, count + 1):
        draft = await setup.create_draft(_review(number), REVISION, _content())
        attempts.append(await setup.lock_submission(draft.id, draft.version))
    await setup.close()
    return attempts


def _corrupt_attempt(db_path: Path, attempt_id: UUID) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE submission_attempts SET updated_at = ? WHERE id = ?",
            ("not-a-timestamp", str(attempt_id)),
        )


class _Cursor:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[object]:
        return self._rows


class _InterleavingConnection:
    """Run another store's action right after recovery lists its attempts."""

    def __init__(
        self,
        connection: aiosqlite.Connection,
        action: Callable[[], Awaitable[object]],
    ) -> None:
        self._connection = connection
        self._action = action
        self.interleaved = False

    async def execute(self, sql: str, parameters: object = ()) -> object:
        cursor = await self._connection.execute(sql, parameters)  # type: ignore[arg-type]
        if not self.interleaved and "SELECT id FROM submission_attempts" in sql:
            rows = list(await cursor.fetchall())
            self.interleaved = True
            await self._action()
            return _Cursor(rows)
        return cursor


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "drafts.db"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["cancel", "complete"])
async def test_attempt_resolved_by_another_store_during_recovery_is_skipped(
    db_path: Path, resolution: str
) -> None:
    owner = DraftStore(db_path)
    recoverer = DraftStore(db_path)
    await owner.open()
    first = await owner.create_draft(_review(1), REVISION, _content())
    resolved = await owner.lock_submission(first.id, first.version)
    await recoverer.open()
    # A second attempt whose owner is gone, so recovery still has work to do.
    orphan_owner = DraftStore(db_path)
    await orphan_owner.open()
    second = await orphan_owner.create_draft(_review(2), REVISION, _content())
    orphan = await orphan_owner.lock_submission(second.id, second.version)
    await orphan_owner.close()

    async def resolve() -> object:
        if resolution == "cancel":
            return await owner.cancel_submission(resolved.id)
        return await owner.complete_submission(resolved.id)

    proxy = _InterleavingConnection(recoverer._connection(), resolve)
    real_connection = recoverer._connection
    recoverer._connection = lambda: proxy  # type: ignore[assignment,method-assign]
    try:
        recovered = await recoverer.recover_incomplete_attempts()
    finally:
        recoverer._connection = real_connection  # type: ignore[method-assign]

    assert proxy.interleaved is True
    assert tuple(item.id for item in recovered) == (orphan.id,)
    assert recovered[0].state is DraftState.UNKNOWN
    assert recoverer.recovery_warnings == ()
    expected = DraftState.EDITABLE if resolution == "cancel" else DraftState.SUBMITTED
    assert (await recoverer.get_draft(first.id)).state is expected
    # The skipped attempt's lock was released, so a later pass can take it.
    assert await recoverer.recover_incomplete_attempts() == ()
    await owner.close()
    await recoverer.close()


@pytest.mark.asyncio
async def test_corrupt_attempt_is_reported_and_others_still_recover(
    db_path: Path,
) -> None:
    attempts = await _abandoned_attempts(db_path, 3)
    damaged = attempts[1]
    _corrupt_attempt(db_path, damaged.id)

    store = DraftStore(db_path)
    await store.open()
    recovered = await store.recover_incomplete_attempts()

    assert {item.id for item in recovered} == {attempts[0].id, attempts[2].id}
    assert all(item.state is DraftState.UNKNOWN for item in recovered)
    assert store.recovery_warnings == (RecoveryWarning(damaged.id),)
    warning = store.recovery_warnings[0]
    assert warning.message == RECOVERY_CORRUPT_ATTEMPT_MESSAGE
    assert str(damaged.id) in warning.describe()
    assert SECRET_BODY not in warning.describe()
    # The damaged attempt and its draft are kept, never deleted.
    with sqlite3.connect(db_path) as connection:
        attempt_rows = connection.execute(
            "SELECT state FROM submission_attempts WHERE id = ?", (str(damaged.id),)
        ).fetchall()
        draft_rows = connection.execute(
            "SELECT state, body FROM drafts WHERE id = ?",
            (str(damaged.draft_id),),
        ).fetchall()
    assert attempt_rows == [(DraftState.SUBMITTING.value,)]
    assert draft_rows == [(DraftState.SUBMITTING.value, SECRET_BODY)]
    await store.close()


@pytest.mark.asyncio
async def test_unparseable_attempt_id_is_reported_without_identity(
    db_path: Path,
) -> None:
    attempts = await _abandoned_attempts(db_path, 1)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO submission_attempts
            SELECT 'not-a-uuid', draft_id, frozen_version, snapshot_json, state,
                   started_at, updated_at
            FROM submission_attempts WHERE id = ?
            """,
            (str(attempts[0].id),),
        )

    store = DraftStore(db_path)
    await store.open()
    recovered = await store.recover_incomplete_attempts()

    assert tuple(item.id for item in recovered) == (attempts[0].id,)
    assert store.recovery_warnings == (RecoveryWarning(None),)
    assert store.recovery_warnings[0].describe() == RECOVERY_CORRUPT_ATTEMPT_MESSAGE
    await store.close()
