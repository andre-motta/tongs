"""Publish one issue #110 receipt for a production stage that has no adapter.

Issues #135 and #139 own bespoke evidence adapters for the archive SBOM and the
reproducible archive.  The remaining production stages consume already reviewed
producers whose outputs are ordinary test reports and small JSON records, so
they need a receipt and an ``artifact-lifecycle-v1`` report rather than a second
validation framework.  This module is that thin binder and nothing more.

Four properties keep it honest:

* The checkout is admitted before anything else.  ``HEAD``, its tree and a
  clean tracked status are compared with the caller-owned commit and tree, so a
  stage cannot claim a source identity the working tree does not have.
* Every declared stage must name at least one staged file, and the emitter
  records that file's observed size and digest inside the stage.  A stage
  cannot be asserted without bytes behind it.
* Every staged test report is parsed with the issue #115 semantic parser for
  its declared format *before* the receipt is written, so a failing TAP report
  or a skipped pytest report cannot produce a ``success`` receipt.
* Declared byte equalities between staged files are checked here and recorded,
  which is how a producer's intended lifecycle equalities become receipt-bound
  observations rather than a generic success flag.

What it deliberately does not do: it does not run producers, re-derive producer
semantics, or choose policy.  The plan is written by the workflow step that
already ran the real command, the check identity comes from the caller, and the
aggregate independently re-binds and re-parses everything published here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
RECEIPT_READER_PROGRAM = ".github/scripts/verify_desktop_production.py"
REPORT_PARSER_PROGRAM = ".github/scripts/desktop_test_reports.py"
EMITTER_PROGRAM = "tests/ci/desktop_stage_receipt.py"

MAX_PLAN_BYTES = 256 * 1024
MAX_SMALL_FILE_BYTES = 8 * 1024 * 1024
MAX_STAGES = 32
MAX_FILES = 64
MAX_EQUALITIES = 32
MAX_LARGE_ARTIFACTS = 32

PYTEST_JUNIT = "pytest-junit"
NODE_TAP = "node-tap"
ARTIFACT_LIFECYCLE = "artifact-lifecycle-v1"
STAGED_REPORT_FORMATS = frozenset({PYTEST_JUNIT, NODE_TAP})


class StageReceiptError(ValueError):
    """Raised when a stage plan or its staged bytes are not acceptable."""


def _fail(message: str) -> NoReturn:
    raise StageReceiptError(message)


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        _fail(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


RECEIPTS = _load_module("desktop_stage_receipts", ROOT / RECEIPT_READER_PROGRAM)
REPORTS = _load_module("desktop_stage_reports", ROOT / REPORT_PARSER_PROGRAM)


@dataclass(frozen=True, slots=True)
class StageReceiptBinding:
    """Small immutable identity of one published stage receipt."""

    check_id: str
    receipt_path: str
    receipt_sha256: str
    report_path: str
    report_sha256: str
    stage_count: int
    file_count: int


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    """Read a bounded regular file, rejecting symlinks and mid-read changes."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"unable to open {label} safely: {error}")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            _fail(f"{label} must be a regular file")
        if first.st_size > maximum:
            _fail(f"{label} exceeds its bounded size")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            payload = handle.read(maximum + 1)
        second = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        _fail(f"{label} exceeds its bounded size")
    if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    ):
        _fail(f"{label} changed while it was read")
    return payload


