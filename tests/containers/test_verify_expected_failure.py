"""Tests for strict expected-failure evidence verification."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from . import probe
from .probe import (
    EXPECTED_PLUGIN_EVIDENCE,
    SMOKE_TESTS,
    StepResult,
    missing_smoke_tests,
    skipped_smoke_tests,
    smoke_tests_without_results,
    validate_plugin_evidence,
)
from .verify_expected_failure import (
    EXPECTED_STEPS,
    FAILURE_MESSAGE,
    FAILURE_TEST_NAME,
    VerificationError,
    main,
    verify_expected_failure,
)

REPOSITORY = Path(__file__).parents[2]


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
    _write_junit(
        output_dir / "deliberate-failure.junit.xml",
        tests=1,
        failures=1,
        deliberate=True,
    )


def test_accepts_only_the_intended_assertion_failure(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)

    verify_expected_failure(tmp_path, 1)
    assert main(["--output-dir", str(tmp_path), "--actual-status", "1"]) == 0


def test_rejects_setup_failure(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["steps"] = [{"name": "build-core-wheel", "returncode": 1}]
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
    summary["steps"].insert(0, {"name": "smoke-tests", "returncode": 1})
    summary["failed_steps"] = ["smoke-tests", "deliberate-failure"]
    (tmp_path / "summary.json").write_text(json.dumps(summary))

    with pytest.raises(VerificationError, match="only deliberate-failure"):
        verify_expected_failure(tmp_path, 1)


def test_rejects_unexpected_step(tmp_path: Path) -> None:
    _valid_evidence(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    summary["steps"].insert(0, {"name": "unexpected-setup", "returncode": 0})
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
    evidence["desktop_states"]["mcp"] = "discovered"

    with pytest.raises(ValueError, match="unexpected compatibility evidence"):
        validate_plugin_evidence(json.dumps(evidence))


def test_every_smoke_test_path_exists() -> None:
    assert missing_smoke_tests(REPOSITORY) == []


def _write_smoke_junit(
    path: Path, classnames: list[str], outcomes: dict[str, str] | None = None
) -> None:
    """One testcase per classname; ``outcomes`` adds a non-passing child."""

    suite = ET.Element("testsuite")
    for classname in classnames:
        case = ET.SubElement(suite, "testcase", {"classname": classname, "name": "t"})
        outcome = (outcomes or {}).get(classname)
        if outcome is not None:
            ET.SubElement(case, outcome, {"message": "mcp not installed"})
    ET.ElementTree(suite).write(path, encoding="unicode")


def _smoke_classnames() -> list[str]:
    return [
        f"{path.removesuffix('.py').replace('/', '.')}.TestCase" for path in SMOKE_TESTS
    ]


def test_smoke_report_accepts_a_testcase_from_every_path(tmp_path: Path) -> None:
    report = tmp_path / "smoke.junit.xml"
    _write_smoke_junit(
        report,
        [
            f"{path.removesuffix('.py').replace('/', '.')}.TestCase"
            for path in SMOKE_TESTS
        ],
    )

    assert smoke_tests_without_results(report) == []


def test_smoke_report_names_a_path_that_ran_nothing(tmp_path: Path) -> None:
    report = tmp_path / "smoke.junit.xml"
    modules = [path.removesuffix(".py").replace("/", ".") for path in SMOKE_TESTS]
    _write_smoke_junit(report, [*modules[1:], f"{modules[0]}_extra.TestCase"])

    assert smoke_tests_without_results(report) == [SMOKE_TESTS[0]]


def test_smoke_report_rejects_malformed_junit(tmp_path: Path) -> None:
    report = tmp_path / "smoke.junit.xml"
    report.write_text("not XML")

    with pytest.raises(ValueError, match="smoke report is unreadable"):
        smoke_tests_without_results(report)


MCP_SMOKE = "tests/test_mcp/test_server.py"


@pytest.mark.parametrize("outcome", ["skipped", "error", "failure"])
def test_a_smoke_path_whose_only_testcase_did_not_pass_ran_nothing(
    tmp_path: Path, outcome: str
) -> None:
    """The MCP server tests self-skip without ``mcp``; a skipped, erroring or
    failing testcase must not count as that path's result."""

    report = tmp_path / "smoke.junit.xml"
    classnames = _smoke_classnames()
    mcp = classnames[SMOKE_TESTS.index(MCP_SMOKE)]
    _write_smoke_junit(report, classnames, {mcp: outcome})

    assert smoke_tests_without_results(report) == [MCP_SMOKE]
    assert skipped_smoke_tests(report) == (
        [f"{mcp}::t"] if outcome == "skipped" else []
    )


def test_the_smoke_step_fails_on_a_skipped_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip-only MCP smoke run exits 0 under pytest, so the step itself must
    turn the skip into a failure."""

    classnames = _smoke_classnames()
    mcp = classnames[SMOKE_TESTS.index(MCP_SMOKE)]

    def fake_run(name: str, command: list[str], **_: object) -> StepResult:
        _write_smoke_junit(
            tmp_path / "smoke-tests.junit.xml", classnames, {mcp: "skipped"}
        )
        (tmp_path / f"{name}.stderr.txt").write_text("")
        return StepResult(
            name, command, 0, 0.0, f"{name}.stdout.txt", f"{name}.stderr.txt"
        )

    monkeypatch.setattr(probe, "OUTPUT", tmp_path)
    monkeypatch.setattr(probe, "SOURCE", REPOSITORY)
    monkeypatch.setattr(probe, "_run", fake_run)

    result = probe._smoke_tests(Path("/unused/python"))

    assert result.returncode == 1
    stderr = (tmp_path / result.stderr).read_text()
    assert "Smoke tests were skipped" in stderr
    assert f"{mcp}::t" in stderr
