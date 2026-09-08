"""Adversarial tests for the bounded desktop receipt consumer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_validator() -> ModuleType:
    script_path = (
        Path(__file__).parents[3]
        / ".github"
        / "scripts"
        / "verify_desktop_production.py"
    )
    script_spec = importlib.util.spec_from_file_location(
        "verify_desktop_production_receipts", script_path
    )
    assert script_spec is not None and script_spec.loader is not None
    validator: ModuleType = importlib.util.module_from_spec(script_spec)
    sys.modules[script_spec.name] = validator
    script_spec.loader.exec_module(validator)
    return validator


VERIFY = _load_validator()

COMMIT = "a" * 40
TREE = "b" * 40
CHECK_ID = "desktop-python-312"
REPOSITORY = "andre-motta/tongs"
RUN_ID = "123456"
ATTEMPT = 1
ENVIRONMENT = "ubuntu-24.04-python-3.12"
PROVENANCE = "hosted"
REPORT_PATH = "reports/junit.xml"
REPORT_BYTES = b"<testsuite tests='1' failures='0' errors='0' skipped='0'/>\n"


def _policy(
    *,
    commit: str = COMMIT,
    tree: str = TREE,
    check_id: str = CHECK_ID,
    report_format: str = VERIFY.PYTEST_JUNIT_FORMAT,
) -> VERIFY.ReceiptPolicy:
    return VERIFY.ReceiptPolicy(
        expected_commit=commit,
        expected_tree=tree,
        expected_repository=REPOSITORY,
        expected_run_id=RUN_ID,
        expected_attempt=ATTEMPT,
        expected_environment=ENVIRONMENT,
        expected_provenance=PROVENANCE,
        expected_check_id=check_id,
        allowed_report_formats=frozenset({report_format}),
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(root: Path, relative_path: str, value: bytes) -> None:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value)


def _receipt_data(root: Path) -> dict[str, object]:
    _write(root, REPORT_PATH, REPORT_BYTES)
    artifact_path = "artifacts/archive.tar.gz"
    artifact_bytes = b"archive bytes\n"
    _write(root, artifact_path, artifact_bytes)
    input_path = "inputs/build.json"
    input_bytes = b'{"source":"fixture"}\n'
    _write(root, input_path, input_bytes)
    return {
        "schema_version": 1,
        "check_id": CHECK_ID,
        "source": {"commit": COMMIT, "tree": TREE},
        "execution": {
            "repository": REPOSITORY,
            "run_id": RUN_ID,
            "attempt": ATTEMPT,
            "environment": ENVIRONMENT,
            "provenance": PROVENANCE,
        },
        "result": "success",
        "reports": [
            {
                "path": REPORT_PATH,
                "size": len(REPORT_BYTES),
                "sha256": _digest(REPORT_BYTES),
                "format": VERIFY.PYTEST_JUNIT_FORMAT,
            }
        ],
        "artifacts": [
            {
                "path": artifact_path,
                "size": len(artifact_bytes),
                "sha256": _digest(artifact_bytes),
                "role": "desktop-archive",
            }
        ],
        "inputs": [{"path": input_path, "sha256": _digest(input_bytes)}],
    }


def _encoded(data: dict[str, object]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _validate(
    root: Path, data: dict[str, object], policy: VERIFY.ReceiptPolicy | None = None
):
    return VERIFY.validate_receipt(
        _encoded(data), evidence_root=root, policy=policy or _policy()
    )


def test_valid_receipt_binds_reports_artifacts_and_inputs(tmp_path: Path) -> None:
    result = _validate(tmp_path, _receipt_data(tmp_path))

    assert [bound.path for bound in result.bound_files] == [
        REPORT_PATH,
        "artifacts/archive.tar.gz",
        "inputs/build.json",
    ]
    assert all(bound.size > 0 for bound in result.bound_files)


def test_receipt_file_must_be_below_staged_root(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_encoded(data))

    result = VERIFY.validate_receipt_file(
        receipt, evidence_root=tmp_path, policy=_policy()
    )

    assert result.receipt["check_id"] == CHECK_ID


def test_receipt_file_outside_staged_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "receipt.json"
    outside.write_bytes(b"{}")

    with pytest.raises(VERIFY.ReceiptValidationError, match="below"):
        VERIFY.validate_receipt_file(outside, evidence_root=root, policy=_policy())


def test_consumer_identity_rejects_stale_receipt(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)

    with pytest.raises(VERIFY.ReceiptValidationError, match="identity"):
        _validate(tmp_path, data, _policy(commit="c" * 40))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "andre-motta/other-repository"),
        ("run_id", "654321"),
        ("attempt", 2),
        ("environment", "ubuntu-24.04-python-3.13"),
        ("provenance", "local"),
    ],
)
def test_consumer_execution_identity_rejects_each_mismatch(
    tmp_path: Path, field: str, value: object
) -> None:
    data = _receipt_data(tmp_path)
    data["execution"][field] = value  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="execution identity"):
        _validate(tmp_path, data)


def test_consumer_hosted_expectation_rejects_local_provenance(
    tmp_path: Path,
) -> None:
    data = _receipt_data(tmp_path)
    data["execution"]["provenance"] = "local"  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="execution identity"):
        _validate(tmp_path, data, _policy())


def test_consumer_check_id_and_format_are_independent(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["check_id"] = "desktop-python-313"

    with pytest.raises(VERIFY.ReceiptValidationError, match="consumer expectation"):
        _validate(tmp_path, data)


def test_duplicate_json_keys_are_rejected_before_schema_validation(
    tmp_path: Path,
) -> None:
    _receipt_data(tmp_path)
    duplicate = b'{"schema_version":1,"schema_version":1}'

    with pytest.raises(VERIFY.ReceiptValidationError, match="duplicate"):
        VERIFY.validate_receipt(duplicate, evidence_root=tmp_path, policy=_policy())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("attempt", True),
        ("report_size", True),
    ],
)
def test_boolean_counters_are_not_integers(
    tmp_path: Path, field: str, value: object
) -> None:
    data = _receipt_data(tmp_path)
    if field == "report_size":
        data["reports"][0]["size"] = value  # type: ignore[index]
    elif field == "attempt":
        data["execution"]["attempt"] = value  # type: ignore[index]
    else:
        data[field] = value

    with pytest.raises(VERIFY.ReceiptValidationError):
        _validate(tmp_path, data)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.xml",
        "reports/../outside.xml",
        "reports\\junit.xml",
        "reports/\x00junit.xml",
        "C:/outside.xml",
    ],
)
def test_unsafe_relative_paths_are_rejected(tmp_path: Path, path: str) -> None:
    data = _receipt_data(tmp_path)
    data["reports"][0]["path"] = path  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="path|traversal|bounded"):
        _validate(tmp_path, data)


def test_duplicate_paths_across_receipt_sections_are_rejected(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["artifacts"].append(  # type: ignore[union-attr]
        {
            "path": "REPORTS/JUNIT.XML",
            "size": len(REPORT_BYTES),
            "sha256": _digest(REPORT_BYTES),
            "role": "duplicate",
        }
    )

    with pytest.raises(VERIFY.ReceiptValidationError, match="duplicate"):
        _validate(tmp_path, data)


def test_symlinked_report_is_not_a_real_bound_file(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    link = tmp_path / "reports" / "link.xml"
    link.symlink_to(tmp_path / REPORT_PATH)
    data["reports"][0].update(  # type: ignore[index]
        {"path": "reports/link.xml", "sha256": _digest(REPORT_BYTES)}
    )

    with pytest.raises(VERIFY.ReceiptValidationError):
        _validate(tmp_path, data)


def test_symlinked_evidence_root_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(VERIFY.ReceiptValidationError, match="real directory"):
        _validate(linked_root, _receipt_data(real_root))


def test_symlinked_intermediate_directory_is_rejected(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    reports = tmp_path / "reports"
    real_reports = tmp_path / "real-reports"
    reports.rename(real_reports)
    reports.symlink_to(real_reports, target_is_directory=True)

    with pytest.raises(VERIFY.ReceiptValidationError):
        _validate(tmp_path, data)


def test_symlinked_receipt_is_rejected(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    real_receipt = tmp_path / "real-receipt.json"
    real_receipt.write_bytes(_encoded(data))
    linked_receipt = tmp_path / "receipt.json"
    linked_receipt.symlink_to(real_receipt)

    with pytest.raises(VERIFY.ReceiptValidationError):
        VERIFY.validate_receipt_file(
            linked_receipt, evidence_root=tmp_path, policy=_policy()
        )


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    fifo_path = tmp_path / "reports" / "result.pipe"
    os.mkfifo(fifo_path)
    data["reports"][0].update(  # type: ignore[index]
        {
            "path": "reports/result.pipe",
            "size": 1,
            "sha256": "0" * 64,
        }
    )

    with pytest.raises(VERIFY.ReceiptValidationError, match="regular file"):
        _validate(tmp_path, data)


def test_receipt_document_size_is_bounded(tmp_path: Path) -> None:
    oversized = b"{" + b"x" * VERIFY.MAX_RECEIPT_BYTES + b"}"

    with pytest.raises(VERIFY.ReceiptValidationError, match="size"):
        VERIFY.validate_receipt(oversized, evidence_root=tmp_path, policy=_policy())


def test_input_receipt_at_bound_is_accepted(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    input_bytes = b"i" * VERIFY.MAX_RECEIPT_BYTES
    input_path = tmp_path / "inputs" / "build.json"
    input_path.write_bytes(input_bytes)
    data["inputs"][0]["sha256"] = _digest(input_bytes)  # type: ignore[index]

    result = _validate(tmp_path, data)

    assert result.bound_files[-1].size == VERIFY.MAX_RECEIPT_BYTES


def test_oversized_input_receipt_is_rejected(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    input_bytes = b"i" * (VERIFY.MAX_RECEIPT_BYTES + 1)
    input_path = tmp_path / "inputs" / "build.json"
    input_path.write_bytes(input_bytes)
    data["inputs"][0]["sha256"] = _digest(input_bytes)  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="supported size bound"):
        _validate(tmp_path, data)


def test_artifact_archive_is_not_limited_by_receipt_bound(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    artifact_bytes = b"a" * (VERIFY.MAX_RECEIPT_BYTES + 1)
    artifact_path = tmp_path / "artifacts" / "archive.tar.gz"
    artifact_path.write_bytes(artifact_bytes)
    data["artifacts"][0].update(  # type: ignore[index]
        {"size": len(artifact_bytes), "sha256": _digest(artifact_bytes)}
    )

    result = _validate(tmp_path, data)

    assert result.bound_files[1].size == VERIFY.MAX_RECEIPT_BYTES + 1


def test_unknown_top_level_field_is_rejected(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["unexpected"] = "producer-controlled"

    with pytest.raises(VERIFY.ReceiptValidationError, match="unexpected"):
        _validate(tmp_path, data)


def test_malformed_digest_is_rejected_before_file_access(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["reports"][0]["sha256"] = "not-a-digest"  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="SHA-256"):
        _validate(tmp_path, data)


def test_file_replacement_between_binding_reads_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _receipt_data(tmp_path)
    original_read = VERIFY._read_staged_file
    calls = 0

    def read_once_then_replace(
        root: Path,
        relative_path: str,
        expected_size: int | None,
        maximum_size: int | None,
    ) -> tuple[int, str, object, object]:
        nonlocal calls
        result = original_read(root, relative_path, expected_size, maximum_size)
        calls += 1
        if calls == 1:
            replacement = root / "reports" / "replacement.xml"
            replacement.write_bytes(b"X" * len(REPORT_BYTES))
            os.replace(replacement, root / REPORT_PATH)
        return result

    monkeypatch.setattr(VERIFY, "_read_staged_file", read_once_then_replace)
    with pytest.raises(VERIFY.ReceiptValidationError, match="replaced|changed"):
        _validate(tmp_path, data)


@pytest.mark.parametrize("mutation", ["size", "sha256"])
def test_real_file_binding_rejects_size_or_digest_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    data = _receipt_data(tmp_path)
    if mutation == "size":
        data["reports"][0]["size"] += 1  # type: ignore[operator]
    else:
        data["reports"][0]["sha256"] = "0" * 64  # type: ignore[index]

    with pytest.raises(VERIFY.ReceiptValidationError, match="match"):
        _validate(tmp_path, data)


def test_lifecycle_json_report_has_a_smaller_bound(tmp_path: Path) -> None:
    oversized = b"{" + b"x" * VERIFY.MAX_JSON_REPORT_BYTES + b"}"
    path = "reports/lifecycle.json"
    _write(tmp_path, path, oversized)
    data = _receipt_data(tmp_path)
    data["reports"] = [
        {
            "path": path,
            "size": len(oversized),
            "sha256": _digest(oversized),
            "format": VERIFY.ARTIFACT_LIFECYCLE_FORMAT,
        }
    ]

    with pytest.raises(VERIFY.ReceiptValidationError, match="JSON report"):
        _validate(
            tmp_path,
            data,
            _policy(report_format=VERIFY.ARTIFACT_LIFECYCLE_FORMAT),
        )


def test_combined_entry_count_is_bounded(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["artifacts"] = [
        {
            "path": f"artifacts/{index}.bin",
            "size": 0,
            "sha256": "0" * 64,
            "role": "fixture",
        }
        for index in range(VERIFY.MAX_ARRAY_ENTRIES)
    ]

    with pytest.raises(VERIFY.ReceiptValidationError, match="combined"):
        _validate(tmp_path, data)


def test_cli_does_not_advertise_structural_validation_as_production_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _receipt_data(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_encoded(data))

    result = VERIFY.main(
        [
            "--receipt",
            str(receipt),
            "--evidence-root",
            str(tmp_path),
            "--commit",
            COMMIT,
            "--tree",
            TREE,
            "--repository",
            REPOSITORY,
            "--run-id",
            RUN_ID,
            "--attempt",
            str(ATTEMPT),
            "--environment",
            ENVIRONMENT,
            "--provenance",
            PROVENANCE,
            "--check-id",
            CHECK_ID,
            "--format",
            VERIFY.PYTEST_JUNIT_FORMAT,
        ]
    )

    assert result == 0
    assert "production success require independent checks" in capsys.readouterr().out


def test_cli_rejects_duplicate_check_id_occurrences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = VERIFY.main(
        [
            "--receipt",
            str(tmp_path / "missing.json"),
            "--evidence-root",
            str(tmp_path),
            "--commit",
            COMMIT,
            "--tree",
            TREE,
            "--repository",
            REPOSITORY,
            "--run-id",
            RUN_ID,
            "--attempt",
            str(ATTEMPT),
            "--environment",
            ENVIRONMENT,
            "--provenance",
            PROVENANCE,
            "--check-id",
            CHECK_ID,
            "--check-id",
            CHECK_ID,
            "--format",
            VERIFY.PYTEST_JUNIT_FORMAT,
        ]
    )

    assert result == 1
    assert "exactly once" in capsys.readouterr().err