def _decode_object(payload: bytes, label: str) -> dict[str, Any]:
    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            _fail(f"{label} contains a duplicate key")
        return dict(pairs)

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"{label} is not decodable JSON: {error}")
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unexpected = sorted(set(value) - expected)
        _fail(f"{label} key set mismatch: missing={missing}, unexpected={unexpected}")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _require_list(value: Any, label: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be a JSON array")
    if len(value) > maximum:
        _fail(f"{label} exceeds its bounded entry count")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        _fail(f"{label} must be bounded nonempty text")
    return value


def _require_relative_path(value: Any, label: str) -> str:
    path = _require_text(value, label)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        _fail(f"{label} must be a safe relative path")
    return path


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def _git(source_root: Path, *arguments: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LANG": "C",
    }
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            capture_output=True,
            check=False,
            env=environment,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(f"git {' '.join(arguments)} could not run: {error}")
    if completed.returncode != 0:
        _fail(f"git {' '.join(arguments)} failed")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def admit_source(source_root: Path, commit: str, tree: str) -> None:
    """Require a clean checkout whose HEAD equals the caller-owned identity."""

    if _git(source_root, "rev-parse", "HEAD") != commit:
        _fail("checked-out HEAD does not match the expected source commit")
    if _git(source_root, "rev-parse", "HEAD^{tree}") != tree:
        _fail("checked-out tree does not match the expected source tree")
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        _git(source_root, *arguments)


def _tool_identity() -> dict[str, str]:
    identity: dict[str, str] = {}
    for label, program in (
        ("emitter", EMITTER_PROGRAM),
        ("receipt_reader", RECEIPT_READER_PROGRAM),
        ("report_parser", REPORT_PARSER_PROGRAM),
    ):
        payload = _read_regular_bytes(ROOT / program, MAX_SMALL_FILE_BYTES, program)
        identity[f"{label}_program"] = program
        identity[f"{label}_program_sha256"] = _digest(payload)
    return identity


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    kind: str
    path: str
    source: Path
    report_format: str | None
    role: str | None


def _parse_plan(plan: Mapping[str, Any], check_id: str) -> dict[str, Any]:
    _require_exact_keys(
        plan,
        {
            "check_id",
            "report_path",
            "scope",
            "files",
            "stages",
            "equalities",
            "large_artifacts",
        },
        "stage plan",
    )
    if _require_text(plan["check_id"], "plan check_id") != check_id:
        _fail("stage plan check_id does not match the caller-owned check identity")
    report_path = _require_relative_path(plan["report_path"], "plan report_path")

    scope = _require_object(plan["scope"], "plan scope")
    _require_exact_keys(scope, {"covered", "excluded"}, "plan scope")
    _require_text(scope["covered"], "scope covered")
    excluded = _require_list(scope["excluded"], "scope excluded", 32)
    if not excluded:
        _fail("a stage must state what its scope excludes")
    for item in excluded:
        _require_text(item, "scope exclusion")

    files: list[_PlannedFile] = []
    seen_paths: set[str] = set()
    for entry in _require_list(plan["files"], "plan files", MAX_FILES):
        entry = _require_object(entry, "plan file")
        kind = _require_text(entry.get("kind"), "plan file kind")
        if kind == "report":
            _require_exact_keys(entry, {"kind", "path", "source", "format"}, "report")
            report_format = _require_text(entry["format"], "report format")
            if report_format not in STAGED_REPORT_FORMATS:
                _fail(
                    "a staged report must be a pytest JUnit or Node TAP report; "
                    "the lifecycle report is generated here"
                )
            role = None
        elif kind == "artifact":
            _require_exact_keys(entry, {"kind", "path", "source", "role"}, "artifact")
            report_format = None
            role = _require_text(entry["role"], "artifact role")
        elif kind == "input":
            _require_exact_keys(entry, {"kind", "path", "source"}, "input")
            report_format = None
            role = None
        else:
            _fail(f"plan file kind {kind!r} is unsupported")
        path = _require_relative_path(entry["path"], "plan file path")
        if path == report_path:
            _fail("a planned file must not collide with the lifecycle report")
        if path in seen_paths:
            _fail(f"stage plan repeats the staged path {path!r}")
        seen_paths.add(path)
        source = Path(_require_text(entry["source"], "plan file source"))
        if not source.is_absolute():
            _fail("plan file source must be an absolute path")
        files.append(_PlannedFile(kind, path, source, report_format, role))
    if not files:
        _fail("a stage must stage at least one file")

    stages = _require_list(plan["stages"], "plan stages", MAX_STAGES)
    if not stages:
        _fail("a stage plan must declare at least one stage")
    stage_names: list[str] = []
    for entry in stages:
        entry = _require_object(entry, "plan stage")
        _require_exact_keys(entry, {"name", "evidence", "observation"}, "plan stage")
        name = _require_text(entry["name"], "stage name")
        if name in stage_names:
            _fail(f"stage plan repeats the stage name {name!r}")
        stage_names.append(name)
        evidence = _require_list(entry["evidence"], "stage evidence", MAX_FILES)
        if not evidence:
            _fail(f"stage {name!r} names no staged evidence")
        for item in evidence:
            reference = _require_relative_path(item, "stage evidence path")
            if reference not in seen_paths:
                _fail(f"stage {name!r} names unstaged evidence {reference!r}")
        _require_object(entry["observation"], "stage observation")

    equalities = _require_list(plan["equalities"], "plan equalities", MAX_EQUALITIES)
    for entry in equalities:
        entry = _require_object(entry, "plan equality")
        _require_exact_keys(entry, {"left", "right", "reason"}, "plan equality")
        for side in ("left", "right"):
            reference = _require_relative_path(entry[side], f"equality {side}")
            if reference not in seen_paths:
                _fail(f"equality names unstaged file {reference!r}")
        _require_text(entry["reason"], "equality reason")

    large = _require_list(
        plan["large_artifacts"], "plan large_artifacts", MAX_LARGE_ARTIFACTS
    )
    for entry in large:
        entry = _require_object(entry, "large artifact")
        _require_exact_keys(
            entry, {"name", "workflow_artifact", "size", "sha256"}, "large artifact"
        )
        _require_text(entry["name"], "large artifact name")
        _require_text(entry["workflow_artifact"], "large artifact workflow name")
        if type(entry["size"]) is not int or entry["size"] <= 0:
            _fail("large artifact size must be a positive integer")
        digest = _require_text(entry["sha256"], "large artifact sha256")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            _fail("large artifact sha256 must be lowercase hexadecimal")

    return {
        "report_path": report_path,
        "scope": {"covered": scope["covered"], "excluded": list(excluded)},
        "files": files,
        "stages": stages,
        "equalities": equalities,
        "large_artifacts": large,
    }


def _stage_files(
    files: Sequence[_PlannedFile], destination: Path
) -> dict[str, dict[str, Any]]:
    staged: dict[str, dict[str, Any]] = {}
    for planned in files:
        payload = _read_regular_bytes(
            planned.source, MAX_SMALL_FILE_BYTES, f"staged input {planned.path}"
        )
        _write_exclusive(destination / planned.path, payload)
        staged[planned.path] = {
            "kind": planned.kind,
            "size": len(payload),
            "sha256": _digest(payload),
            "format": planned.report_format,
            "role": planned.role,
            "bytes": payload,
        }
    return staged


def _verify_staged_reports(staged: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Parse every staged test report and record its observed passing counts."""

    outcomes: dict[str, Any] = {}
    for path, record in sorted(staged.items()):
        if record["kind"] != "report":
            continue
        try:
            if record["format"] == PYTEST_JUNIT:
                counts = REPORTS.verify_pytest_junit(record["bytes"])
                outcomes[path] = {
                    "format": PYTEST_JUNIT,
                    "tests": counts.tests,
                    "suites": counts.suites,
                }
            else:
                counts = REPORTS.verify_node_tap(record["bytes"])
                outcomes[path] = {
                    "format": NODE_TAP,
                    "tests": counts.tests,
                    "suites": counts.suites,
                    "passed": counts.passed,
                }
        except REPORTS.ReportValidationError as error:
            _fail(f"staged report {path!r} did not pass: {error}")
    return outcomes


def _verify_equalities(
    equalities: Sequence[Mapping[str, Any]], staged: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    for entry in equalities:
        left, right = entry["left"], entry["right"]
        if staged[left]["bytes"] != staged[right]["bytes"]:
            _fail(
                f"declared equality failed: {left!r} and {right!r} differ "
                f"({entry['reason']})"
            )
        recorded.append(
            {
                "left": left,
                "right": right,
                "reason": entry["reason"],
                "sha256": staged[left]["sha256"],
            }
        )
    return sorted(recorded, key=lambda item: (item["left"], item["right"]))


def _build_report(
    *,
    check_id: str,
    commit: str,
    tree: str,
    parsed: Mapping[str, Any],
    staged: Mapping[str, dict[str, Any]],
    outcomes: Mapping[str, Any],
    equalities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stages = []
    for entry in parsed["stages"]:
        evidence = [
            {
                "path": path,
                "size": staged[path]["size"],
                "sha256": staged[path]["sha256"],
            }
            for path in sorted(entry["evidence"])
        ]
        stages.append(
            {
                "name": entry["name"],
                "result": "pass",
                "evidence": evidence,
                "observation": entry["observation"],
            }
        )
    return {
        "schema_version": 1,
        "check_id": check_id,
        "result": "pass",
        "source": {"commit": commit, "tree": tree},
        "tool": _tool_identity(),
        "stages": stages,
        "report_outcomes": dict(outcomes),
        "equalities": list(equalities),
        "large_artifacts": sorted(
            (dict(entry) for entry in parsed["large_artifacts"]),
            key=lambda item: item["name"],
        ),
        "scope": parsed["scope"],
    }


def _build_receipt(
    *,
    policy: Any,
    report_path: str,
    report_bytes: bytes,
    staged: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    reports = [
        {
            "path": report_path,
            "size": len(report_bytes),
            "sha256": _digest(report_bytes),
            "format": ARTIFACT_LIFECYCLE,
        }
    ]
    artifacts = []
    inputs = []
    for path, record in sorted(staged.items()):
        if record["kind"] == "report":
            reports.append(
                {
                    "path": path,
                    "size": record["size"],
                    "sha256": record["sha256"],
                    "format": record["format"],
                }
            )
        elif record["kind"] == "artifact":
            artifacts.append(
                {
                    "path": path,
                    "size": record["size"],
                    "sha256": record["sha256"],
                    "role": record["role"],
                }
            )
        else:
            inputs.append({"path": path, "sha256": record["sha256"]})
    return {
        "schema_version": 1,
        "check_id": policy.expected_check_id,
        "source": {
            "commit": policy.expected_commit,
            "tree": policy.expected_tree,
        },
        "execution": {
            "repository": policy.expected_repository,
            "run_id": policy.expected_run_id,
            "attempt": policy.expected_attempt,
            "environment": policy.expected_environment,
            "provenance": policy.expected_provenance,
        },
        "result": "success",
        "reports": reports,
        "artifacts": artifacts,
        "inputs": inputs,
    }


def publish_stage_receipt(
    *,
    source_root: Path,
    plan_path: Path,
    output_root: Path,
    receipt_name: str,
    receipt_policy: Any,
) -> StageReceiptBinding:
    """Stage one production check's evidence and publish it atomically."""

    if not isinstance(receipt_policy, RECEIPTS.ReceiptPolicy):
        _fail("receipt policy must use the existing validated policy type")
    if ARTIFACT_LIFECYCLE not in receipt_policy.allowed_report_formats:
        _fail("the stage policy must allow the generated lifecycle report format")
    _require_relative_path(receipt_name, "receipt name")
    destination = Path(output_root)
    if destination.exists() or destination.is_symlink():
        _fail("output root must not already exist")

    admit_source(
        Path(source_root), receipt_policy.expected_commit, receipt_policy.expected_tree
    )

    plan = _decode_object(
        _read_regular_bytes(Path(plan_path), MAX_PLAN_BYTES, "stage plan"),
        "stage plan",
    )
    parsed = _parse_plan(plan, receipt_policy.expected_check_id)
    for planned in parsed["files"]:
        if (
            planned.report_format is not None
            and planned.report_format not in receipt_policy.allowed_report_formats
        ):
            _fail(
                f"the stage policy does not allow the staged report format "
                f"{planned.report_format!r}"
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    os.chmod(temporary, 0o700)
    try:
        staged = _stage_files(parsed["files"], temporary)
        outcomes = _verify_staged_reports(staged)
        equalities = _verify_equalities(parsed["equalities"], staged)
        report = _build_report(
            check_id=receipt_policy.expected_check_id,
            commit=receipt_policy.expected_commit,
            tree=receipt_policy.expected_tree,
            parsed=parsed,
            staged=staged,
            outcomes=outcomes,
            equalities=equalities,
        )
        report_bytes = _canonical_json(report)
        if len(report_bytes) > RECEIPTS.MAX_JSON_REPORT_BYTES:
            _fail("stage lifecycle report exceeds its bounded size")
        _write_exclusive(temporary / parsed["report_path"], report_bytes)

        receipt = _build_receipt(
            policy=receipt_policy,
            report_path=parsed["report_path"],
            report_bytes=report_bytes,
            staged=staged,
        )
        receipt_bytes = _canonical_json(receipt)
        _write_exclusive(temporary / receipt_name, receipt_bytes)

        binding = consume_stage_receipt(
            evidence_root=temporary,
            receipt_path=temporary / receipt_name,
            receipt_policy=receipt_policy,
            expected_report_path=parsed["report_path"],
        )
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return binding


def consume_stage_receipt(
    *,
    evidence_root: Path,
    receipt_path: Path,
    receipt_policy: Any,
    expected_report_path: str,
) -> StageReceiptBinding:
    """Validate a published stage receipt and its lifecycle report outcome."""

    if not isinstance(receipt_policy, RECEIPTS.ReceiptPolicy):
        _fail("receipt policy must use the existing validated policy type")
    root = Path(evidence_root)
    validation = RECEIPTS.validate_receipt_file(
        Path(receipt_path), evidence_root=root, policy=receipt_policy
    )
    if validation.receipt.get("result") != "success":
        _fail("published stage receipt does not record success")

    bound = {item.path: item for item in validation.bound_files}
    if expected_report_path not in bound:
        _fail("published receipt does not bind its lifecycle report")
    report_bytes = RECEIPTS.read_bound_bytes(
        root, bound[expected_report_path], RECEIPTS.MAX_JSON_REPORT_BYTES
    )
    report = _decode_object(report_bytes, "stage lifecycle report")
    _require_exact_keys(
        report,
        {
            "schema_version",
            "check_id",
            "result",
            "source",
            "tool",
            "stages",
            "report_outcomes",
            "equalities",
            "large_artifacts",
            "scope",
        },
        "stage lifecycle report",
    )
    if report["schema_version"] != 1:
        _fail("stage lifecycle report schema is unsupported")
    if report["check_id"] != receipt_policy.expected_check_id:
        _fail("stage lifecycle report names another check")
    if report["result"] != "pass":
        _fail("stage lifecycle report does not record a pass")
    if report["source"] != {
        "commit": receipt_policy.expected_commit,
        "tree": receipt_policy.expected_tree,
    }:
        _fail("stage lifecycle report source does not match caller policy")
    if report["tool"] != _tool_identity():
        _fail("stage lifecycle report tool identity does not match this checkout")

    stages = report["stages"]
    if not isinstance(stages, list) or not stages:
        _fail("stage lifecycle report records no stages")
    names = []
    for stage in stages:
        stage = _require_object(stage, "lifecycle stage")
        _require_exact_keys(
            stage, {"name", "result", "evidence", "observation"}, "lifecycle stage"
        )
        if stage["result"] != "pass":
            _fail(f"lifecycle stage {stage['name']!r} does not record a pass")
        evidence = _require_list(stage["evidence"], "lifecycle evidence", MAX_FILES)
        if not evidence:
            _fail(f"lifecycle stage {stage['name']!r} names no evidence")
        for item in evidence:
            item = _require_object(item, "lifecycle evidence entry")
            _require_exact_keys(item, {"path", "size", "sha256"}, "lifecycle evidence")
            reference = bound.get(item["path"])
            if reference is None:
                _fail(f"lifecycle evidence {item['path']!r} is not receipt bound")
            if reference.size != item["size"] or reference.sha256 != item["sha256"]:
                _fail(f"lifecycle evidence {item['path']!r} does not match its binding")
        names.append(stage["name"])
    if len(set(names)) != len(names):
        _fail("stage lifecycle report repeats a stage name")

    for path, outcome in report["report_outcomes"].items():
        reference = bound.get(path)
        if reference is None or reference.kind != "report":
            _fail(f"recorded report outcome {path!r} is not a bound report")
        payload = RECEIPTS.read_bound_bytes(root, reference, REPORTS.MAX_REPORT_BYTES)
        try:
            if outcome.get("format") == PYTEST_JUNIT:
                REPORTS.verify_pytest_junit(payload)
            elif outcome.get("format") == NODE_TAP:
                REPORTS.verify_node_tap(payload)
            else:
                _fail(f"recorded report outcome {path!r} has an unsupported format")
        except REPORTS.ReportValidationError as error:
            _fail(f"bound report {path!r} did not pass: {error}")

    for entry in report["equalities"]:
        left = bound.get(entry["left"])
        right = bound.get(entry["right"])
        if left is None or right is None:
            _fail("a recorded equality names an unbound file")
        if left.sha256 != right.sha256 or left.sha256 != entry["sha256"]:
            _fail(
                f"recorded equality between {entry['left']!r} and "
                f"{entry['right']!r} no longer holds"
            )

    receipt_bytes = _read_regular_bytes(
        Path(receipt_path), RECEIPTS.MAX_RECEIPT_BYTES, "published receipt"
    )
    return StageReceiptBinding(
        check_id=receipt_policy.expected_check_id,
        receipt_path=Path(receipt_path).name,
        receipt_sha256=_digest(receipt_bytes),
        report_path=expected_report_path,
        report_sha256=bound[expected_report_path].sha256,
        stage_count=len(stages),
        file_count=len(bound),
    )


def _policy(arguments: argparse.Namespace) -> Any:
    return RECEIPTS.ReceiptPolicy(
        expected_commit=arguments.commit,
        expected_tree=arguments.tree,
        expected_repository=arguments.repository,
        expected_run_id=arguments.run_id,
        expected_attempt=arguments.attempt,
        expected_environment=arguments.environment,
        expected_provenance=arguments.provenance,
        expected_check_id=arguments.check_id,
        allowed_report_formats=tuple(arguments.format),
    )


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--provenance", required=True, choices=("hosted", "local", "controlled-fixture")
    )
    parser.add_argument("--check-id", required=True)
    parser.add_argument("--format", action="append", required=True)
    parser.add_argument("--receipt-name", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish", help="stage and receipt one check")
    _common_arguments(publish)
    publish.add_argument("--source-root", required=True, type=Path)
    publish.add_argument("--plan", required=True, type=Path)
    publish.add_argument("--output-root", required=True, type=Path)
    consume = commands.add_parser("consume", help="validate one published receipt")
    _common_arguments(consume)
    consume.add_argument("--evidence-root", required=True, type=Path)
    consume.add_argument("--report-path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Publish or validate one production stage receipt, failing closed."""

    arguments = _parser().parse_args(argv)
    try:
        policy = _policy(arguments)
        if arguments.command == "publish":
            binding = publish_stage_receipt(
                source_root=arguments.source_root,
                plan_path=arguments.plan,
                output_root=arguments.output_root,
                receipt_name=arguments.receipt_name,
                receipt_policy=policy,
            )
        else:
            binding = consume_stage_receipt(
                evidence_root=arguments.evidence_root,
                receipt_path=arguments.evidence_root / arguments.receipt_name,
                receipt_policy=policy,
                expected_report_path=arguments.report_path,
            )
    except (
        OSError,
        StageReceiptError,
        RECEIPTS.ReceiptValidationError,
        REPORTS.ReportValidationError,
    ) as error:
        print(f"desktop stage receipt failed: {error}", file=sys.stderr)
        return 1
    print(
        f"desktop stage receipt {binding.check_id} bound {binding.file_count} files "
        f"across {binding.stage_count} stages: sha256={binding.receipt_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
