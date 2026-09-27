from __future__ import annotations

import json

import pytest

from tongs.desktop.protocol.messages import (
    MAX_RESPONSE_FRAME_BYTES,
    JsonLimits,
    ProtocolError,
    ProtocolErrorCode,
    RequestFrame,
    decode_frame,
    encode_event,
    encode_response,
)


def _request(version: object = 1) -> bytes:
    return (
        json.dumps(
            {
                "v": version,
                "type": "request",
                "id": "request-1",
                "method": "assets.list",
                "params": {},
            }
        ).encode()
        + b"\n"
    )


def test_decode_frame_accepts_strict_request() -> None:
    assert decode_frame(_request()) == RequestFrame("request-1", "assets.list", {})


@pytest.mark.parametrize("version", [True, 1.0])
def test_decode_frame_requires_exact_integer_version(version: object) -> None:
    with pytest.raises(ProtocolError) as caught:
        decode_frame(_request(version))

    assert caught.value.code is ProtocolErrorCode.UNSUPPORTED_PROTOCOL


def test_decode_frame_rejects_duplicate_keys() -> None:
    raw = b'{"v":1,"type":"request","id":"a","id":"b","method":"x","params":{}}\n'

    with pytest.raises(ProtocolError) as caught:
        decode_frame(raw)

    assert caught.value.code is ProtocolErrorCode.INVALID_FRAME


def test_decode_frame_normalizes_json_integer_limit() -> None:
    raw = (
        b'{"v":1,"type":"request","id":"a","method":"x","params":{"n":'
        + b"9" * 5_000
        + b"}}\n"
    )

    with pytest.raises(ProtocolError) as caught:
        decode_frame(raw)

    assert caught.value.code is ProtocolErrorCode.INVALID_FRAME


def test_decode_frame_rejects_unpaired_unicode_surrogate() -> None:
    raw = b'{"v":1,"type":"request","id":"a","method":"x","params":{"x":"\\ud800"}}\n'

    with pytest.raises(ProtocolError) as caught:
        decode_frame(raw)

    assert caught.value.code is ProtocolErrorCode.INVALID_FRAME


def test_encode_response_replaces_invalid_unicode_with_safe_error() -> None:
    encoded = encode_response("a", result={"value": "\ud800"})
    document = json.loads(encoded)

    assert document["error"]["code"] == "internal"
    assert "result" not in document


def test_encode_response_replaces_oversized_result() -> None:
    encoded = encode_response("a", result="x" * MAX_RESPONSE_FRAME_BYTES)
    document = json.loads(encoded)

    assert len(encoded) < 1024
    assert document["error"]["code"] == "response_too_large"
    # Retrying the same read returns the same oversized result.
    assert document["error"]["retryable"] is False


def test_response_too_large_is_never_retryable_for_any_limit() -> None:
    limits = JsonLimits(values=1_024, depth=8)
    by_bytes = json.loads(encode_response("a", result="x" * MAX_RESPONSE_FRAME_BYTES))
    by_values = json.loads(encode_response("b", result=[0] * 2_000, limits=limits))
    deep: object = "leaf"
    for _ in range(10):
        deep = [deep]
    by_depth = json.loads(encode_response("c", result=deep, limits=limits))

    for document in (by_bytes, by_values, by_depth):
        assert document["error"]["code"] == "response_too_large"
        assert document["error"]["retryable"] is False


def test_encode_event_enforces_the_negotiated_value_and_depth_limits() -> None:
    limits = JsonLimits(values=1_024, depth=8)
    fits = json.loads(encode_event(1, "service.changed", [0] * 100, limits=limits))
    assert fits["data"] == [0] * 100

    with pytest.raises(ProtocolError) as too_many:
        encode_event(2, "service.changed", [0] * 1_024, limits=limits)
    assert too_many.value.code is ProtocolErrorCode.EVENT_OVERFLOW

    deep: object = "leaf"
    for _ in range(8):
        deep = [deep]
    with pytest.raises(ProtocolError) as too_deep:
        encode_event(3, "service.changed", deep, limits=limits)
    assert too_deep.value.code is ProtocolErrorCode.EVENT_OVERFLOW
