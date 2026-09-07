"""Versioned protocol primitives for the production desktop sidecar."""

from __future__ import annotations

from tongs.desktop.protocol.messages import (
    MAX_EVENT_FRAME_BYTES,
    MAX_PENDING_REQUESTS,
    MAX_QUEUED_EVENTS,
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    PROTOCOL_MAJOR,
    CancelFrame,
    ProtocolError,
    ProtocolErrorCode,
    RequestFrame,
    decode_frame,
    encode_event,
    encode_response,
)
from tongs.desktop.protocol.state import (
    MAX_SNAPSHOT_RETAINED_BYTES,
    HandleKind,
    HandleRegistry,
    SnapshotPage,
    SnapshotStore,
)

__all__ = [
    "MAX_EVENT_FRAME_BYTES",
    "MAX_PENDING_REQUESTS",
    "MAX_QUEUED_EVENTS",
    "MAX_REQUEST_FRAME_BYTES",
    "MAX_RESPONSE_FRAME_BYTES",
    "MAX_SNAPSHOT_RETAINED_BYTES",
    "PROTOCOL_MAJOR",
    "CancelFrame",
    "HandleKind",
    "HandleRegistry",
    "ProtocolError",
    "ProtocolErrorCode",
    "RequestFrame",
    "SnapshotPage",
    "SnapshotStore",
    "decode_frame",
    "encode_event",
    "encode_response",
]
