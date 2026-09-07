"""Transactional SQLite storage for durable review drafts."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Concatenate, Self, cast
from uuid import UUID, uuid4

import aiosqlite
from platformdirs import user_data_dir

from tongs.scanner.repo import ForgeType
from tongs.services.models import RepositoryRef, ReviewRef, ReviewRevision
from tongs.state.drafts._locks import AttemptLock
from tongs.state.drafts.errors import (
    DraftAttemptOwnedError,
    DraftConflictError,
    DraftCorruptionError,
    DraftNotFoundError,
    DraftNotOpenError,
    DraftPermissionError,
    DraftReceiptConflictError,
    DraftSchemaError,
    DraftStateError,
    DraftStoreError,
)
from tongs.state.drafts.models import (
    DiffSide,
    DraftComment,
    DraftContent,
    DraftSnapshot,
    DraftState,
    DraftVerdict,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    PendingSubmissionDispatch,
    ReconciliationRecord,
    ReconciliationResolution,
    ReplyDraftComment,
    StepReceipt,
    SubmissionAttempt,
    SubmissionPlanRecord,
    SubmissionPlanStepRecord,
    SubmissionRetryAuthorization,
    UnknownSubmissionOutcome,
)

_SCHEMA_VERSION = 2
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE drafts (
            id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            project_path TEXT NOT NULL,
            review_number INTEGER NOT NULL CHECK(review_number > 0),
            head_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            start_sha TEXT,
            version INTEGER NOT NULL CHECK(version > 0),
            body TEXT NOT NULL,
            verdict TEXT,
            comments_json TEXT NOT NULL,
            state TEXT NOT NULL,
            active_attempt_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX drafts_review_idx
        ON drafts(hostname, project_path, review_number, updated_at)
        """,
        """
        CREATE TABLE submission_attempts (
            id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
            frozen_version INTEGER NOT NULL CHECK(frozen_version > 0),
            snapshot_json TEXT NOT NULL,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE submission_receipts (
            attempt_id TEXT NOT NULL
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id, step_id)
        )
        """,
        """
        CREATE TABLE submission_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            resolution TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
    ),
    2: (
        """
        CREATE TABLE submission_retry_authorizations (
            attempt_id TEXT NOT NULL
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal > 0),
            step_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id, ordinal)
        )
        """,
        """
        CREATE TABLE submission_unknown_outcomes (
            attempt_id TEXT NOT NULL
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal > 0),
            step_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id, ordinal)
        )
        """,
        """
        CREATE TABLE submission_pending_dispatches (
            attempt_id TEXT PRIMARY KEY
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE submission_plans (
            attempt_id TEXT PRIMARY KEY
                REFERENCES submission_attempts(id) ON DELETE CASCADE,
            forge TEXT NOT NULL,
            atomic INTEGER NOT NULL CHECK(atomic IN (0, 1)),
            steps_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
    ),
}


def _plan_steps_to_json(steps: tuple[SubmissionPlanStepRecord, ...]) -> str:
    return json.dumps(
        [
            {
                "step_id": step.step_id,
                "kind": step.kind,
                "comment_ids": [str(comment_id) for comment_id in step.comment_ids],
            }
            for step in steps
        ],
        separators=(",", ":"),
    )


def _plan_steps_from_json(value: str) -> tuple[SubmissionPlanStepRecord, ...]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise TypeError("submission plan steps must be a list")
    return tuple(
        SubmissionPlanStepRecord(
            step_id=cast(str, item["step_id"]),
            kind=cast(str, item["kind"]),
            comment_ids=tuple(UUID(cast(str, value)) for value in item["comment_ids"]),
        )
        for item in cast(list[dict[str, object]], data)
    )


def default_draft_db_path() -> Path:
    """Return the dedicated application-data path for durable drafts."""
    return Path(user_data_dir("tongs")) / "drafts.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp lacks a timezone")
    return parsed


def _validate_version(version: int) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("expected_version must be a positive integer")


def _serialized[**P, R](
    method: Callable[Concatenate[DraftStore, P], Awaitable[R]],
) -> Callable[Concatenate[DraftStore, P], Awaitable[R]]:
    """Serialize complete operations sharing one aiosqlite connection."""

    @functools.wraps(method)
    async def wrapped(self: DraftStore, *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._operation_lock:
            return await method(self, *args, **kwargs)

    return wrapped


def _anchor_to_data(anchor: InlineAnchor) -> dict[str, object]:
    return {
        "revision": {
            "head_sha": anchor.revision.head_sha,
            "base_sha": anchor.revision.base_sha,
            "start_sha": anchor.revision.start_sha,
        },
        "old_path": anchor.old_path,
        "new_path": anchor.new_path,
        "old_line": anchor.old_line,
        "new_line": anchor.new_line,
        "side": anchor.side.value,
        "context_fingerprint": anchor.context_fingerprint,
        "start_line": anchor.start_line,
        "start_side": anchor.start_side.value if anchor.start_side else None,
        "stale": anchor.stale,
    }


def _comment_to_data(comment: DraftComment) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": comment.kind,
        "id": str(comment.id),
        "body": comment.body,
    }
    if isinstance(comment, InlineDraftComment):
        data["anchor"] = _anchor_to_data(comment.anchor)
    elif isinstance(comment, ReplyDraftComment):
        data["thread_id"] = comment.thread_id
    return data


def _revision_from_data(data: object) -> ReviewRevision:
    values = cast(dict[str, object], data)
    return ReviewRevision(
        head_sha=cast(str, values["head_sha"]),
        base_sha=cast(str, values["base_sha"]),
        start_sha=cast(str | None, values.get("start_sha")),
    )


