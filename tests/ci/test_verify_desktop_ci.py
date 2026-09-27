"""Tests for plan-aware CI aggregate and strict MCP evidence validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ci.verify_desktop_ci import (
    CI_PLAN,
    VerificationError,
    fallback_note,
    load_plan,
    main,
    verify_aggregate_results,
    verify_mcp_junit,
    verify_pytest_junit,
)

COMMIT = "1" * 40
FULL = CI_PLAN.full_plan(COMMIT, "event 'push' is not pull_request")


def _plan(*lanes: str) -> object:
    return CI_PLAN.Plan(
        version=CI_PLAN.PLAN_VERSION,
        full=False,
        lanes=frozenset(lanes),
        reasons=(),
        checked_out=COMMIT,
    )


DOCS_ONLY = _plan("docs")
TUI_ONLY = _plan("lint", "core")
DESKTOP_WITHOUT_PACKAGING = _plan("lint", "core", "desktop")
DESKTOP_SOURCE = _plan("lint", "core", "desktop", "archive")


def _results(plan: object, **overrides: str | None) -> dict[str, object]:
    results: dict[str, object] = {"changes": {"result": "success", "outputs": {}}}
    for lane, job in CI_PLAN.LANE_CI_JOBS.items():
        result = "success" if lane in plan.lanes else "skipped"
        results[job] = {"result": result, "outputs": {}}
    for job, result in overrides.items():
        results[job.replace("_", "-")] = {"result": result, "outputs": {}}
    return results


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


@pytest.mark.parametrize(
    "plan",
    [FULL, DOCS_ONLY, DESKTOP_WITHOUT_PACKAGING, DESKTOP_SOURCE],
    ids=["full", "docs", "desktop-without-packaging", "desktop-source"],
)
def test_aggregate_accepts_the_results_its_plan_selects(plan: object) -> None:
    verify_aggregate_results(json.dumps(_results(plan)), plan)


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled", "not-run", None])
def test_aggregate_rejects_every_non_success_selected_lane(result: object) -> None:
    results = _results(FULL, fedora_podman=result)
    with pytest.raises(VerificationError, match="fedora-podman"):
        verify_aggregate_results(json.dumps(results), FULL)


def test_a_partial_rerun_passes_once_every_rerun_lane_passes() -> None:
    """The ``needs`` context holds each job's latest attempt after a rerun.

    Attempt 2 reran only the failed desktop production lane and it passed, so
    every selected lane now reports success, whichever attempt produced it.
    """

    verify_aggregate_results(json.dumps(_results(FULL)), FULL)


@pytest.mark.parametrize("lane", ["core", "desktop-production", "fedora-podman"])
def test_a_lane_still_failing_after_a_partial_rerun_fails_the_aggregate(
    lane: str,
) -> None:
    with pytest.raises(VerificationError, match=f"{lane}='failure'"):
        verify_aggregate_results(json.dumps(_results(FULL, **{lane: "failure"})), FULL)


@pytest.mark.parametrize("result", ["failure", "success", "cancelled", None])
def test_a_deselected_lane_must_report_exactly_skipped(result: object) -> None:
    """A deselected lane that ran anyway proves the wiring drifted."""

    # The verifier treats every lane job alike, so one job stands for all.
    results = _results(DOCS_ONLY)
    results["core"] = {"result": result, "outputs": {}}
    with pytest.raises(VerificationError, match="core"):
        verify_aggregate_results(json.dumps(results), DOCS_ONLY)


def test_a_docs_only_plan_requires_the_docs_lane_to_succeed() -> None:
    results = _results(DOCS_ONLY, docs="skipped")
    with pytest.raises(VerificationError, match="docs='skipped'"):
        verify_aggregate_results(json.dumps(results), DOCS_ONLY)


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_changes_must_succeed_for_a_reduced_plan(result: object) -> None:
    results = _results(TUI_ONLY, changes=result)
    with pytest.raises(VerificationError, match="changes"):
        verify_aggregate_results(json.dumps(results), TUI_ONLY)


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_full_plan_falls_back_when_changes_failed(result: str) -> None:
    results = json.dumps(_results(FULL, changes=result))
    verify_aggregate_results(results, FULL)
    note = fallback_note(results, FULL)
    assert note is not None and "fell back to the full graph" in note


def test_a_full_fallback_still_requires_every_lane() -> None:
    results = _results(FULL, changes="failure", docs="skipped")
    with pytest.raises(VerificationError, match="docs"):
        verify_aggregate_results(json.dumps(results), FULL)


def test_a_full_fallback_rejects_an_unknown_changes_result() -> None:
    results = _results(FULL, changes="neutral")
    with pytest.raises(VerificationError, match="changes"):
        verify_aggregate_results(json.dumps(results), FULL)


def test_no_fallback_is_recorded_when_changes_succeeded() -> None:
    assert fallback_note(json.dumps(_results(FULL)), FULL) is None


def test_aggregate_rejects_missing_or_unexpected_jobs() -> None:
    missing = _results(FULL)
    missing.pop("fedora-podman")
    with pytest.raises(VerificationError, match="fedora-podman"):
        verify_aggregate_results(json.dumps(missing), FULL)

    unexpected = _results(FULL)
    unexpected["optional"] = {"result": "success"}
    with pytest.raises(VerificationError, match="optional"):
        verify_aggregate_results(json.dumps(unexpected), FULL)

    without_changes = _results(DOCS_ONLY)
    without_changes.pop("changes")
    with pytest.raises(VerificationError, match="changes"):
        verify_aggregate_results(json.dumps(without_changes), DOCS_ONLY)


def test_aggregate_rejects_malformed_results() -> None:
    with pytest.raises(VerificationError):
        verify_aggregate_results("{broken", FULL)


def test_aggregate_rejects_a_malformed_required_job() -> None:
    results = _results(FULL)
    results["core"] = None
    with pytest.raises(VerificationError, match="core=malformed"):
        verify_aggregate_results(json.dumps(results), FULL)


def test_aggregate_rejects_an_unclosed_or_inconsistent_plan() -> None:
    unclosed = _plan("packaging")
    with pytest.raises(VerificationError, match="effective plan is invalid"):
        verify_aggregate_results(json.dumps(_results(FULL)), unclosed)
    partial_full = CI_PLAN.Plan(
        version=1, full=True, lanes=frozenset({"docs"}), reasons=(), checked_out=""
    )
    with pytest.raises(VerificationError, match="effective plan is invalid"):
        verify_aggregate_results(json.dumps(_results(FULL)), partial_full)


def test_load_plan_rejects_a_missing_or_malformed_file(tmp_path: Path) -> None:
    with pytest.raises(VerificationError, match="unreadable"):
        load_plan(tmp_path / "absent.json")
    malformed = tmp_path / "plan.json"
    malformed.write_text('{"full": true}')
    with pytest.raises(VerificationError, match="malformed"):
        load_plan(malformed)
    malformed.write_text(DOCS_ONLY.to_json())
    assert load_plan(malformed) == DOCS_ONLY


def test_the_cli_requires_a_plan_and_verifies_against_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit):
        main(["aggregate"])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(TUI_ONLY.to_json())
    monkeypatch.setenv("DESKTOP_GATE_RESULTS", json.dumps(_results(TUI_ONLY)))
    assert main(["aggregate", "--plan", str(plan_path)]) == 0
    assert "selected lanes: lint, core" in capsys.readouterr().out

    monkeypatch.setenv("DESKTOP_GATE_RESULTS", json.dumps(_results(FULL)))
    assert main(["aggregate", "--plan", str(plan_path)]) == 1
    assert "docs=" in capsys.readouterr().err


def test_the_cli_records_a_full_graph_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(FULL.to_json())
    monkeypatch.setenv(
        "DESKTOP_GATE_RESULTS", json.dumps(_results(FULL, changes="failure"))
    )
    assert main(["aggregate", "--plan", str(plan_path)]) == 0
    assert "fell back to the full graph" in capsys.readouterr().out


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

    # Every counter is compared, not only tests: a declared failure or skip
    # with no matching element is as inconsistent as a wrong test count.
    for counters in ({"tests": 2}, {"failures": 1}, {"skipped": 1}):
        _write_junit(report, **counters)
        with pytest.raises(VerificationError, match="counters do not match"):
            verify_mcp_junit(report)


def test_mcp_report_rejects_a_non_mcp_testcase(tmp_path: Path) -> None:
    report = tmp_path / "mcp.xml"
    _write_junit(report)
    report.write_text(
        report.read_text().replace("tests.test_mcp.", "tests.test_cache.")
    )
    with pytest.raises(VerificationError, match="outside"):
        verify_mcp_junit(report)


def test_junit_report_accepts_every_named_package_and_rejects_others(
    tmp_path: Path,
) -> None:
    """The lint job's harness report must come from tests/ci or tests/containers."""

    report = tmp_path / "harness.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" failures="0" errors="0" '
        'skipped="0"><testcase classname="tests.ci.test_ci_plan" name="a"/>'
        '<testcase classname="tests.containers.test_verify_expected_failure" '
        'name="b"/></testsuite></testsuites>'
    )
    verify_pytest_junit(report, ("tests.ci.", "tests.containers."), "harness")
    with pytest.raises(VerificationError, match="outside"):
        verify_pytest_junit(report, ("tests.ci.",), "harness")
    with pytest.raises(VerificationError, match="no classname prefix"):
        verify_pytest_junit(report, (), "harness")
    assert (
        main(
            [
                "junit-report",
                "--path",
                str(report),
                "--prefix",
                "tests.ci.",
                "--prefix",
                "tests.containers.",
            ]
        )
        == 0
    )
    assert main(["junit-report", "--path", str(report), "--prefix", "tests.ci."]) == 1
