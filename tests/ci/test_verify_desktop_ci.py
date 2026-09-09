"""Tests for strict desktop CI aggregate and MCP evidence validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ci.verify_desktop_ci import (
    REQUIRED_GATE_JOBS,
    VerificationError,
    verify_aggregate_results,
    verify_mcp_junit,
)


def _results(result: str = "success") -> dict[str, object]:
    return {name: {"result": result, "outputs": {}} for name in REQUIRED_GATE_JOBS}


def _write_junit(
    path: Path,
    *,
    tests: int = 1,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    outcome: str = "",
) -> None:
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="{skipped}">'
        f'<testcase classname="tests.test_mcp.test_server" name="test_import">'
        f"{outcome}</testcase></testsuite></testsuites>"
    )


def test_aggregate_accepts_exact_successful_job_set() -> None:
    verify_aggregate_results(json.dumps(_results()))


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled", "not-run", None])
def test_aggregate_rejects_every_non_success_result(result: object) -> None:
    results = _results()
    results["fedora-podman"] = {"result": result, "outputs": {}}
    with pytest.raises(VerificationError, match="fedora-podman"):
        verify_aggregate_results(json.dumps(results))


@pytest.mark.parametrize("raw_results", ["", "[]", "{broken"])
def test_aggregate_rejects_malformed_results(raw_results: str) -> None:
    with pytest.raises(VerificationError):
        verify_aggregate_results(raw_results)


def test_aggregate_rejects_a_malformed_required_job() -> None:
    results = _results()
    results["desktop-production"] = None
    with pytest.raises(VerificationError, match="desktop-production=malformed"):
        verify_aggregate_results(json.dumps(results))


def test_mcp_report_accepts_a_clean_test_run(tmp_path: Path) -> None:
    report = tmp_path / "mcp.xml"
    _write_junit(report)
    verify_mcp_junit(report)


@pytest.mark.parametrize(
    ("counts", "outcome"),
    [
        ({"failures": 1}, '<failure message="failed"/>'),
        ({"errors": 1}, '<error message="import error"/>'),
        ({"skipped": 1}, '<skipped message="mcp not installed"/>'),
    ],
)
def test_mcp_report_rejects_non_passing_outcomes(
    tmp_path: Path, counts: dict[str, int], outcome: str
) -> None:
    report = tmp_path / "mcp.xml"
    _write_junit(report, **counts, outcome=outcome)
    with pytest.raises(VerificationError, match="did not pass"):
        verify_mcp_junit(report)


def test_mcp_report_rejects_missing_malformed_empty_and_mismatched_reports(
    tmp_path: Path,
) -> None:
    report = tmp_path / "mcp.xml"
    with pytest.raises(VerificationError, match="missing"):
        verify_mcp_junit(report)

    report.write_text("<testsuites>")
    with pytest.raises(VerificationError, match="malformed"):
        verify_mcp_junit(report)

    report.write_text(
        '<testsuites><testsuite name="pytest" tests="0" failures="0" '
        'errors="0" skipped="0"/></testsuites>'
    )
    with pytest.raises(VerificationError, match="contains no tests"):
        verify_mcp_junit(report)

    _write_junit(report, tests=2)
    with pytest.raises(VerificationError, match="counters do not match"):
        verify_mcp_junit(report)


def test_mcp_report_rejects_a_non_mcp_testcase(tmp_path: Path) -> None:
    report = tmp_path / "mcp.xml"
    _write_junit(report)
    report.write_text(
        report.read_text().replace("tests.test_mcp.", "tests.test_cache.")
    )
    with pytest.raises(VerificationError, match="outside tests/test_mcp"):
        verify_mcp_junit(report)
