"""Strict bounded NDJSON message schema for desktop protocol major 1."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import cast

PROTOCOL_MAJOR = 1
MAX_REQUEST_FRAME_BYTES = 256 * 1024
MAX_RESPONSE_FRAME_BYTES = 8 * 1024 * 1024
MAX_EVENT_FRAME_BYTES = 64 * 1024
MAX_PENDING_REQUESTS = 64
MAX_QUEUED_EVENTS = 256
MAX_JSON_DEPTH = 24
MAX_JSON_ITEMS = 20_000

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METHOD_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class ProtocolErrorCode(StrEnum):
    """Stable safe failure categories exposed on the wire."""

    INVALID_FRAME = "invalid_frame"
    FRAME_TOO_LARGE = "frame_too_large"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    HANDSHAKE_REQUIRED = "handshake_required"
    ALREADY_HANDSHAKEN = "already_handshaken"
    INVALID_REQUEST = "invalid_request"
    INVALID_PARAMS = "invalid_params"
    UNKNOWN_METHOD = "unknown_method"
    DUPLICATE_REQUEST = "duplicate_request"
    TOO_MANY_REQUESTS = "too_many_requests"
    INVALID_HANDLE = "invalid_handle"
    WRONG_HANDLE_KIND = "wrong_handle_kind"
    REQUEST_CANCELLED = "request_cancelled"
    SERVICE_ERROR = "service_error"
    PLUGIN_ERROR = "plugin_error"
    SNAPSHOT_EXPIRED = "snapshot_expired"
    RESPONSE_TOO_LARGE = "response_too_large"
    EVENT_OVERFLOW = "event_overflow"
    SHUTTING_DOWN = "shutting_down"
    INTERNAL = "internal"


class ProtocolError(Exception):
    """A redacted protocol failure suitable for serialization."""

    def __init__(
        self,
        code: ProtocolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, JsonScalar] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})

    def to_wire(self) -> JsonObject:
        result: JsonObject = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            result["details"] = cast(JsonValue, dict(self.details))
        return result


@dataclass(frozen=True, slots=True)
class RequestFrame:
    request_id: str
    method: str
    params: JsonObject


@dataclass(frozen=True, slots=True)
class CancelFrame:
    request_id: str


type IncomingFrame = RequestFrame | CancelFrame


def decode_frame(raw: bytes) -> IncomingFrame:
    """Parse one complete NDJSON frame with strict fields and bounded JSON."""
    if len(raw) > MAX_REQUEST_FRAME_BYTES:
        raise ProtocolError(
            ProtocolErrorCode.FRAME_TOO_LARGE,
            "The request frame exceeds the supported size.",
        )
    if not raw.endswith(b"\n"):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "Protocol frames must end with a newline.",
        )
    payload = raw[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    if not payload:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "The request frame is empty.",
        )
    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except ProtocolError:
        raise
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "The request frame is not valid UTF-8 JSON.",
        ) from None
    _validate_json(document)
    if not isinstance(document, dict):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "The request frame must be a JSON object.",
        )
    if type(document.get("v")) is not int or document.get("v") != PROTOCOL_MAJOR:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The desktop protocol major is not supported.",
        )
    frame_type = document.get("type")
    if frame_type == "request":
        if set(document) != {"v", "type", "id", "method", "params"}:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_REQUEST,
                "The request frame has unknown or missing fields.",
            )
        request_id = _request_id(document.get("id"))
        method = document.get("method")
        params = document.get("params")
        if (
            not isinstance(method, str)
            or len(method) > 120
            or not _METHOD_RE.fullmatch(method)
            or not isinstance(params, dict)
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_REQUEST,
                "The request method or parameters are invalid.",
            )
        return RequestFrame(request_id, method, cast(JsonObject, params))
    if frame_type == "cancel":
        if set(document) != {"v", "type", "id"}:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_REQUEST,
                "The cancellation frame has unknown or missing fields.",
            )
        return CancelFrame(_request_id(document.get("id")))
    raise ProtocolError(
        ProtocolErrorCode.INVALID_FRAME,
        "The protocol frame type is unknown.",
    )


def encode_response(
    request_id: str | None,
    *,
    result: object | None = None,
    error: ProtocolError | None = None,
) -> bytes:
    """Encode one response and replace oversized output with a bounded error."""
    document: JsonObject = {"v": PROTOCOL_MAJOR, "type": "response", "id": request_id}
    try:
        if error is None:
            document["result"] = to_json_value(result)
        else:
            document["error"] = cast(JsonValue, error.to_wire())
        encoded = _encode(document)
    except (ProtocolError, TypeError, ValueError, UnicodeError):
        encoded = _encode_response_fallback(
            request_id,
            ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "The response could not be encoded safely.",
            ),
        )
    if len(encoded) <= MAX_RESPONSE_FRAME_BYTES:
        return encoded
    fallback = ProtocolError(
        ProtocolErrorCode.RESPONSE_TOO_LARGE,
        "The response exceeds the supported size; request a smaller page.",
        retryable=True,
    )
    return _encode_response_fallback(request_id, fallback)


def encode_event(sequence: int, event: str, data: object) -> bytes:
    """Encode one event, enforcing the dedicated event-frame limit."""
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise ValueError("event sequence must be positive")
    if not _METHOD_RE.fullmatch(event):
        raise ValueError("invalid event name")
    try:
        encoded = _encode(
            {
                "v": PROTOCOL_MAJOR,
                "type": "event",
                "sequence": sequence,
                "event": event,
                "data": to_json_value(data),
            }
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProtocolError(
            ProtocolErrorCode.EVENT_OVERFLOW,
            "The event could not be encoded safely.",
        ) from error
    if len(encoded) > MAX_EVENT_FRAME_BYTES:
        raise ProtocolError(
            ProtocolErrorCode.EVENT_OVERFLOW,
            "The event exceeds the supported size.",
        )
    return encoded


def to_json_value(value: object, *, _depth: int = 0) -> JsonValue:
    """Convert controlled immutable service values into strict JSON values."""
    if _depth > MAX_JSON_DEPTH:
        raise ProtocolError(
            ProtocolErrorCode.RESPONSE_TOO_LARGE,
            "The response value is nested too deeply.",
        )
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        _validate_unicode(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "The response contained an invalid number.",
            )
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_json_value(value.value, _depth=_depth + 1)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_json_value(getattr(value, item.name), _depth=_depth + 1)
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(
                    ProtocolErrorCode.INTERNAL,
                    "The response contained an invalid object key.",
                )
            _validate_unicode(key)
            result[key] = to_json_value(item, _depth=_depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item, _depth=_depth + 1) for item in value]
    raise ProtocolError(
        ProtocolErrorCode.INTERNAL,
        "The response contained an unsupported value.",
    )


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_REQUEST,
            "The request id is invalid.",
        )
    return value


def _validate_json(value: object) -> None:
    budget = [MAX_JSON_ITEMS]

    def visit(item: object, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "The request JSON is nested too deeply.",
            )
        budget[0] -= 1
        if budget[0] < 0:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "The request JSON contains too many values.",
            )
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, str):
            _validate_unicode(item)
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_FRAME,
                    "The request JSON contains an invalid number.",
                )
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ProtocolError(
                        ProtocolErrorCode.INVALID_FRAME,
                        "The request JSON contains an invalid object key.",
                    )
                _validate_unicode(key)
                visit(child, depth + 1)
            return
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "The request contains an unsupported JSON value.",
        )

    visit(value, 0)


def _encode(document: JsonObject) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _encode_response_fallback(request_id: str | None, error: ProtocolError) -> bytes:
    safe_id = request_id
    try:
        if safe_id is not None:
            _validate_unicode(safe_id)
    except (ProtocolError, UnicodeError):
        safe_id = None
    return _encode(
        {
            "v": PROTOCOL_MAJOR,
            "type": "response",
            "id": safe_id,
            "error": cast(JsonValue, error.to_wire()),
        }
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "The request JSON contains a duplicate object key.",
            )
        result[key] = value
    return result


def _validate_unicode(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "The JSON text contains invalid Unicode.",
        ) from error


__all__ = [
    "MAX_EVENT_FRAME_BYTES",
    "MAX_PENDING_REQUESTS",
    "MAX_QUEUED_EVENTS",
    "MAX_REQUEST_FRAME_BYTES",
    "MAX_RESPONSE_FRAME_BYTES",
    "PROTOCOL_MAJOR",
    "CancelFrame",
    "IncomingFrame",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "ProtocolError",
    "ProtocolErrorCode",
    "RequestFrame",
    "decode_frame",
    "encode_event",
    "encode_response",
    "to_json_value",
]
