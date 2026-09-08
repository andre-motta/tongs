"""Validate bounded desktop gate receipts and bind their files safely.

This module validates receipt structure, consumer supplied source/check identity,
and the existence and digest of files staged beside a receipt.  It deliberately
does not parse report outcomes and never treats a producer supplied ``result``
as proof of a production check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

MAX_RECEIPT_BYTES = 256 * 1024
MAX_JSON_REPORT_BYTES = 128 * 1024
MAX_ARRAY_ENTRIES = 256
MAX_STRING_BYTES = 512
MAX_JSON_DEPTH = 16
MAX_JSON_VALUES = 4096
MAX_INTEGER = 2**63 - 1
HASH_CHUNK_BYTES = 1024 * 1024

PYTEST_JUNIT_FORMAT = "pytest-junit"
NODE_TAP_FORMAT = "node-tap"
ARTIFACT_LIFECYCLE_FORMAT = "artifact-lifecycle-v1"
INITIAL_REPORT_FORMATS = frozenset(
    {PYTEST_JUNIT_FORMAT, NODE_TAP_FORMAT, ARTIFACT_LIFECYCLE_FORMAT}
)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^[0-9]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_PROVENANCES = frozenset({"hosted", "local", "controlled-fixture"})


class ReceiptValidationError(ValueError):
    """Raised when a receipt or one of its staged files is not trustworthy."""


@dataclass(frozen=True, slots=True)
class ReceiptPolicy:
    """Consumer supplied identity, required checks and report formats.

    ``report_formats`` must contain one nonempty allowed-format collection for
    every required check ID.  This policy is supplied by the consumer and is
    never read from the receipt itself.
    """

    expected_commit: str
    expected_tree: str
    required_check_ids: Collection[str]
    report_formats: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        _validate_sha1(self.expected_commit, "expected commit")
        _validate_sha1(self.expected_tree, "expected tree")
        check_ids = tuple(self.required_check_ids)
        if not check_ids:
            raise ReceiptValidationError("required check IDs must not be empty")
        for check_id in check_ids:
            _validate_text(check_id, "required check ID")
        if len(check_ids) != len(set(check_ids)):
            raise ReceiptValidationError(
                "required check IDs must not contain duplicates"
            )
        object.__setattr__(self, "required_check_ids", frozenset(check_ids))
        configured_ids = frozenset(self.report_formats)
        if configured_ids != self.required_check_ids:
            raise ReceiptValidationError(
                "report formats must be configured for exactly the required check IDs"
            )
        for check_id, formats in self.report_formats.items():
            if not formats:
                raise ReceiptValidationError(
                    f"report formats for {check_id!r} must not be empty"
                )
            for report_format in formats:
                _validate_text(report_format, "report format")
                if report_format not in INITIAL_REPORT_FORMATS:
                    raise ReceiptValidationError(
                        f"report format {report_format!r} is unsupported"
                    )


@dataclass(frozen=True, slots=True)
class BoundFile:
    """A staged file whose size and digest matched the receipt."""

    path: str
    size: int
    sha256: str
    kind: str


@dataclass(frozen=True, slots=True)
class ReceiptValidation:
    """Structural and file-binding result, without report outcome claims."""

    receipt: Mapping[str, Any]
    bound_files: tuple[BoundFile, ...]


def validate_receipt(
    receipt_bytes: bytes,
    *,
    evidence_root: Path,
    policy: ReceiptPolicy,
) -> ReceiptValidation:
    """Validate a receipt and bind every referenced staged file.

    The caller supplies the evidence root and policy independently.  This
    function validates structure and bytes only.  It does not parse JUnit, TAP,
    or lifecycle report outcomes, and a valid return value is not production
    success.
    """

    document = _decode_receipt(receipt_bytes)
    parsed = _validate_structure(document, policy)
    root = _prepare_evidence_root(evidence_root)
    bound_files: list[BoundFile] = []
    for kind, entries in parsed:
        for entry in entries:
            path = entry["path"]
            expected_size = entry.get("size")
            expected_hash = entry["sha256"]
            if (
                kind == "report"
                and entry["format"] == ARTIFACT_LIFECYCLE_FORMAT
                and expected_size > MAX_JSON_REPORT_BYTES
            ):
                raise ReceiptValidationError(
                    f"JSON report {path!r} exceeds its bounded size"
                )
            observed_size, observed_hash = _verify_staged_file(
                root,
                path,
                expected_size=expected_size,
                expected_sha256=expected_hash,
            )
            bound_files.append(BoundFile(path, observed_size, observed_hash, kind))
    return ReceiptValidation(document, tuple(bound_files))


def validate_receipt_file(
    receipt_path: Path,
    *,
    evidence_root: Path,
    policy: ReceiptPolicy,
) -> ReceiptValidation:
    """Read a receipt from the evidence root and validate it safely."""

    root = _prepare_evidence_root(evidence_root)
    root_absolute = Path(os.path.abspath(root))
    receipt_absolute = Path(os.path.abspath(receipt_path))
    try:
        relative = receipt_absolute.relative_to(root_absolute).as_posix()
    except ValueError as error:
        raise ReceiptValidationError(
            "receipt must be located below the staged evidence root"
        ) from error
    _validate_relative_path(relative, "receipt path")
    receipt_bytes = _read_staged_bytes(root, relative, MAX_RECEIPT_BYTES)
    return validate_receipt(receipt_bytes, evidence_root=root, policy=policy)


def _decode_receipt(receipt_bytes: bytes) -> dict[str, Any]:
    if not isinstance(receipt_bytes, bytes):
        raise ReceiptValidationError("receipt must be UTF-8 JSON bytes")
    if not receipt_bytes or len(receipt_bytes) > MAX_RECEIPT_BYTES:
        raise ReceiptValidationError("receipt size is outside the supported bound")
    try:
        text = receipt_bytes.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except ReceiptValidationError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise ReceiptValidationError("receipt is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ReceiptValidationError("receipt root must be an object")
    _validate_json_bounds(value)
    return value


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptValidationError("receipt contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ReceiptValidationError(f"receipt contains non-finite number {value!r}")


def _validate_json_bounds(value: Any) -> None:
    remaining = MAX_JSON_VALUES

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise ReceiptValidationError("receipt JSON exceeds bounded limits")
        if isinstance(item, str):
            _validate_text(item, "receipt string")
        elif type(item) is int:
            if abs(item) > MAX_INTEGER:
                raise ReceiptValidationError("receipt integer exceeds bounded limits")
        elif isinstance(item, bool) or item is None:
            return
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ReceiptValidationError("receipt contains a non-finite number")
        elif isinstance(item, list):
            if len(item) > MAX_ARRAY_ENTRIES:
                raise ReceiptValidationError("receipt array exceeds bounded limits")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                _validate_text(key, "receipt object key")
                visit(child, depth + 1)
        else:
            raise ReceiptValidationError("receipt contains an unsupported JSON value")

    visit(value, 0)


def _validate_structure(
    document: dict[str, Any], policy: ReceiptPolicy
) -> tuple[tuple[str, Sequence[dict[str, Any]]], ...]:
    _require_exact_keys(
        document,
        {
            "schema_version",
            "check_id",
            "source",
            "execution",
            "result",
            "reports",
            "artifacts",
            "inputs",
        },
        "receipt",
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ReceiptValidationError("schema_version must be integer 1")
    check_id = _require_text(document["check_id"], "check_id")
    if check_id not in policy.required_check_ids:
        raise ReceiptValidationError(f"check_id {check_id!r} is not required")

    source = _require_object(document["source"], "source")
    _require_exact_keys(source, {"commit", "tree"}, "source")
    commit = _require_text(source["commit"], "source.commit")
    tree = _require_text(source["tree"], "source.tree")
    _validate_sha1(commit, "source.commit")
    _validate_sha1(tree, "source.tree")
    if commit != policy.expected_commit or tree != policy.expected_tree:
        raise ReceiptValidationError(
            "receipt source identity does not match the consumer expectation"
        )

    execution = _require_object(document["execution"], "execution")
    _require_exact_keys(
        execution,
        {"repository", "run_id", "attempt", "environment", "provenance"},
        "execution",
    )
    _require_text(execution["repository"], "execution.repository")
    run_id = _require_text(execution["run_id"], "execution.run_id")
    if _DECIMAL_RE.fullmatch(run_id) is None:
        raise ReceiptValidationError("execution.run_id must be a decimal string")
    attempt = execution["attempt"]
    if type(attempt) is not int or attempt < 1 or attempt > MAX_INTEGER:
        raise ReceiptValidationError("execution.attempt must be a positive integer")
    _require_text(execution["environment"], "execution.environment")
    provenance = _require_text(execution["provenance"], "execution.provenance")
    if provenance not in _PROVENANCES:
        raise ReceiptValidationError("execution.provenance is unsupported")

    result = _require_text(document["result"], "result")
    if result not in {"success", "failure"}:
        raise ReceiptValidationError("result must be success or failure")

    reports = _require_entries(document["reports"], "reports", nonempty=True)
    artifacts = _require_entries(document["artifacts"], "artifacts", nonempty=False)
    inputs = _require_entries(document["inputs"], "inputs", nonempty=False)
    if len(reports) + len(artifacts) + len(inputs) > MAX_ARRAY_ENTRIES:
        raise ReceiptValidationError("combined receipt entries exceed bounded limits")
    paths: set[str] = set()
    normalized_paths: set[str] = set()
    for entry in reports:
        _validate_report_entry(
            entry, policy.report_formats[check_id], paths, normalized_paths
        )
    for entry in artifacts:
        _validate_artifact_entry(entry, paths, normalized_paths)
    for entry in inputs:
        _validate_input_entry(entry, paths, normalized_paths)
    return (("report", reports), ("artifact", artifacts), ("input", inputs))


def _validate_report_entry(
    entry: dict[str, Any],
    allowed_formats: Collection[str],
    paths: set[str],
    normalized_paths: set[str],
) -> None:
    _require_exact_keys(entry, {"path", "size", "sha256", "format"}, "report entry")
    path = _validate_entry_path(entry["path"], paths, normalized_paths)
    _validate_nonzero_size(entry["size"], f"report {path} size")
    _validate_sha256(entry["sha256"], f"report {path} sha256")
    report_format = _require_text(entry["format"], f"report {path} format")
    if report_format not in allowed_formats:
        raise ReceiptValidationError(f"report {path!r} has an unconfigured format")


def _validate_artifact_entry(
    entry: dict[str, Any], paths: set[str], normalized_paths: set[str]
) -> None:
    _require_exact_keys(entry, {"path", "size", "sha256", "role"}, "artifact entry")
    path = _validate_entry_path(entry["path"], paths, normalized_paths)
    _validate_size(entry["size"], f"artifact {path} size")
    _validate_sha256(entry["sha256"], f"artifact {path} sha256")
    _require_text(entry["role"], f"artifact {path} role")


def _validate_input_entry(
    entry: dict[str, Any], paths: set[str], normalized_paths: set[str]
) -> None:
    _require_exact_keys(entry, {"path", "sha256"}, "input entry")
    path = _validate_entry_path(entry["path"], paths, normalized_paths)
    _validate_sha256(entry["sha256"], f"input {path} sha256")


def _validate_entry_path(
    value: Any, paths: set[str], normalized_paths: set[str]
) -> str:
    path = _require_text(value, "entry path", max_bytes=MAX_STRING_BYTES)
    _validate_relative_path(path, "entry path")
    normalized = unicodedata.normalize("NFC", path).casefold()
    if path in paths or normalized in normalized_paths:
        raise ReceiptValidationError(f"receipt contains duplicate path {path!r}")
    paths.add(path)
    normalized_paths.add(normalized)
    return path


def _validate_relative_path(value: str, label: str) -> None:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or _DRIVE_RE.match(value) is not None
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ReceiptValidationError(f"{label} must be a safe relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReceiptValidationError(f"{label} must not contain traversal components")


def _require_entries(value: Any, label: str, *, nonempty: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReceiptValidationError(f"{label} must be an array")
    if len(value) > MAX_ARRAY_ENTRIES or (nonempty and not value):
        raise ReceiptValidationError(f"{label} has an invalid bounded length")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        entries.append(_require_object(item, f"{label}[{index}]"))
    return entries


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptValidationError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise ReceiptValidationError(f"{label} has unexpected or missing fields")


def _require_text(value: Any, label: str, *, max_bytes: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptValidationError(f"{label} must be a nonempty string")
    _validate_text(value, label, max_bytes=max_bytes)
    return value


def _validate_text(
    value: str, label: str, *, max_bytes: int = MAX_STRING_BYTES
) -> None:
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ReceiptValidationError(f"{label} is not valid UTF-8") from error
    if size > max_bytes or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ReceiptValidationError(f"{label} exceeds bounded string rules")


def _validate_size(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_INTEGER:
        raise ReceiptValidationError(f"{label} must be a nonnegative integer")
    return value


def _validate_nonzero_size(value: Any, label: str) -> int:
    size = _validate_size(value, label)
    if size == 0:
        raise ReceiptValidationError(f"{label} must be positive")
    return size


def _validate_sha1(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA1_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} must be a lowercase full SHA-1")


def _validate_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} must be a lowercase SHA-256")


def _prepare_evidence_root(evidence_root: Path) -> Path:
    root = Path(evidence_root)
    root_stat = _lstat_root(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ReceiptValidationError("staged evidence root must be a real directory")
    return root


def _open_bound_file(
    root: Path, relative_path: str
) -> tuple[int, os.stat_result, os.stat_result]:
    """Open a path beneath root with no-follow checks for every component."""

    root_stat = _lstat_root(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ReceiptValidationError(
            "staged evidence root changed or is not a directory"
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as error:
        raise ReceiptValidationError(
            "unable to open staged evidence root safely"
        ) from error
    try:
        opened_root_stat = os.fstat(directory_fd)
        _same_identity(root_stat, opened_root_stat, "staged evidence root was replaced")
        parts = relative_path.split("/")
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except OSError as error:
                raise ReceiptValidationError(
                    f"unable to open directory for staged file {relative_path!r}"
                ) from error
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        except OSError as error:
            raise ReceiptValidationError(
                f"unable to open staged file {relative_path!r} safely"
            ) from error
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise ReceiptValidationError(
                f"staged path {relative_path!r} is not a regular file"
            )
        return file_fd, file_stat, root_stat
    finally:
        os.close(directory_fd)


def _verify_staged_file(
    root: Path,
    relative_path: str,
    *,
    expected_size: int | None,
    expected_sha256: str,
) -> tuple[int, str]:
    first_size, first_digest, first_stat, first_root_stat = _read_staged_file(
        root, relative_path, expected_size
    )
    second_size, second_digest, second_stat, second_root_stat = _read_staged_file(
        root, relative_path, expected_size
    )
    _same_identity(
        first_root_stat, second_root_stat, "staged evidence root was replaced"
    )
    _same_identity(
        first_stat, second_stat, f"staged file {relative_path!r} was replaced"
    )
    if first_size != second_size or first_digest != second_digest:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} changed during verification"
        )
    if expected_size is not None and first_size != expected_size:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} size does not match the receipt"
        )
    if first_digest != expected_sha256:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} SHA-256 does not match the receipt"
        )
    return first_size, first_digest


def _read_staged_file(
    root: Path, relative_path: str, expected_size: int | None
) -> tuple[int, str, os.stat_result, os.stat_result]:
    file_fd, before_stat, root_stat = _open_bound_file(root, relative_path)
    hasher = hashlib.sha256()
    observed_size = 0
    try:
        with os.fdopen(file_fd, "rb", closefd=True) as stream:
            while chunk := stream.read(HASH_CHUNK_BYTES):
                observed_size += len(chunk)
                hasher.update(chunk)
                if expected_size is not None and observed_size > expected_size:
                    raise ReceiptValidationError(
                        f"staged file {relative_path!r} exceeds its declared size"
                    )
            after_stat = os.fstat(stream.fileno())
    except ReceiptValidationError:
        raise
    except OSError as error:
        raise ReceiptValidationError(
            f"unable to read staged file {relative_path!r}"
        ) from error
    _same_identity(
        before_stat, after_stat, f"staged file {relative_path!r} changed during read"
    )
    if after_stat.st_size != observed_size:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} changed during read"
        )
    _same_identity(root_stat, _lstat_root(root), "staged evidence root was replaced")
    return observed_size, hasher.hexdigest(), after_stat, root_stat


def _read_staged_bytes(root: Path, relative_path: str, maximum: int) -> bytes:
    first = _read_staged_bytes_once(root, relative_path, maximum)
    second = _read_staged_bytes_once(root, relative_path, maximum)
    if first != second:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} changed during verification"
        )
    return first


def _read_staged_bytes_once(root: Path, relative_path: str, maximum: int) -> bytes:
    file_fd, before_stat, root_stat = _open_bound_file(root, relative_path)
    chunks: list[bytes] = []
    observed_size = 0
    try:
        with os.fdopen(file_fd, "rb", closefd=True) as stream:
            while chunk := stream.read(HASH_CHUNK_BYTES):
                observed_size += len(chunk)
                if observed_size > maximum:
                    raise ReceiptValidationError(
                        "receipt exceeds the supported size bound"
                    )
                chunks.append(chunk)
            after_stat = os.fstat(stream.fileno())
    except ReceiptValidationError:
        raise
    except OSError as error:
        raise ReceiptValidationError(
            f"unable to read staged file {relative_path!r}"
        ) from error
    _same_identity(
        before_stat, after_stat, f"staged file {relative_path!r} changed during read"
    )
    if after_stat.st_size != observed_size:
        raise ReceiptValidationError(
            f"staged file {relative_path!r} changed during read"
        )
    _same_identity(root_stat, _lstat_root(root), "staged evidence root was replaced")
    return b"".join(chunks)


def _lstat_root(root: Path) -> os.stat_result:
    try:
        return root.lstat()
    except OSError as error:
        raise ReceiptValidationError("staged evidence root is unavailable") from error


def _same_identity(first: os.stat_result, second: os.stat_result, message: str) -> None:
    if (
        first.st_dev != second.st_dev
        or first.st_ino != second.st_ino
        or first.st_mode != second.st_mode
        or first.st_size != second.st_size
        or first.st_mtime_ns != second.st_mtime_ns
        or first.st_ctime_ns != second.st_ctime_ns
    ):
        raise ReceiptValidationError(message)


def _parse_policy(arguments: argparse.Namespace) -> ReceiptPolicy:
    formats: dict[str, frozenset[str]] = {}
    for item in arguments.format:
        try:
            check_id, report_format = item.split("=", 1)
        except ValueError as error:
            raise ReceiptValidationError("--format must be CHECK_ID=FORMAT") from error
        if check_id in formats:
            formats[check_id] = formats[check_id] | frozenset({report_format})
        else:
            formats[check_id] = frozenset({report_format})
    return ReceiptPolicy(
        expected_commit=arguments.commit,
        expected_tree=arguments.tree,
        required_check_ids=frozenset(arguments.check_id),
        report_formats=formats,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate desktop receipt structure and staged file bindings. "
            "This does not establish production success."
        )
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--check-id", action="append", required=True)
    parser.add_argument(
        "--format",
        action="append",
        required=True,
        metavar="CHECK_ID=FORMAT",
        help="Allowed report format for a configured check; repeat as needed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the structural/binding validator as a diagnostic command."""

    arguments = _build_parser().parse_args(argv)
    try:
        policy = _parse_policy(arguments)
        validation = validate_receipt_file(
            arguments.receipt,
            evidence_root=arguments.evidence_root,
            policy=policy,
        )
    except ReceiptValidationError as error:
        print(f"receipt validation failed: {error}", file=sys.stderr)
        return 1
    print(
        "receipt structure and staged file bindings are valid; "
        "report outcomes and production success require independent checks "
        f"({len(validation.bound_files)} files bound)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
