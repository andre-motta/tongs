"""Verify that a harness failure came only from the deliberate assertion."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_STEPS = (
    "build-core-wheel",
    "build-reference-plugin-wheel",
    "install-wheels",
    "cli-help",
    "core-tests",
    "backend-fixture-tests",
    "installed-plugin-discovery",
    "deliberate-failure",
)
FAILURE_TEST_NAME = "test_deliberate_failure_propagates"
FAILURE_MESSAGE = "intentional harness failure for issue 27"


class VerificationError(ValueError):
    """Raised when expected-failure evidence is absent or inconsistent."""


@dataclass(frozen=True)
class JUnitCounts:
    """Aggregate counters from a JUnit report."""

    tests: int
    failures: int
    errors: int
    skipped: int


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except OSError as error:
        raise VerificationError(f"missing evidence: {path.name}") from error
    except json.JSONDecodeError as error:
        raise VerificationError(f"malformed JSON: {path.name}") from error


def _junit(path: Path) -> tuple[JUnitCounts, ET.Element]:
    try:
        root = ET.parse(path).getroot()
    except OSError as error:
        raise VerificationError(f"missing evidence: {path.name}") from error
    except ET.ParseError as error:
        raise VerificationError(f"malformed JUnit: {path.name}") from error

    suites = list(root.iter("testsuite"))
    if not suites:
        raise VerificationError(f"malformed JUnit: {path.name} has no testsuite")
    try:
        counts = JUnitCounts(
            tests=sum(int(suite.attrib["tests"]) for suite in suites),
            failures=sum(int(suite.attrib["failures"]) for suite in suites),
            errors=sum(int(suite.attrib["errors"]) for suite in suites),
            skipped=sum(int(suite.attrib["skipped"]) for suite in suites),
        )
    except (KeyError, ValueError) as error:
        raise VerificationError(f"malformed JUnit counters: {path.name}") from error
    return counts, root


def _verify_required_junit(output_dir: Path, filename: str) -> None:
    counts, _ = _junit(output_dir / filename)
    if counts.tests < 1 or any((counts.failures, counts.errors, counts.skipped)):
        raise VerificationError(
            f"required test report did not pass cleanly: {filename} {counts}"
        )


def _verify_deliberate_junit(output_dir: Path) -> None:
    filename = "deliberate-failure.junit.xml"
    counts, root = _junit(output_dir / filename)
    if counts != JUnitCounts(tests=1, failures=1, errors=0, skipped=0):
        raise VerificationError(f"deliberate failure counters are unexpected: {counts}")

    testcases = list(root.iter("testcase"))
    if len(testcases) != 1 or testcases[0].attrib.get("name") != FAILURE_TEST_NAME:
        raise VerificationError("deliberate failure testcase is unexpected")
    failures = list(testcases[0].iter("failure"))
    failure_text = (
        " ".join([failures[0].attrib.get("message", ""), failures[0].text or ""])
        if len(failures) == 1
        else ""
    )
    if FAILURE_MESSAGE not in failure_text:
        raise VerificationError("deliberate failure message is unexpected")


def _verify_plugins(output_dir: Path) -> None:
    evidence = _load_json(output_dir / "plugin-discovery.json")
    expected = {
        "desktop_backend_statuses": {
            "sample-desktop": "ready",
            "sample-terminal": "terminal_only",
        },
        "terminal_discovery": ["sample-desktop", "sample-terminal"],
        "terminal_only_command": "Sample terminal action",
    }
    if evidence != expected:
        raise VerificationError("installed plugin discovery evidence is unexpected")


def verify_expected_failure(output_dir: Path, actual_status: int) -> None:
    """Validate the complete expected-failure result directory."""
    if actual_status != 1:
        raise VerificationError(
            f"harness status must be 1 for the deliberate failure, got {actual_status}"
        )

    summary = _load_json(output_dir / "summary.json")
    if not isinstance(summary, dict):
        raise VerificationError("malformed summary: expected a JSON object")
    if summary.get("expected_result") != "failure":
        raise VerificationError("summary expected_result must be failure")
    if summary.get("result") != "failed":
        raise VerificationError("summary result must be failed")
    if summary.get("failed_steps") != ["deliberate-failure"]:
        raise VerificationError("only deliberate-failure may be a failed step")

    steps = summary.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise VerificationError("malformed summary steps")
    names = tuple(step.get("name") for step in steps)
    if names != EXPECTED_STEPS:
        raise VerificationError(f"unexpected harness steps: {names}")
    returncodes = tuple(step.get("returncode") for step in steps)
    if not all(type(returncode) is int for returncode in returncodes):
        raise VerificationError("malformed harness return codes")
    if returncodes != (0, 0, 0, 0, 0, 0, 0, 1):
        raise VerificationError(f"unexpected harness return codes: {returncodes}")

    _verify_required_junit(output_dir, "core-tests.junit.xml")
    _verify_required_junit(output_dir, "backend-tests.junit.xml")
    _verify_deliberate_junit(output_dir)
    _verify_plugins(output_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--actual-status", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_expected_failure(args.output_dir, args.actual_status)
    except VerificationError as error:
        print(f"Expected-failure verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "Deliberate failure evidence verified: required checks passed, assertion failed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
