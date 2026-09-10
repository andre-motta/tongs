"""Tests for strict expected-failure evidence verification."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from .probe import EXPECTED_PLUGIN_EVIDENCE, validate_plugin_evidence
from .verify_expected_failure import (
    EXPECTED_STEPS,
    FAILURE_MESSAGE,
    FAILURE_TEST_NAME,
    VerificationError,
    main,
    verify_expected_failure,
)


def _write_junit(
    path: Path,
    *,
    tests: int,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    deliberate: bool = False,
) -> None:
    suites = ET.Element("testsuites")
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    if deliberate:
        testcase = ET.SubElement(suite, "testcase", {"name": FAILURE_TEST_NAME})
        failure = ET.SubElement(testcase, "failure", {"message": FAILURE_MESSAGE})
        failure.text = FAILURE_MESSAGE
    ET.ElementTree(suites).write(path, encoding="unicode", xml_declaration=True)


def _valid_evidence(output_dir: Path) -> None:
    steps = [
        {"name": name, "returncode": 1 if name == "deliberate-failure" else 0}
        for name in EXPECTED_STEPS
    ]
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "expected_result": "failure",
                "failed_steps": ["deliberate-failure"],
                "result": "failed",
                "steps": steps,
            }
        )
    )
    _write_junit(output_dir / "core-tests.junit.xml", tests=621)
    _write_junit(output_dir / "backend-tests.junit.xml", tests=7)
    _write_junit(
        output_dir / "deliberate-failure.junit.xml",
        tests=1,
        failures=1,
        deliberate=True,
    )
    (output_dir / "plugin-discovery.json").write_text(
        json.dumps(
            {
                "desktop_backend_statuses": {
                    "sample-desktop": "ready",
                    "sample-terminal": "terminal_only",
                },
                "terminal_discovery": ["sample-desktop", "sample-terminal"],
                "terminal_only_command": "Sample terminal action",
            }
        )
    )


def test_accepts_only_the_intended_assertion_failure(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)

    verify_expected_failure(tmp_path, 1)
    assert main(["--output-dir", str(tmp_path), "--actual-status", "1"]) == 0


def test_rejects_setup_failure(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["steps"] = summary["steps"][:1]
    summary["steps"][0]["returncode"] = 1
    summary["failed_steps"] = ["build-core-wheel"]
    (tmp_path / "summary.json").write_text(json.dumps(summary))

    with pytest.raises(VerificationError, match="only deliberate-failure"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_missing_summary(tmp_path: Path) -> None:
    with pytest.raises(VerificationError, match="missing evidence: summary.json"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_malformed_summary(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text("not JSON")

    with pytest.raises(VerificationError, match="malformed JSON: summary.json"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_unexpected_required_step_failure(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["steps"][4]["returncode"] = 1
    summary["failed_steps"] = ["core-tests", "deliberate-failure"]
    (tmp_path / "summary.json").write_text(json.dumps(summary))

    with pytest.raises(VerificationError, match="only deliberate-failure"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_unexpected_step(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["steps"].insert(7, {"name": "unexpected-setup", "returncode": 0})
    (tmp_path / "summary.json").write_text(json.dumps(summary))

    with pytest.raises(VerificationError, match="unexpected harness steps"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_zero_harness_status(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)

    with pytest.raises(VerificationError, match="status must be 1"):
        verify_expected_failure(tmp_path, 0)


def test_rejects_missing_deliberate_junit(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    (tmp_path / "deliberate-failure.junit.xml").unlink()

    with pytest.raises(
        VerificationError,
        match="missing evidence: deliberate-failure.junit.xml",
    ):
        verify_expected_failure(tmp_path, 1)


def test_rejects_malformed_deliberate_junit(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    (tmp_path / "deliberate-failure.junit.xml").write_text("not XML")

    with pytest.raises(
        VerificationError,
        match="malformed JUnit: deliberate-failure.junit.xml",
    ):
        verify_expected_failure(tmp_path, 1)


def test_rejects_wrong_deliberate_assertion(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    _write_junit(
        tmp_path / "deliberate-failure.junit.xml",
        tests=1,
        failures=1,
        deliberate=False,
    )

    with pytest.raises(VerificationError, match="testcase is unexpected"):
        verify_expected_failure(tmp_path, 1)


def test_plugin_evidence_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        validate_plugin_evidence("not JSON")


def test_plugin_evidence_rejects_scalar_json() -> None:
    with pytest.raises(TypeError, match="JSON object"):
        validate_plugin_evidence('"ready"')


def test_plugin_evidence_rejects_wrong_status() -> None:
    evidence = json.loads(json.dumps(EXPECTED_PLUGIN_EVIDENCE))
    evidence["desktop_backend_statuses"]["sample-terminal"] = "ready"

    with pytest.raises(ValueError, match="unexpected compatibility evidence"):
        validate_plugin_evidence(json.dumps(evidence))