def _anchor_from_data(data: object) -> InlineAnchor:
    values = cast(dict[str, object], data)
    start_side = values.get("start_side")
    return InlineAnchor(
        revision=_revision_from_data(values["revision"]),
        old_path=cast(str, values["old_path"]),
        new_path=cast(str, values["new_path"]),
        old_line=cast(int | None, values.get("old_line")),
        new_line=cast(int | None, values.get("new_line")),
        side=DiffSide(cast(str, values["side"])),
        context_fingerprint=cast(str, values["context_fingerprint"]),
        start_line=cast(int | None, values.get("start_line")),
        start_side=DiffSide(cast(str, start_side)) if start_side else None,
        stale=cast(bool, values.get("stale", False)),
    )


def _comment_from_data(data: object) -> DraftComment:
    values = cast(dict[str, object], data)
    comment_id = UUID(cast(str, values["id"]))
    body = cast(str, values["body"])
    kind = values["kind"]
    if kind == "general":
        return GeneralDraftComment(comment_id, body)
    if kind == "inline":
        return InlineDraftComment(comment_id, body, _anchor_from_data(values["anchor"]))
    if kind == "reply":
        return ReplyDraftComment(comment_id, body, cast(str, values["thread_id"]))
    raise ValueError("unknown draft comment kind")


def _comments_to_json(comments: tuple[DraftComment, ...]) -> str:
    return json.dumps(
        [_comment_to_data(comment) for comment in comments], separators=(",", ":")
    )


def _comments_from_json(value: str) -> tuple[DraftComment, ...]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise TypeError("comments payload must be a list")
    return tuple(_comment_from_data(item) for item in data)


def _snapshot_to_json(snapshot: DraftSnapshot) -> str:
    return json.dumps(
        {
            "id": str(snapshot.id),
            "review": {
                "hostname": snapshot.review.repository.hostname,
                "project_path": snapshot.review.repository.project_path,
                "number": snapshot.review.number,
            },
            "revision": {
                "head_sha": snapshot.revision.head_sha,
                "base_sha": snapshot.revision.base_sha,
                "start_sha": snapshot.revision.start_sha,
            },
            "version": snapshot.version,
            "body": snapshot.body,
            "verdict": snapshot.verdict.value if snapshot.verdict else None,
            "comments": [_comment_to_data(comment) for comment in snapshot.comments],
            "state": snapshot.state.value,
            "created_at": _format_time(snapshot.created_at),
            "updated_at": _format_time(snapshot.updated_at),
        },
        separators=(",", ":"),
    )


def _snapshot_from_json(value: str) -> DraftSnapshot:
    data = cast(dict[str, object], json.loads(value))
    review_data = cast(dict[str, object], data["review"])
    verdict = data.get("verdict")
    comments = data["comments"]
    if not isinstance(comments, list):
        raise TypeError("snapshot comments payload must be a list")
    return DraftSnapshot(
        id=UUID(cast(str, data["id"])),
        review=ReviewRef(
            RepositoryRef(
                cast(str, review_data["hostname"]),
                cast(str, review_data["project_path"]),
            ),
            cast(int, review_data["number"]),
        ),
        revision=_revision_from_data(data["revision"]),
        version=cast(int, data["version"]),
        body=cast(str, data["body"]),
        verdict=DraftVerdict(cast(str, verdict)) if verdict else None,
        comments=tuple(_comment_from_data(item) for item in comments),
        state=DraftState(cast(str, data["state"])),
        created_at=_parse_time(cast(str, data["created_at"])),
        updated_at=_parse_time(cast(str, data["updated_at"])),
    )


