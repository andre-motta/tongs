"""Per-connection opaque handles and revision-bound paging snapshots."""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar, cast

from tongs.desktop.protocol.messages import (
    JsonObject,
    JsonValue,
    ProtocolError,
    ProtocolErrorCode,
)

_DEFAULT_MAX_HANDLES = 8192
_DEFAULT_MAX_SNAPSHOTS = 32
_DEFAULT_SNAPSHOT_TTL_SECONDS = 300.0
MAX_SNAPSHOT_RETAINED_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_RETAINED_ROWS = 100_000
_DEFAULT_PAGE_ITEMS = 400
_MAX_PAGE_ITEMS = 1000
_PAGE_TARGET_BYTES = 512 * 1024
_HandleT = TypeVar("_HandleT")


class HandleKind(StrEnum):
    REPOSITORY = "repository"
    REVIEW = "review"
    PIPELINE = "pipeline"
    JOB = "job"


@dataclass(frozen=True, slots=True)
class _HandleRecord:
    kind: HandleKind
    value: object


class HandleRegistry:
    """Issue unpredictable handles valid only for one sidecar connection."""

    def __init__(self, *, max_handles: int = _DEFAULT_MAX_HANDLES) -> None:
        if max_handles <= 0:
            raise ValueError("max_handles must be positive")
        self.session_id = secrets.token_urlsafe(24)
        self._max_handles = max_handles
        self._records: dict[str, _HandleRecord] = {}
        self._reverse: dict[tuple[HandleKind, object], str] = {}

    def issue(self, kind: HandleKind, value: object) -> str:
        key = (kind, value)
        existing = self._reverse.get(key)
        if existing is not None:
            return existing
        if len(self._records) >= self._max_handles:
            raise ProtocolError(
                ProtocolErrorCode.TOO_MANY_REQUESTS,
                "The session resource-handle limit was reached.",
                retryable=True,
            )
        token = self._new_token()
        self._records[token] = _HandleRecord(kind, value)
        self._reverse[key] = token
        return token

    def resolve(
        self, token: object, kind: HandleKind, expected_type: type[_HandleT]
    ) -> _HandleT:
        if not isinstance(token, str):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_HANDLE,
                "The resource handle is invalid or expired.",
            )
        record = self._records.get(token)
        if record is None:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_HANDLE,
                "The resource handle is invalid or expired.",
            )
        if record.kind is not kind:
            raise ProtocolError(
                ProtocolErrorCode.WRONG_HANDLE_KIND,
                "The resource handle has the wrong kind for this operation.",
            )
        if not isinstance(record.value, expected_type):
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "The resource handle could not be resolved safely.",
            )
        return cast(_HandleT, record.value)

    def find(self, kind: HandleKind, value: object) -> str | None:
        return self._reverse.get((kind, value))

    def clear(self) -> None:
        self._records.clear()
        self._reverse.clear()

    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(24)
            if token not in self._records:
                return token


@dataclass(frozen=True, slots=True)
class SnapshotPage:
    snapshot_id: str
    resource_handle: str
    revision: JsonObject
    cursor: int
    next_cursor: int | None
    entries: tuple[JsonObject, ...]
    projection: str | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    snapshot_id: str
    resource_handle: str
    revision: bytes
    entries: tuple[bytes, ...]
    byte_count: int
    created_at: float
    projection: str | None


