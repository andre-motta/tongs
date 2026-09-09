"""Reject incomplete aggregate results and skipped MCP test reports."""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# NEGATIVE CONTROL BRANCH ONLY: reduced to the jobs this scratch run keeps.
REQUIRED_GATE_JOBS = frozenset({"desktop-production"})


class VerificationError(ValueError):
    """Raised when required CI evidence is absent or inconsistent."""


def verify_aggregate_results(raw_results: str) -> None:
    """Require the exact desktop gate job set and a success result for each job."""
    try:
        results: Any = json.loads(raw_results)
    except json.JSONDecodeError as error:
        raise VerificationError("aggregate results are malformed JSON") from error

    if not isinstance(results, dict):
        raise VerificationError("aggregate results must be a JSON object")

    actual_jobs = set(results)
    if actual_jobs != REQUIRED_GATE_JOBS:
        missing = sorted(REQUIRED_GATE_JOBS - actual_jobs)
        unexpected = sorted(actual_jobs - REQUIRED_GATE_JOBS)
        raise VerificationError(
            f"aggregate job set mismatch: missing={missing}, unexpected={unexpected}"
        )

    rejected: list[str] = []
    for name in sorted(REQUIRED_GATE_JOBS):
        job = results[name]
        if not isinstance(job, dict):
            rejected.append(f"{name}=malformed")
            continue
        result = job.get("result")
        if result != "success":
            rejected.append(f"{name}={result!r}")
    if rejected:
        raise VerificationError(
            "required jobs did not all succeed: " + ", ".join(rejected)
        )


def _integer_attribute(suite: ET.Element, attribute: str, path: Path) -> int:
    try:
        value = int(suite.attrib[attribute])
    except (KeyError, ValueError) as error:
        raise VerificationError(
            f"MCP JUnit has an invalid {attribute!r} counter: {path}"
        ) from error
    if value < 0:
        raise VerificationError(
            f"MCP JUnit has a negative {attribute!r} counter: {path}"
        )
    return value


def verify_mcp_junit(path: Path) -> None:
    """Require a well-formed JUnit report with tests and no non-passing outcomes."""
    try:
        root = ET.parse(path).getroot()
    except OSError as error:
        raise VerificationError(f"MCP JUnit report is missing: {path}") from error
    except ET.ParseError as error:
        raise VerificationError(f"MCP JUnit report is malformed: {path}") from error

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        suites = []
    if not suites:
        raise VerificationError(f"MCP JUnit contains no test suites: {path}")

    declared = {
        attribute: sum(_integer_attribute(suite, attribute, path) for suite in suites)
        for attribute in ("tests", "failures", "errors", "skipped")
    }
    testcases = [testcase for suite in suites for testcase in suite.findall("testcase")]
    observed = {
        "tests": len(testcases),
        "failures": sum(len(testcase.findall("failure")) for testcase in testcases),
        "errors": sum(len(testcase.findall("error")) for testcase in testcases),
        "skipped": sum(len(testcase.findall("skipped")) for testcase in testcases),
    }
    if declared != observed:
        raise VerificationError(
            f"MCP JUnit counters do not match test cases: declared={declared}, "
            f"observed={observed}"
        )
    if observed["tests"] < 1:
        raise VerificationError("MCP JUnit contains no tests")
    if any(
        not testcase.attrib.get("classname", "").startswith("tests.test_mcp.")
        for testcase in testcases
    ):
        raise VerificationError("MCP JUnit contains a testcase outside tests/test_mcp")
    if any(observed[name] for name in ("failures", "errors", "skipped")):
        raise VerificationError(f"MCP tests did not pass without skips: {observed}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("aggregate", help="verify DESKTOP_GATE_RESULTS")
    mcp_report = commands.add_parser("mcp-report", help="verify an MCP JUnit report")
    mcp_report.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "aggregate":
            verify_aggregate_results(os.environ.get("DESKTOP_GATE_RESULTS", ""))
        else:
            verify_mcp_junit(args.path)
    except VerificationError as error:
        print(f"Desktop CI verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Desktop CI {args.command} evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
