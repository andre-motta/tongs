"""Verify the aggregate's lane results and reject skipped MCP test reports.

The ``aggregate`` command reads the effective lane plan the aggregate job wrote
and the ``toJSON(needs)`` results of ordinary CI.  The needed job set must be
exactly the lane jobs plus ``changes``.  A lane the effective plan selected must
report ``success``; a lane it deselected must report exactly ``skipped``, so a
deselected lane that still ran proves the wiring drifted and fails.  ``changes``
must succeed unless the effective plan is the full graph and every lane
succeeded, in which case the aggregate records that it fell back to the full
graph.

A partial rerun ("Re-run failed jobs") is judged the same way: each entry of
``needs`` is that job's latest attempt, so a lane that passed in an earlier
attempt reports ``success`` and a lane still failing reports ``failure``.  The
aggregate runs this check before it downloads any evidence, so a lane's
evidence left over from an earlier attempt never stands in for its result.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CI_PLAN_PROGRAM = "tests/ci/ci_plan.py"
#: One shared module name, so both verifiers see the same ``Plan`` class.
CI_PLAN_MODULE = "tongs_ci_plan"


class VerificationError(ValueError):
    """Raised when required CI evidence is absent or inconsistent."""


def _load_ci_plan() -> ModuleType:
    loaded = sys.modules.get(CI_PLAN_MODULE)
    if loaded is not None:
        return loaded
    path = ROOT / CI_PLAN_PROGRAM
    specification = importlib.util.spec_from_file_location(CI_PLAN_MODULE, path)
    if specification is None or specification.loader is None:
        raise VerificationError(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[CI_PLAN_MODULE] = module
    specification.loader.exec_module(module)
    return module


CI_PLAN = _load_ci_plan()

REQUIRED_GATE_JOBS: frozenset[str] = frozenset(CI_PLAN.LANE_CI_JOBS.values()) | {
    CI_PLAN.CHANGES_JOB
}


def load_plan(path: Path) -> Any:
    """Read the effective plan file strictly."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise VerificationError(f"effective plan is unreadable: {path}") from error
    try:
        return CI_PLAN.Plan.from_json(text)
    except ValueError as error:
        raise VerificationError(f"effective plan is malformed: {error}") from error


def verify_job_set(
    raw_results: str, expected: dict[str, frozenset[str]], label: str
) -> dict[str, str]:
    """Require the exact job set and an allowed result for each job."""

    if not isinstance(raw_results, str) or not raw_results.strip():
        raise VerificationError(f"{label} results are absent")
    try:
        results: Any = json.loads(raw_results)
    except json.JSONDecodeError as error:
        raise VerificationError(f"{label} results are malformed JSON") from error
    if not isinstance(results, dict):
        raise VerificationError(f"{label} results must be a JSON object")

    actual_jobs = set(results)
    if actual_jobs != set(expected):
        missing = sorted(set(expected) - actual_jobs)
        unexpected = sorted(actual_jobs - set(expected))
        raise VerificationError(
            f"{label} job set mismatch: missing={missing}, unexpected={unexpected}"
        )

    observed: dict[str, str] = {}
    rejected: list[str] = []
    for name in sorted(expected):
        job = results[name]
        if not isinstance(job, dict):
            rejected.append(f"{name}=malformed")
            continue
        result = job.get("result")
        if result not in expected[name]:
            allowed = "|".join(sorted(expected[name]))
            rejected.append(f"{name}={result!r} (expected {allowed})")
            continue
        observed[name] = result
    if rejected:
        raise VerificationError(
            f"{label} jobs did not match the effective plan: " + ", ".join(rejected)
        )
    return observed


def verify_aggregate_results(raw_results: str, plan: Any) -> None:
    """Require every selected lane to succeed and every other lane to skip."""

    try:
        expected = CI_PLAN.expected_ci_results(plan)
    except (AttributeError, ValueError) as error:
        raise VerificationError(f"effective plan is invalid: {error}") from error
    if set(expected) != REQUIRED_GATE_JOBS:
        raise VerificationError("lane policy does not cover the aggregate job set")
    verify_job_set(raw_results, expected, "aggregate")


def fallback_note(raw_results: str, plan: Any) -> str | None:
    """Describe a verified fallback to the full graph, if one happened."""

    results = json.loads(raw_results)
    changes = results[CI_PLAN.CHANGES_JOB]["result"]
    if plan.full and changes != "success":
        return (
            f"changes reported {changes!r}, so the aggregate fell back to the full "
            "graph and every lane succeeded"
        )
    return None


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
    aggregate = commands.add_parser("aggregate", help="verify DESKTOP_GATE_RESULTS")
    aggregate.add_argument("--plan", required=True, type=Path)
    mcp_report = commands.add_parser("mcp-report", help="verify an MCP JUnit report")
    mcp_report.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "aggregate":
            plan = load_plan(args.plan)
            raw_results = os.environ.get("DESKTOP_GATE_RESULTS", "")
            verify_aggregate_results(raw_results, plan)
            note = fallback_note(raw_results, plan)
            if note is not None:
                print(note)
            selected = ", ".join(lane for lane in CI_PLAN.LANES if lane in plan.lanes)
            graph = "full graph" if plan.full else "reduced graph"
            print(f"Effective plan: {graph}; selected lanes: {selected or 'none'}")
        else:
            verify_mcp_junit(args.path)
    except VerificationError as error:
        print(f"CI verification failed: {error}", file=sys.stderr)
        return 1
    print(f"CI {args.command} evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