class SnapshotStore:
    """Retain bounded session-local snapshots and return byte-aware pages."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_SNAPSHOT_TTL_SECONDS,
        max_snapshots: int = _DEFAULT_MAX_SNAPSHOTS,
        max_retained_bytes: int = MAX_SNAPSHOT_RETAINED_BYTES,
        max_retained_rows: int = _DEFAULT_MAX_RETAINED_ROWS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            ttl_seconds <= 0
            or max_snapshots <= 0
            or max_retained_bytes <= 0
            or max_retained_rows <= 0
        ):
            raise ValueError("snapshot limits must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_snapshots = max_snapshots
        self._max_retained_bytes = max_retained_bytes
        self._max_retained_rows = max_retained_rows
        self._clock = clock
        self._snapshots: dict[str, _Snapshot] = {}
        self._order: deque[str] = deque()
        self._expired: deque[str] = deque(maxlen=max_snapshots * 4)
        self._retained_bytes = 0
        self._retained_rows = 0

    def create(
        self,
        resource_handle: str,
        revision: Mapping[str, JsonValue],
        entries: Sequence[Mapping[str, JsonValue]],
        *,
        projection: str | None = None,
    ) -> str:
        self.prune()
        encoded_revision = _encode_object(revision)
        if (
            len(encoded_revision) > self._max_retained_bytes
            or len(entries) > self._max_retained_rows
        ):
            raise ProtocolError(
                ProtocolErrorCode.RESPONSE_TOO_LARGE,
                "The snapshot exceeds the session retention limit.",
            )
        retained: list[bytes] = []
        byte_count = len(encoded_revision)
        for entry in entries:
            encoded_entry = _encode_object(entry)
            byte_count += len(encoded_entry)
            if byte_count > self._max_retained_bytes:
                raise ProtocolError(
                    ProtocolErrorCode.RESPONSE_TOO_LARGE,
                    "The snapshot exceeds the session retention limit.",
                )
            retained.append(encoded_entry)
        encoded_entries = tuple(retained)
        row_count = len(encoded_entries)
        while self._snapshots and (
            len(self._snapshots) >= self._max_snapshots
            or self._retained_bytes + byte_count > self._max_retained_bytes
            or self._retained_rows + row_count > self._max_retained_rows
        ):
            self._expire(self._order.popleft())
        snapshot_id = self._new_token()
        snapshot = _Snapshot(
            snapshot_id,
            resource_handle,
            encoded_revision,
            encoded_entries,
            byte_count,
            self._clock(),
            projection,
        )
        self._snapshots[snapshot_id] = snapshot
        self._order.append(snapshot_id)
        self._retained_bytes += byte_count
        self._retained_rows += row_count
        return snapshot_id

    def page(
        self,
        snapshot_id: object,
        resource_handle: object,
        cursor: object = 0,
        max_items: object = _DEFAULT_PAGE_ITEMS,
    ) -> SnapshotPage:
        snapshot = self._resolve(snapshot_id, resource_handle)
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
            or cursor > len(snapshot.entries)
            or not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or not 1 <= max_items <= _MAX_PAGE_ITEMS
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The snapshot cursor or page size is invalid.",
            )
        selected: list[JsonObject] = []
        size = 0
        next_index = cursor
        for encoded_entry in snapshot.entries[cursor : cursor + max_items]:
            entry_size = len(encoded_entry)
            if selected and size + entry_size > _PAGE_TARGET_BYTES:
                break
            if entry_size > _PAGE_TARGET_BYTES * 8:
                raise ProtocolError(
                    ProtocolErrorCode.RESPONSE_TOO_LARGE,
                    "A diff entry exceeds the supported response size.",
                )
            selected.append(_decode_object(encoded_entry))
            size += entry_size
            next_index += 1
        next_cursor = next_index if next_index < len(snapshot.entries) else None
        return SnapshotPage(
            snapshot.snapshot_id,
            snapshot.resource_handle,
            _decode_object(snapshot.revision),
            cursor,
            next_cursor,
            tuple(selected),
            snapshot.projection,
        )

    def expire_for_resource(self, resource_handle: str) -> None:
        for snapshot_id, snapshot in tuple(self._snapshots.items()):
            if snapshot.resource_handle == resource_handle:
                self._expire(snapshot_id)

    def prune(self) -> None:
        now = self._clock()
        for snapshot_id, snapshot in tuple(self._snapshots.items()):
            if now - snapshot.created_at >= self._ttl_seconds:
                self._expire(snapshot_id)

    def clear(self) -> None:
        for snapshot_id in tuple(self._snapshots):
            self._expire(snapshot_id)

    def _resolve(self, snapshot_id: object, resource_handle: object) -> _Snapshot:
        if not isinstance(snapshot_id, str) or not isinstance(resource_handle, str):
            raise ProtocolError(
                ProtocolErrorCode.SNAPSHOT_EXPIRED,
                "The resource snapshot is invalid or expired; refetch it.",
                retryable=True,
            )
        self.prune()
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None or snapshot.resource_handle != resource_handle:
            raise ProtocolError(
                ProtocolErrorCode.SNAPSHOT_EXPIRED,
                "The resource snapshot is invalid or expired; refetch it.",
                retryable=True,
            )
        return snapshot

    def _expire(self, snapshot_id: str) -> None:
        snapshot = self._snapshots.pop(snapshot_id, None)
        if snapshot is not None:
            self._retained_bytes -= snapshot.byte_count
            self._retained_rows -= len(snapshot.entries)
            self._expired.append(snapshot_id)
        try:
            self._order.remove(snapshot_id)
        except ValueError:
            pass

    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(24)
            if token not in self._snapshots and token not in self._expired:
                return token


def _encode_object(value: Mapping[str, JsonValue]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProtocolError(
            ProtocolErrorCode.INTERNAL,
            "The snapshot contained an invalid value.",
        ) from error


def _decode_object(value: bytes) -> JsonObject:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ProtocolError(
            ProtocolErrorCode.INTERNAL,
            "The snapshot could not be decoded safely.",
        )
    return cast(JsonObject, decoded)


__all__ = [
    "MAX_SNAPSHOT_RETAINED_BYTES",
    "HandleKind",
    "HandleRegistry",
    "SnapshotPage",
    "SnapshotStore",
]
