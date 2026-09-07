"""Strict bounded JSON decoding helpers for artifact manifests."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Never

from tongs.desktop.artifact_contract.models import (
    ArtifactContractError,
    ArtifactContractErrorCode,
)

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_VALUES = 20_000
MAX_JSON_STRING_BYTES = 256 * 1024
MAX_JSON_KEY_BYTES = 256
MAX_JSON_INTEGER = 2**63 - 1

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def decode_json_document(document: bytes) -> JsonObject:
    """Decode one bounded UTF-8 JSON object with duplicate-key rejection."""
    if not isinstance(document, bytes):
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_JSON,
            "Manifest document must be bytes",
        )
    if not document or len(document) > MAX_DOCUMENT_BYTES:
        raise ArtifactContractError(
            ArtifactContractErrorCode.LIMIT_EXCEEDED,
            "Manifest document size is outside supported bounds",
        )
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_JSON,
            "Manifest document is not valid UTF-8",
        ) from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_finite,
        )
    except ArtifactContractError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_JSON,
            "Manifest document is not valid JSON",
        ) from error
    if not isinstance(value, dict):
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_JSON,
            "Manifest root must be an object",
        )
    _validate_json_bounds(value)
    return value


def _object_without_duplicates(
    pairs: list[tuple[str, JsonValue]],
) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactContractError(
                ArtifactContractErrorCode.DUPLICATE_VALUE,
                "Manifest contains a duplicate object key",
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Never:
    raise ArtifactContractError(
        ArtifactContractErrorCode.INVALID_JSON,
        "Manifest contains a non-finite number",
    )


def _validate_json_bounds(value: JsonValue) -> None:
    remaining = [MAX_JSON_VALUES]

    def visit(item: JsonValue, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ArtifactContractError(
                ArtifactContractErrorCode.LIMIT_EXCEEDED,
                "Manifest nesting exceeds the supported limit",
            )
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ArtifactContractError(
                ArtifactContractErrorCode.LIMIT_EXCEEDED,
                "Manifest value count exceeds the supported limit",
            )
        if isinstance(item, str):
            if _utf8_length(item) > MAX_JSON_STRING_BYTES:
                raise ArtifactContractError(
                    ArtifactContractErrorCode.LIMIT_EXCEEDED,
                    "Manifest string exceeds the supported limit",
                )
            return
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, int):
            if abs(item) > MAX_JSON_INTEGER:
                raise ArtifactContractError(
                    ArtifactContractErrorCode.LIMIT_EXCEEDED,
                    "Manifest integer exceeds the supported limit",
                )
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ArtifactContractError(
                    ArtifactContractErrorCode.INVALID_JSON,
                    "Manifest contains a non-finite number",
                )
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        for key, child in item.items():
            if _utf8_length(key) > MAX_JSON_KEY_BYTES:
                raise ArtifactContractError(
                    ArtifactContractErrorCode.LIMIT_EXCEEDED,
                    "Manifest object key exceeds the supported limit",
                )
            visit(child, depth + 1)

    visit(value, 0)


def require_object(value: JsonValue, label: str) -> JsonObject:
    if not isinstance(value, dict):
        _invalid(label)
    return value


def require_array(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        _invalid(label)
    return value


def require_string(value: JsonValue, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value:
        _invalid(label)
    if _utf8_length(value) > max_bytes or any(ord(char) < 32 for char in value):
        _invalid(label)
    return value


def require_bool(value: JsonValue, label: str) -> bool:
    if not isinstance(value, bool):
        _invalid(label)
    return value


def require_integer(
    value: JsonValue,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(label)
    return value


def require_exact_keys(
    value: JsonObject,
    required: frozenset[str],
    label: str,
) -> None:
    if value.keys() != required:
        _invalid(label)


def require_enum[EnumT](
    value: JsonValue,
    enum_type: Callable[[str], EnumT],
    label: str,
) -> EnumT:
    text = require_string(value, label, max_bytes=64)
    try:
        return enum_type(text)
    except ValueError as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_FIELD,
            f"Manifest {label} is unsupported",
        ) from error


def _invalid(label: str) -> Never:
    raise ArtifactContractError(
        ArtifactContractErrorCode.INVALID_FIELD,
        f"Manifest {label} is invalid",
    )


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_JSON,
            "Manifest contains an invalid Unicode string",
        ) from error