class DraftStore:
    """A process-safe durable draft store with optimistic content revisions."""

    def __init__(
        self, db_path: Path | None = None, *, busy_timeout_ms: int = 5_000
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._db_path = db_path or default_draft_db_path()
        self._lock_dir = self._db_path.with_name(f"{self._db_path.name}.locks")
        self._busy_timeout_ms = busy_timeout_ms
        self._db: aiosqlite.Connection | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._held_attempt_locks: dict[UUID, AttemptLock] = {}

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    @_serialized
    async def open(self) -> None:
        """Open and atomically migrate the dedicated draft database."""
        async with self._lifecycle_lock:
            if self._db is not None:
                return
            created = False
            db: aiosqlite.Connection | None = None
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not self._db_path.exists():
                    try:
                        fd = os.open(
                            self._db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                        )
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                        created = True
                self._validate_private_file(self._db_path)
                db = await aiosqlite.connect(self._db_path, isolation_level=None)
                db.row_factory = aiosqlite.Row
                await db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
                check = await (await db.execute("PRAGMA quick_check")).fetchone()
                if check is None or check[0] != "ok":
                    raise DraftCorruptionError("draft database integrity check failed")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("PRAGMA journal_mode=WAL")
                await self._migrate(db)
                await self._validate_schema(db)
                self._secure_sidecars()
                self._db = db
            except (DraftStoreError, OSError, sqlite3.Error) as error:
                if db is not None:
                    await db.close()
                if isinstance(error, DraftStoreError):
                    raise
                if isinstance(error, PermissionError):
                    raise DraftPermissionError(
                        "cannot open private draft storage"
                    ) from error
                message = "cannot initialize draft database"
                if not created and isinstance(error, sqlite3.DatabaseError):
                    raise DraftCorruptionError(message) from error
                raise DraftStoreError(message) from error
            except BaseException:
                if db is not None:
                    await db.close()
                raise

    @_serialized
    async def close(self) -> None:
        """Close the connection and release all live attempt ownership locks."""
        async with self._lifecycle_lock:
            db, self._db = self._db, None
            locks = tuple(self._held_attempt_locks.values())
            self._held_attempt_locks.clear()
            for lock in locks:
                lock.release()
            if db is not None:
                await db.close()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        try:
            await db.execute("BEGIN EXCLUSIVE")
            row = await (await db.execute("PRAGMA user_version")).fetchone()
            version = int(row[0]) if row else 0
            if version > _SCHEMA_VERSION:
                raise DraftSchemaError(
                    f"draft schema {version} is newer than supported schema {_SCHEMA_VERSION}"
                )
            for target in range(version + 1, _SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target]:
                    await db.execute(statement)
                await db.execute(f"PRAGMA user_version={target}")
            await db.execute("COMMIT")
        except BaseException:
            await self._rollback(db)
            raise

    async def _validate_schema(self, db: aiosqlite.Connection) -> None:
        required = {
            "drafts": {
                "id",
                "hostname",
                "project_path",
                "review_number",
                "head_sha",
                "base_sha",
                "start_sha",
                "version",
                "body",
                "verdict",
                "comments_json",
                "state",
                "active_attempt_id",
                "created_at",
                "updated_at",
            },
            "submission_attempts": {
                "id",
                "draft_id",
                "frozen_version",
                "snapshot_json",
                "state",
                "started_at",
                "updated_at",
            },
            "submission_receipts": {
                "attempt_id",
                "step_id",
                "remote_id",
                "recorded_at",
            },
            "submission_reconciliations": {
                "id",
                "attempt_id",
                "resolution",
                "recorded_at",
            },
            "submission_retry_authorizations": {
                "attempt_id",
                "ordinal",
                "step_id",
                "recorded_at",
            },
            "submission_unknown_outcomes": {
                "attempt_id",
                "ordinal",
                "step_id",
                "reason",
                "recorded_at",
            },
            "submission_pending_dispatches": {
                "attempt_id",
                "step_id",
                "operation_id",
                "recorded_at",
            },
            "submission_plans": {
                "attempt_id",
                "forge",
                "atomic",
                "steps_json",
                "recorded_at",
            },
        }
        for table, expected_columns in required.items():
            rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            columns = {row[1] for row in rows}
            if not expected_columns.issubset(columns):
                raise DraftSchemaError("draft database schema is incomplete")

    def _validate_private_file(self, path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DraftPermissionError("draft database path is not a regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise DraftPermissionError("draft database permits access by other users")

    def _secure_sidecars(self) -> None:
        if os.name == "nt":
            return
        for suffix in ("-wal", "-shm"):
            path = Path(f"{self._db_path}{suffix}")
            if path.exists():
                try:
                    path.chmod(0o600)
                except OSError as error:
                    raise DraftPermissionError(
                        "cannot restrict draft database sidecar permissions"
                    ) from error

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise DraftNotOpenError("draft store is not open")
        return self._db

    @_serialized
    async def create_draft(
        self,
        review: ReviewRef,
        revision: ReviewRevision,
        content: DraftContent | None = None,
        *,
        draft_id: UUID | None = None,
    ) -> DraftSnapshot:
        """Create a durable editable draft with a stable UUID."""
        if not isinstance(review, ReviewRef) or not isinstance(
            revision, ReviewRevision
        ):
            raise TypeError("review and revision must use service identity types")
        content = content or DraftContent()
        self._validate_content_revision(content, revision)
        identifier = draft_id or uuid4()
        now = _now()
        snapshot = DraftSnapshot(
            identifier,
            review,
            revision,
            1,
            content.body,
            content.verdict,
            content.comments,
            DraftState.EDITABLE,
            now,
            now,
        )
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO drafts (
                    id, hostname, project_path, review_number,
                    head_sha, base_sha, start_sha, version, body, verdict,
                    comments_json, state, active_attempt_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    str(identifier),
                    review.repository.hostname,
                    review.repository.project_path,
                    review.number,
                    revision.head_sha,
                    revision.base_sha,
                    revision.start_sha,
                    1,
                    content.body,
                    content.verdict.value if content.verdict else None,
                    _comments_to_json(content.comments),
                    DraftState.EDITABLE.value,
                    _format_time(now),
                    _format_time(now),
                ),
            )
            await db.execute("COMMIT")
            self._secure_sidecars()
            return snapshot
        except sqlite3.IntegrityError as error:
            await self._rollback(db)
            raise DraftStoreError("draft identity already exists") from error
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("draft database write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def get_draft(self, draft_id: UUID) -> DraftSnapshot:
        """Load one immutable current draft snapshot."""
        db = self._connection()
        try:
            row = await (
                await db.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),))
            ).fetchone()
        except sqlite3.Error as error:
            raise DraftStoreError("draft database read failed") from error
        if row is None:
            raise DraftNotFoundError("draft does not exist")
        return self._row_to_snapshot(row)

    @_serialized
    async def list_drafts(
        self,
        *,
        review: ReviewRef | None = None,
        states: frozenset[DraftState] | None = None,
    ) -> tuple[DraftSnapshot, ...]:
        """List drafts by review and durable state, newest first."""
        clauses: list[str] = []
        values: list[object] = []
        if review is not None:
            clauses.extend(("hostname = ?", "project_path = ?", "review_number = ?"))
            values.extend(
                (
                    review.repository.hostname,
                    review.repository.project_path,
                    review.number,
                )
            )
        if states is not None:
            if not states:
                return ()
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            values.extend(state.value for state in states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        db = self._connection()
        try:
            rows = await (
                await db.execute(
                    f"SELECT * FROM drafts{where} ORDER BY updated_at DESC, id", values
                )
            ).fetchall()
            return tuple(self._row_to_snapshot(row) for row in rows)
        except sqlite3.Error as error:
            raise DraftStoreError("draft database read failed") from error

    @_serialized
    async def save_draft(
        self,
        draft_id: UUID,
        expected_version: int,
        content: DraftContent,
        *,
        current_revision: ReviewRevision,
    ) -> DraftSnapshot:
        """Replace editable content using optimistic compare-and-swap."""
        _validate_version(expected_version)
        if not isinstance(content, DraftContent):
            raise TypeError("content must be DraftContent")
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await self._draft_in_transaction(db, draft_id)
            self._assert_expected(current, expected_version, content)
            if current.state != DraftState.EDITABLE:
                raise DraftStateError(
                    "only editable drafts can be saved", current=current
                )
            self._validate_content_revision(content, current.revision)
            persisted = content.assessed_against(current_revision)
            now = _now()
            cursor = await db.execute(
                """
                UPDATE drafts
                SET version = version + 1, body = ?, verdict = ?, comments_json = ?,
                    updated_at = ?
                WHERE id = ? AND version = ? AND state = ?
                """,
                (
                    persisted.body,
                    persisted.verdict.value if persisted.verdict else None,
                    _comments_to_json(persisted.comments),
                    _format_time(now),
                    str(draft_id),
                    expected_version,
                    DraftState.EDITABLE.value,
                ),
            )
            if cursor.rowcount != 1:
                current = await self._draft_in_transaction(db, draft_id)
                self._assert_expected(current, expected_version, content)
                raise DraftStateError(
                    "only editable drafts can be saved", current=current
                )
            result = await self._draft_in_transaction(db, draft_id)
            await db.execute("COMMIT")
            return result
        except (DraftConflictError, DraftStateError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("draft database write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def discard_draft(
        self, draft_id: UUID, expected_version: int
    ) -> DraftSnapshot:
        """Delete an editable draft using optimistic compare-and-swap."""
        _validate_version(expected_version)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await self._draft_in_transaction(db, draft_id)
            self._assert_expected(current, expected_version, None)
            if current.state != DraftState.EDITABLE:
                raise DraftStateError(
                    "only editable drafts can be discarded", current=current
                )
            cursor = await db.execute(
                "DELETE FROM drafts WHERE id = ? AND version = ? AND state = ?",
                (str(draft_id), expected_version, DraftState.EDITABLE.value),
            )
            if cursor.rowcount != 1:
                current = await self._draft_in_transaction(db, draft_id)
                self._assert_expected(current, expected_version, None)
                raise DraftStateError(
                    "only editable drafts can be discarded", current=current
                )
            await db.execute("COMMIT")
            return current
        except (DraftConflictError, DraftStateError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("draft database write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def lock_submission(
        self, draft_id: UUID, expected_version: int, *, attempt_id: UUID | None = None
    ) -> SubmissionAttempt:
        """Freeze and lock an exact editable version before any network write."""
        _validate_version(expected_version)
        identifier = attempt_id or uuid4()
        db = self._connection()
        ownership: AttemptLock | None = None
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await self._draft_in_transaction(db, draft_id)
            self._assert_expected(current, expected_version, None)
            if current.state != DraftState.EDITABLE:
                raise DraftStateError(
                    "only editable drafts can begin submission", current=current
                )
            ownership = AttemptLock.try_acquire(self._lock_dir, str(identifier))
            if ownership is None:
                raise DraftAttemptOwnedError(
                    "submission attempt is owned by another process"
                )
            now = _now()
            await db.execute(
                """
                INSERT INTO submission_attempts (
                    id, draft_id, frozen_version, snapshot_json, state, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(identifier),
                    str(draft_id),
                    expected_version,
                    _snapshot_to_json(current),
                    DraftState.SUBMITTING.value,
                    _format_time(now),
                    _format_time(now),
                ),
            )
            cursor = await db.execute(
                """
                UPDATE drafts
                SET state = ?, version = version + 1, active_attempt_id = ?, updated_at = ?
                WHERE id = ? AND version = ? AND state = ?
                """,
                (
                    DraftState.SUBMITTING.value,
                    str(identifier),
                    _format_time(now),
                    str(draft_id),
                    expected_version,
                    DraftState.EDITABLE.value,
                ),
            )
            if cursor.rowcount != 1:
                raise DraftConflictError(
                    expected_version=expected_version,
                    current=await self._draft_in_transaction(db, draft_id),
                    caller_content=None,
                )
            result = await self._attempt_in_transaction(db, identifier)
            await db.execute("COMMIT")
            self._held_attempt_locks[identifier] = ownership
            ownership = None
            return result
        except (
            DraftAttemptOwnedError,
            DraftConflictError,
            DraftStateError,
            DraftNotFoundError,
        ):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("draft submission lock failed") from error
        except BaseException:
            await self._rollback(db)
            raise
        finally:
            if ownership is not None:
                ownership.release()

    @_serialized
    async def record_plan(
        self, attempt_id: UUID, plan: SubmissionPlanRecord
    ) -> SubmissionAttempt:
        """Persist the exact validated plan once before any remote dispatch."""
        if not isinstance(plan, SubmissionPlanRecord):
            raise TypeError("plan must be a SubmissionPlanRecord")
        self._require_owned(attempt_id)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                raise DraftStoreError("plan recording requires an active attempt")
            if attempt.plan is not None:
                if attempt.plan != plan:
                    raise DraftStoreError(
                        "submission plan is already bound differently"
                    )
                await db.execute("COMMIT")
                return attempt
            if attempt.pending_dispatch is not None or attempt.receipts:
                raise DraftStoreError("submission plan must precede every remote write")
            now = _now()
            await db.execute(
                """
                INSERT INTO submission_plans
                    (attempt_id, forge, atomic, steps_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(attempt_id),
                    plan.forge.value,
                    int(plan.atomic),
                    _plan_steps_to_json(plan.steps),
                    _format_time(now),
                ),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission plan write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def begin_dispatch(
        self, attempt_id: UUID, step_id: str, operation_id: str
    ) -> SubmissionAttempt:
        """Durably mark the only remote step that may now be dispatched."""
        if not step_id or not operation_id:
            raise ValueError("step_id and operation_id are required")
        self._require_owned(attempt_id)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                raise DraftStoreError("dispatch requires an active submission attempt")
            if any(receipt.step_id == step_id for receipt in attempt.receipts):
                raise DraftStoreError("confirmed submission steps cannot be dispatched")
            if attempt.pending_dispatch is not None:
                if (
                    attempt.pending_dispatch.step_id == step_id
                    and attempt.pending_dispatch.operation_id == operation_id
                ):
                    await db.execute("COMMIT")
                    return attempt
                raise DraftStoreError("another submission step may already be remote")
            now = _now()
            await db.execute(
                """
                INSERT INTO submission_pending_dispatches
                    (attempt_id, step_id, operation_id, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(attempt_id), step_id, operation_id, _format_time(now)),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission dispatch journal failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def reject_dispatch(
        self, attempt_id: UUID, step_id: str, operation_id: str
    ) -> SubmissionAttempt:
        """Clear a pending step after the forge definitely rejected it."""
        self._require_owned(attempt_id)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            pending = attempt.pending_dispatch
            if (
                pending is None
                or pending.step_id != step_id
                or pending.operation_id != operation_id
            ):
                raise DraftStoreError("submission dispatch journal changed")
            await db.execute(
                "DELETE FROM submission_pending_dispatches WHERE attempt_id = ?",
                (str(attempt_id),),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission dispatch rejection failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def record_receipt(
        self,
        attempt_id: UUID,
        step_id: str,
        remote_id: str,
        *,
        operation_id: str | None = None,
    ) -> SubmissionAttempt:
        """Persist a confirmed remote step once under the live attempt lock."""
        if not step_id or not remote_id or operation_id == "":
            raise ValueError("step_id and remote_id are required")
        self._require_owned(attempt_id)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                raise DraftStoreError(
                    "receipts require an active known submission outcome"
                )
            existing = await (
                await db.execute(
                    "SELECT remote_id FROM submission_receipts WHERE attempt_id = ? AND step_id = ?",
                    (str(attempt_id), step_id),
                )
            ).fetchone()
            if existing is not None:
                if existing[0] != remote_id:
                    raise DraftReceiptConflictError(
                        "submission step already has a different remote receipt"
                    )
                await db.execute("COMMIT")
                return attempt
            pending = attempt.pending_dispatch
            if operation_id is not None and (
                pending is None
                or pending.step_id != step_id
                or pending.operation_id != operation_id
            ):
                raise DraftStoreError("submission receipt has no matching dispatch")
            now = _now()
            await db.execute(
                "INSERT INTO submission_receipts VALUES (?, ?, ?, ?)",
                (str(attempt_id), step_id, remote_id, _format_time(now)),
            )
            if operation_id is not None:
                await db.execute(
                    "DELETE FROM submission_pending_dispatches WHERE attempt_id = ?",
                    (str(attempt_id),),
                )
            await db.execute(
                "UPDATE submission_attempts SET state = ?, updated_at = ? WHERE id = ?",
                (DraftState.PARTIAL.value, _format_time(now), str(attempt_id)),
            )
            await db.execute(
                "UPDATE drafts SET state = ?, updated_at = ? WHERE id = ? AND active_attempt_id = ?",
                (
                    DraftState.PARTIAL.value,
                    _format_time(now),
                    str(attempt.draft_id),
                    str(attempt_id),
                ),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission receipt write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def authorize_retry(
        self, attempt_id: UUID, step_id: str
    ) -> SubmissionAttempt:
        """Durably authorize one explicit retry of an unconfirmed active step."""
        if not step_id:
            raise ValueError("step_id is required")
        self._require_owned(attempt_id)
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                raise DraftStoreError("retry authorization requires an active attempt")
            if attempt.pending_dispatch is not None:
                raise DraftStoreError("a pending remote step must be reconciled first")
            if any(receipt.step_id == step_id for receipt in attempt.receipts):
                raise DraftStoreError("confirmed submission steps cannot be retried")
            ordinal = (
                max(
                    (
                        authorization.ordinal
                        for authorization in attempt.retry_authorizations
                    ),
                    default=0,
                )
                + 1
            )
            now = _now()
            await db.execute(
                """
                INSERT INTO submission_retry_authorizations
                    (attempt_id, ordinal, step_id, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(attempt_id), ordinal, step_id, _format_time(now)),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission retry authorization failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def cancel_submission(self, attempt_id: UUID) -> DraftSnapshot:
        """Restore a service-proven never-dispatched attempt to editable state."""
        self._require_owned(attempt_id)
        db = self._connection()
        completed = False
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if (
                attempt.state != DraftState.SUBMITTING
                or attempt.receipts
                or attempt.pending_dispatch is not None
            ):
                raise DraftStoreError(
                    "only a never-dispatched submission attempt can be cancelled"
                )
            now = _now()
            updated = await db.execute(
                """
                UPDATE drafts
                SET state = ?, active_attempt_id = NULL, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND active_attempt_id = ? AND state = ?
                """,
                (
                    DraftState.EDITABLE.value,
                    _format_time(now),
                    str(attempt.draft_id),
                    str(attempt_id),
                    DraftState.SUBMITTING.value,
                ),
            )
            if updated.rowcount != 1:
                raise DraftStoreError("the active submission attempt changed")
            await db.execute(
                "DELETE FROM submission_attempts WHERE id = ?", (str(attempt_id),)
            )
            result = await self._draft_in_transaction(db, attempt.draft_id)
            await db.execute("COMMIT")
            completed = True
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission cancellation failed") from error
        except BaseException:
            await self._rollback(db)
            raise
        finally:
            if completed:
                self._release_owned(attempt_id)

    @_serialized
    async def complete_submission(self, attempt_id: UUID) -> SubmissionAttempt:
        """Mark an owned attempt and its draft submitted, then release ownership."""
        self._require_owned(attempt_id)
        try:
            return await self._transition_owned(attempt_id, DraftState.SUBMITTED)
        finally:
            self._release_owned(attempt_id)

    @_serialized
    async def mark_attempt_unknown(
        self, attempt_id: UUID, *, step_id: str | None = None, reason: str = "unknown"
    ) -> SubmissionAttempt:
        """Durably record an ambiguous owned outcome before releasing ownership."""
        if step_id is not None and (not step_id or not reason):
            raise ValueError("unknown step_id and reason must be non-empty")
        self._require_owned(attempt_id)
        try:
            return await self._transition_owned(
                attempt_id,
                DraftState.UNKNOWN,
                unknown_step_id=step_id,
                unknown_reason=reason,
            )
        finally:
            self._release_owned(attempt_id)

    async def _transition_owned(
        self,
        attempt_id: UUID,
        target: DraftState,
        *,
        unknown_step_id: str | None = None,
        unknown_reason: str = "unknown",
    ) -> SubmissionAttempt:
        db = self._connection()
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                raise DraftStoreError("submission attempt is not active")
            now = _now()
            pending = attempt.pending_dispatch
            if pending is not None:
                if unknown_step_id is not None and unknown_step_id != pending.step_id:
                    raise DraftStoreError("unknown outcome does not match pending step")
                unknown_step_id = pending.step_id
            if unknown_step_id is not None:
                ordinal = len(attempt.unknown_outcomes) + 1
                await db.execute(
                    """
                    INSERT INTO submission_unknown_outcomes
                        (attempt_id, ordinal, step_id, reason, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(attempt_id),
                        ordinal,
                        unknown_step_id,
                        unknown_reason,
                        _format_time(now),
                    ),
                )
            if pending is not None:
                await db.execute(
                    "DELETE FROM submission_pending_dispatches WHERE attempt_id = ?",
                    (str(attempt_id),),
                )
            await db.execute(
                "UPDATE submission_attempts SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, _format_time(now), str(attempt_id)),
            )
            await db.execute(
                """
                UPDATE drafts SET state = ?, active_attempt_id = ?, updated_at = ?
                WHERE id = ? AND active_attempt_id = ?
                """,
                (
                    target.value,
                    str(attempt_id) if target == DraftState.UNKNOWN else None,
                    _format_time(now),
                    str(attempt.draft_id),
                    str(attempt_id),
                ),
            )
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission state write failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def recover_incomplete_attempts(self) -> tuple[SubmissionAttempt, ...]:
        """Mark only attempts whose process ownership lock is no longer held unknown."""
        db = self._connection()
        try:
            rows = await (
                await db.execute(
                    """
                SELECT id FROM submission_attempts
                WHERE state IN (?, ?) ORDER BY started_at, id
                """,
                    (DraftState.SUBMITTING.value, DraftState.PARTIAL.value),
                )
            ).fetchall()
        except sqlite3.Error as error:
            raise DraftStoreError("submission recovery query failed") from error
        recovered: list[SubmissionAttempt] = []
        for row in rows:
            attempt_id = UUID(row[0])
            if attempt_id in self._held_attempt_locks:
                continue
            ownership = AttemptLock.try_acquire(self._lock_dir, str(attempt_id))
            if ownership is None:
                continue
            try:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    attempt = await self._attempt_in_transaction(db, attempt_id)
                    if attempt.state not in {DraftState.SUBMITTING, DraftState.PARTIAL}:
                        await db.execute("COMMIT")
                        continue
                    now = _now()
                    if attempt.pending_dispatch is not None:
                        await db.execute(
                            """
                            INSERT INTO submission_unknown_outcomes
                                (attempt_id, ordinal, step_id, reason, recorded_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                str(attempt_id),
                                len(attempt.unknown_outcomes) + 1,
                                attempt.pending_dispatch.step_id,
                                "process_interrupted",
                                _format_time(now),
                            ),
                        )
                        await db.execute(
                            "DELETE FROM submission_pending_dispatches WHERE attempt_id = ?",
                            (str(attempt_id),),
                        )
                    await db.execute(
                        "UPDATE submission_attempts SET state = ?, updated_at = ? WHERE id = ?",
                        (DraftState.UNKNOWN.value, _format_time(now), str(attempt_id)),
                    )
                    await db.execute(
                        """
                        UPDATE drafts SET state = ?, updated_at = ?
                        WHERE id = ? AND active_attempt_id = ?
                        """,
                        (
                            DraftState.UNKNOWN.value,
                            _format_time(now),
                            str(attempt.draft_id),
                            str(attempt_id),
                        ),
                    )
                    result = await self._attempt_in_transaction(db, attempt_id)
                    await db.execute("COMMIT")
                    recovered.append(result)
                except sqlite3.Error as error:
                    await self._rollback(db)
                    raise DraftStoreError("submission recovery write failed") from error
                except BaseException:
                    await self._rollback(db)
                    raise
            finally:
                ownership.release()
        return tuple(recovered)

    @_serialized
    async def list_recovery_attempts(self) -> tuple[SubmissionAttempt, ...]:
        """Return unresolved unknown attempts requiring an explicit user decision."""
        db = self._connection()
        try:
            await db.execute("BEGIN")
            rows = await (
                await db.execute(
                    """
                SELECT a.id FROM submission_attempts a
                JOIN drafts d ON d.active_attempt_id = a.id
                WHERE a.state = ? AND d.state = ?
                ORDER BY a.updated_at, a.id
                """,
                    (DraftState.UNKNOWN.value, DraftState.UNKNOWN.value),
                )
            ).fetchall()
            attempts = []
            for row in rows:
                attempts.append(await self._attempt_in_transaction(db, UUID(row[0])))
            await db.execute("COMMIT")
            return tuple(attempts)
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission recovery query failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    @_serialized
    async def reconcile_attempt(
        self,
        attempt_id: UUID,
        resolution: ReconciliationResolution,
        *,
        editable_content: DraftContent | None = None,
    ) -> SubmissionAttempt:
        """Persist an explicit decision for an outcome-unknown attempt."""
        if not isinstance(resolution, ReconciliationResolution):
            raise TypeError("resolution must be a ReconciliationResolution")
        if attempt_id in self._held_attempt_locks:
            raise DraftAttemptOwnedError("cannot reconcile a live submission attempt")
        db = self._connection()
        ownership = AttemptLock.try_acquire(self._lock_dir, str(attempt_id))
        if ownership is None:
            raise DraftAttemptOwnedError(
                "submission attempt is owned by another process"
            )
        keep_ownership = False
        try:
            await db.execute("BEGIN IMMEDIATE")
            attempt = await self._attempt_in_transaction(db, attempt_id)
            if attempt.state != DraftState.UNKNOWN:
                raise DraftStoreError("only outcome-unknown attempts can be reconciled")
            current = await (
                await db.execute(
                    "SELECT state, active_attempt_id FROM drafts WHERE id = ?",
                    (str(attempt.draft_id),),
                )
            ).fetchone()
            if (
                current is None
                or current["state"] != DraftState.UNKNOWN.value
                or current["active_attempt_id"] != str(attempt_id)
            ):
                raise DraftStoreError(
                    "only the active unknown attempt can be reconciled"
                )
            if resolution == ReconciliationResolution.RETURN_EDITABLE:
                if editable_content is None:
                    if attempt.receipts:
                        raise DraftStoreError(
                            "confirmed receipts require explicit remaining content"
                        )
                    editable_content = attempt.snapshot.content
                elif not isinstance(editable_content, DraftContent):
                    raise TypeError("editable_content must be DraftContent")
                self._validate_content_revision(
                    editable_content, attempt.snapshot.revision
                )
            elif editable_content is not None:
                raise ValueError(
                    "editable_content is only valid when returning a draft to editing"
                )
            now = _now()
            await db.execute(
                "INSERT INTO submission_reconciliations (attempt_id, resolution, recorded_at) VALUES (?, ?, ?)",
                (str(attempt_id), resolution.value, _format_time(now)),
            )
            if resolution == ReconciliationResolution.RETRY_REMAINING:
                target = (
                    DraftState.PARTIAL if attempt.receipts else DraftState.SUBMITTING
                )
                active_attempt: str | None = str(attempt_id)
            elif resolution == ReconciliationResolution.MARK_SUBMITTED:
                target = DraftState.SUBMITTED
                active_attempt = None
            else:
                target = DraftState.EDITABLE
                active_attempt = None
            attempt_state = (
                target if target != DraftState.EDITABLE else DraftState.UNKNOWN
            )
            await db.execute(
                "UPDATE submission_attempts SET state = ?, updated_at = ? WHERE id = ?",
                (attempt_state.value, _format_time(now), str(attempt_id)),
            )
            updated = await db.execute(
                """
                UPDATE drafts
                SET state = ?, active_attempt_id = ?, body = ?, verdict = ?,
                    comments_json = ?,
                    version = version + CASE WHEN ? = ? THEN 1 ELSE 0 END,
                    updated_at = ?
                WHERE id = ? AND active_attempt_id = ? AND state = ?
                """,
                (
                    target.value,
                    active_attempt,
                    editable_content.body
                    if editable_content is not None
                    else attempt.snapshot.body,
                    (
                        editable_content.verdict.value
                        if editable_content is not None and editable_content.verdict
                        else attempt.snapshot.verdict.value
                        if attempt.snapshot.verdict
                        else None
                    ),
                    _comments_to_json(
                        editable_content.comments
                        if editable_content is not None
                        else attempt.snapshot.comments
                    ),
                    target.value,
                    DraftState.EDITABLE.value,
                    _format_time(now),
                    str(attempt.draft_id),
                    str(attempt_id),
                    DraftState.UNKNOWN.value,
                ),
            )
            if updated.rowcount != 1:
                raise DraftStoreError("the active unknown attempt changed")
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            if resolution == ReconciliationResolution.RETRY_REMAINING:
                self._held_attempt_locks[attempt_id] = ownership
                keep_ownership = True
            return result
        except (DraftStoreError, DraftNotFoundError):
            await self._rollback(db)
            raise
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission reconciliation write failed") from error
        except BaseException:
            await self._rollback(db)
            raise
        finally:
            if not keep_ownership:
                ownership.release()

    @_serialized
    async def get_attempt(self, attempt_id: UUID) -> SubmissionAttempt:
        """Load a submission attempt, its frozen snapshot, receipts and decisions."""
        db = self._connection()
        try:
            await db.execute("BEGIN")
            result = await self._attempt_in_transaction(db, attempt_id)
            await db.execute("COMMIT")
            return result
        except sqlite3.Error as error:
            await self._rollback(db)
            raise DraftStoreError("submission attempt read failed") from error
        except BaseException:
            await self._rollback(db)
            raise

    async def _draft_in_transaction(
        self, db: aiosqlite.Connection, draft_id: UUID
    ) -> DraftSnapshot:
        row = await (
            await db.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),))
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft does not exist")
        return self._row_to_snapshot(row)

    async def _attempt_in_transaction(
        self, db: aiosqlite.Connection, attempt_id: UUID
    ) -> SubmissionAttempt:
        row = await (
            await db.execute(
                "SELECT * FROM submission_attempts WHERE id = ?", (str(attempt_id),)
            )
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("submission attempt does not exist")
        return await self._row_to_attempt(db, row)

    def _row_to_snapshot(self, row: aiosqlite.Row) -> DraftSnapshot:
        try:
            verdict = row["verdict"]
            return DraftSnapshot(
                UUID(row["id"]),
                ReviewRef(
                    RepositoryRef(row["hostname"], row["project_path"]),
                    row["review_number"],
                ),
                ReviewRevision(row["head_sha"], row["base_sha"], row["start_sha"]),
                row["version"],
                row["body"],
                DraftVerdict(verdict) if verdict else None,
                _comments_from_json(row["comments_json"]),
                DraftState(row["state"]),
                _parse_time(row["created_at"]),
                _parse_time(row["updated_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DraftCorruptionError("stored draft record is invalid") from error

    async def _row_to_attempt(
        self, db: aiosqlite.Connection, row: aiosqlite.Row
    ) -> SubmissionAttempt:
        try:
            attempt_id = UUID(row["id"])
            receipt_rows = await (
                await db.execute(
                    "SELECT step_id, remote_id, recorded_at FROM submission_receipts WHERE attempt_id = ? ORDER BY rowid",
                    (str(attempt_id),),
                )
            ).fetchall()
            reconciliation_rows = await (
                await db.execute(
                    "SELECT resolution, recorded_at FROM submission_reconciliations WHERE attempt_id = ? ORDER BY id",
                    (str(attempt_id),),
                )
            ).fetchall()
            retry_rows = await (
                await db.execute(
                    """
                    SELECT step_id, ordinal, recorded_at
                    FROM submission_retry_authorizations
                    WHERE attempt_id = ? ORDER BY ordinal
                    """,
                    (str(attempt_id),),
                )
            ).fetchall()
            unknown_rows = await (
                await db.execute(
                    """
                    SELECT step_id, ordinal, reason, recorded_at
                    FROM submission_unknown_outcomes
                    WHERE attempt_id = ? ORDER BY ordinal
                    """,
                    (str(attempt_id),),
                )
            ).fetchall()
            pending_row = await (
                await db.execute(
                    """
                    SELECT step_id, operation_id, recorded_at
                    FROM submission_pending_dispatches WHERE attempt_id = ?
                    """,
                    (str(attempt_id),),
                )
            ).fetchone()
            plan_row = await (
                await db.execute(
                    """
                    SELECT forge, atomic, steps_json
                    FROM submission_plans WHERE attempt_id = ?
                    """,
                    (str(attempt_id),),
                )
            ).fetchone()
            return SubmissionAttempt(
                attempt_id,
                UUID(row["draft_id"]),
                row["frozen_version"],
                _snapshot_from_json(row["snapshot_json"]),
                DraftState(row["state"]),
                tuple(
                    StepReceipt(item[0], item[1], _parse_time(item[2]))
                    for item in receipt_rows
                ),
                tuple(
                    ReconciliationRecord(
                        ReconciliationResolution(item[0]), _parse_time(item[1])
                    )
                    for item in reconciliation_rows
                ),
                _parse_time(row["started_at"]),
                _parse_time(row["updated_at"]),
                tuple(
                    SubmissionRetryAuthorization(item[0], item[1], _parse_time(item[2]))
                    for item in retry_rows
                ),
                tuple(
                    UnknownSubmissionOutcome(
                        item[0], item[1], item[2], _parse_time(item[3])
                    )
                    for item in unknown_rows
                ),
                PendingSubmissionDispatch(
                    pending_row[0], pending_row[1], _parse_time(pending_row[2])
                )
                if pending_row is not None
                else None,
                SubmissionPlanRecord(
                    ForgeType(plan_row[0]),
                    bool(plan_row[1]),
                    _plan_steps_from_json(plan_row[2]),
                )
                if plan_row is not None
                else None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DraftCorruptionError(
                "stored submission attempt is invalid"
            ) from error

    def _validate_content_revision(
        self, content: DraftContent, revision: ReviewRevision
    ) -> None:
        for comment in content.comments:
            if (
                isinstance(comment, InlineDraftComment)
                and comment.anchor.revision != revision
            ):
                raise ValueError(
                    "inline anchors must retain the draft's captured revision"
                )

    def _assert_expected(
        self,
        current: DraftSnapshot,
        expected_version: int,
        caller_content: DraftContent | None,
    ) -> None:
        if current.version != expected_version:
            raise DraftConflictError(
                expected_version=expected_version,
                current=current,
                caller_content=caller_content,
            )

    def _require_owned(self, attempt_id: UUID) -> None:
        if attempt_id not in self._held_attempt_locks:
            raise DraftAttemptOwnedError(
                "submission attempt is not owned by this process"
            )

    def _release_owned(self, attempt_id: UUID) -> None:
        lock = self._held_attempt_locks.pop(attempt_id, None)
        if lock is not None:
            lock.release()

    @staticmethod
    async def _rollback(db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ROLLBACK")
        except sqlite3.Error:
            pass


__all__ = ["DraftStore", "default_draft_db_path"]
