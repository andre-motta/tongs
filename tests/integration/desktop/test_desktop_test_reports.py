"""Tests for bounded semantic verification of desktop test reports."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / ".github" / "scripts" / "desktop_test_reports.py"
FIXTURES = Path(__file__).with_name("fixtures")


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("desktop_test_reports", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_real_emitter_fixture_hashes_match_recorded_provenance() -> None:
    provenance = json.loads(
        (FIXTURES / "desktop_test_reports_provenance.json").read_text()
    )
    for emitter in ("pytest", "node"):
        for filename, expected_hash in provenance[emitter]["files"].items():
            assert hashlib.sha256(_fixture(filename)).hexdigest() == expected_hash


def _junit(
    testcase: str = '<testcase classname="tests.test_mcp.test_server" name="test_ok"/>',
    *,
    tests: str = "1",
    failures: str = "0",
    errors: str = "0",
    skipped: str = "0",
    root: str = "testsuites",
) -> bytes:
    suite = (
        f'<testsuite name="pytest" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}">{testcase}</testsuite>'
    )
    if root == "testsuite":
        return suite.encode()
    return f"<testsuites>{suite}</testsuites>".encode()


def _flat_tap(
    names: tuple[str, ...] = ("passes",),
    *,
    identifiers: tuple[int, ...] | None = None,
    statuses: tuple[str, ...] | None = None,
    directives: tuple[str, ...] | None = None,
    diagnostic_extra: tuple[str, ...] = (),
    plan: int | None = None,
    summary: dict[str, int] | None = None,
    final_newline: bool = True,
) -> bytes:
    if identifiers is None:
        identifiers = tuple(range(1, len(names) + 1))
    if statuses is None:
        statuses = ("ok",) * len(names)
    if directives is None:
        directives = ("",) * len(names)
    lines = ["TAP version 13"]
    for name, identifier, status, directive in zip(
        names, identifiers, statuses, directives, strict=True
    ):
        lines.extend(
            [
                f"# Subtest: {name}",
                f"{status} {identifier} - {name}{directive}",
                "  ---",
                "  duration_ms: 0.125",
                *[f"  {line}" for line in diagnostic_extra],
                "  type: 'test'",
                "  ...",
            ]
        )
    planned = len(names) if plan is None else plan
    lines.append(f"1..{planned}")
    expected_summary = {
        "tests": len(names),
        "suites": 0,
        "pass": len(names),
        "fail": 0,
        "cancelled": 0,
        "skipped": 0,
        "todo": 0,
    }
    if summary is not None:
        expected_summary.update(summary)
    lines.extend(f"# {name} {value}" for name, value in expected_summary.items())
    lines.append("# duration_ms 1.25")
    return ("\n".join(lines) + ("\n" if final_newline else "")).encode()


def test_pytest_junit_accepts_real_emitter_fixture_and_returns_frozen_counts() -> None:
    counts = verifier.verify_pytest_junit(
        _fixture("desktop_test_reports_pytest_pass.xml"),
        expected_classname_prefix="tests.integration.desktop.fixtures.",
    )
    assert counts == verifier.PytestJUnitCounts(
        tests=2, suites=1, failures=0, errors=0, skipped=0
    )
    with pytest.raises(FrozenInstanceError):
        counts.tests = 3


def test_pytest_junit_accepts_the_root_testsuite_emitter_form() -> None:
    assert verifier.verify_pytest_junit(_junit(root="testsuite")).tests == 1


def test_pytest_junit_rejects_real_nonpassing_emitter_fixture() -> None:
    with pytest.raises(verifier.ReportValidationError, match="non-passing"):
        verifier.verify_pytest_junit(
            _fixture("desktop_test_reports_pytest_nonpassing.xml")
        )


@pytest.mark.parametrize(
    ("outcome", "counter"),
    [
        ('<failure message="failed"/>', "failures"),
        ('<error message="error"/>', "errors"),
        ('<skipped message="skip"/>', "skipped"),
    ],
)
def test_pytest_junit_rejects_each_nonpassing_outcome(
    outcome: str, counter: str
) -> None:
    counters = {counter: "1"}
    testcase = (
        '<testcase classname="tests.test_mcp.test_server" name="test_bad">'
        f"{outcome}</testcase>"
    )
    with pytest.raises(verifier.ReportValidationError, match="non-passing"):
        verifier.verify_pytest_junit(_junit(testcase, **counters))


@pytest.mark.parametrize("counter", ["tests", "failures", "errors", "skipped"])
def test_pytest_junit_rejects_missing_counters(counter: str) -> None:
    report = _junit().replace(f' {counter}="1"'.encode(), b"")
    if counter != "tests":
        report = _junit().replace(f' {counter}="0"'.encode(), b"")
    with pytest.raises(verifier.ReportValidationError, match="missing"):
        verifier.verify_pytest_junit(report)


@pytest.mark.parametrize("value", ["-1", "+1", "one", "1.0", ""])
def test_pytest_junit_rejects_malformed_or_negative_counters(value: str) -> None:
    with pytest.raises(verifier.ReportValidationError, match="malformed"):
        verifier.verify_pytest_junit(_junit(tests=value))


def test_pytest_junit_rejects_counter_mismatch_and_empty_reports() -> None:
    with pytest.raises(verifier.ReportValidationError, match="do not match"):
        verifier.verify_pytest_junit(_junit(tests="2"))
    with pytest.raises(verifier.ReportValidationError, match="no tests"):
        verifier.verify_pytest_junit(_junit("", tests="0"))
    with pytest.raises(verifier.ReportValidationError, match="no test suites"):
        verifier.verify_pytest_junit(b"<testsuites/>")


def test_pytest_junit_rejects_duplicate_identity_and_wrong_prefix() -> None:
    testcase = '<testcase classname="tests.test_mcp.test_server" name="test_ok"/>'
    with pytest.raises(verifier.ReportValidationError, match="duplicate"):
        verifier.verify_pytest_junit(_junit(testcase * 2, tests="2"))
    with pytest.raises(verifier.ReportValidationError, match="outside expected prefix"):
        verifier.verify_pytest_junit(
            _junit(), expected_classname_prefix="tests.test_cache."
        )
    with pytest.raises(verifier.ReportValidationError, match="nonempty string"):
        verifier.verify_pytest_junit(_junit(), expected_classname_prefix="")


def test_pytest_junit_rejects_missing_and_ambiguous_testcase_identity_or_outcome() -> (
    None
):
    with pytest.raises(verifier.ReportValidationError, match="missing classname"):
        verifier.verify_pytest_junit(_junit('<testcase name="test_ok"/>'))
    testcase = (
        '<testcase classname="tests.test_mcp.test_server" name="test_bad">'
        "<failure/><error/></testcase>"
    )
    with pytest.raises(verifier.ReportValidationError, match="ambiguous"):
        verifier.verify_pytest_junit(_junit(testcase, failures="1", errors="1"))


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE testsuites [<!ENTITY hidden "pass">]>',
        '<!ENTITY hidden "pass">',
    ],
)
def test_pytest_junit_rejects_dtd_and_entity_declarations(declaration: str) -> None:
    with pytest.raises(
        verifier.ReportValidationError, match="declarations are forbidden"
    ):
        verifier.verify_pytest_junit((declaration + _junit().decode()).encode())


def test_pytest_junit_rejects_unsupported_or_misleading_nested_structure() -> None:
    nested = (
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="hidden" name="hidden"/></testsuite>'
    )
    with pytest.raises(verifier.ReportValidationError, match="unsupported"):
        verifier.verify_pytest_junit(_junit(f"<properties>{nested}</properties>"))
    with pytest.raises(verifier.ReportValidationError, match="unsupported"):
        verifier.verify_pytest_junit(b"<testsuites><testcase/></testsuites>")


def test_pytest_junit_rejects_malformed_non_utf8_and_non_utf8_declaration() -> None:
    with pytest.raises(verifier.ReportValidationError, match="malformed XML"):
        verifier.verify_pytest_junit(b"<testsuites>")
    with pytest.raises(verifier.ReportValidationError, match="not UTF-8"):
        verifier.verify_pytest_junit(b"\xff")
    report = b'<?xml version="1.0" encoding="iso-8859-1"?>' + _junit()
    with pytest.raises(verifier.ReportValidationError, match="specify UTF-8"):
        verifier.verify_pytest_junit(report)


def test_pytest_junit_enforces_byte_nesting_and_record_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(verifier.ReportValidationError, match="exceeds"):
        verifier.verify_pytest_junit(b"x" * (verifier.MAX_REPORT_BYTES + 1))
    nested = "<properties>" * (verifier.MAX_NESTING + 1)
    nested += "</properties>" * (verifier.MAX_NESTING + 1)
    with pytest.raises(verifier.ReportValidationError, match="nesting limit"):
        verifier.verify_pytest_junit(_junit(nested))
    monkeypatch.setattr(verifier, "MAX_RECORDS", 1)
    testcase = '<testcase classname="tests.test_mcp.test_server" name="test_ok"/>'
    with pytest.raises(verifier.ReportValidationError, match="record limit"):
        verifier.verify_pytest_junit(_junit(testcase * 2, tests="2"))


def test_node_tap_accepts_real_nested_emitter_fixture_and_returns_frozen_counts() -> (
    None
):
    counts = verifier.verify_node_tap(_fixture("desktop_test_reports_node_pass.tap"))
    assert counts == verifier.NodeTapCounts(
        tests=2,
        suites=2,
        passed=2,
        failed=0,
        cancelled=0,
        skipped=0,
        todo=0,
    )
    with pytest.raises(FrozenInstanceError):
        counts.passed = 1


def test_node_tap_rejects_real_nonpassing_emitter_fixture() -> None:
    with pytest.raises(verifier.ReportValidationError, match="failing assertion"):
        verifier.verify_node_tap(_fixture("desktop_test_reports_node_nonpassing.tap"))


@pytest.mark.parametrize(
    ("status", "directive", "message"),
    [
        ("not ok", "", "failing assertion"),
        ("ok", " # SKIP controlled", "skipped test"),
        ("ok", " # TODO controlled", "TODO test"),
    ],
)
def test_node_tap_rejects_fail_skip_and_todo_records(
    status: str, directive: str, message: str
) -> None:
    with pytest.raises(verifier.ReportValidationError, match=message):
        verifier.verify_node_tap(_flat_tap(statuses=(status,), directives=(directive,)))


def test_node_tap_rejects_cancelled_and_failure_records_hidden_by_success_summary() -> (
    None
):
    cancelled = _flat_tap(
        statuses=("not ok",),
        diagnostic_extra=("failureType: 'cancelledByParent'",),
        summary={"pass": 0, "cancelled": 1},
    )
    with pytest.raises(verifier.ReportValidationError, match="failing assertion"):
        verifier.verify_node_tap(cancelled)
    hidden_failure = _flat_tap(statuses=("not ok",))
    with pytest.raises(verifier.ReportValidationError, match="failing assertion"):
        verifier.verify_node_tap(hidden_failure)
    cancelled_summary = _flat_tap(summary={"pass": 0, "cancelled": 1})
    with pytest.raises(verifier.ReportValidationError, match="counters disagree"):
        verifier.verify_node_tap(cancelled_summary)


def test_node_tap_treats_yaml_diagnostics_as_data() -> None:
    report = _flat_tap(
        diagnostic_extra=(
            "message: 'literal not ok and Bail out! text'",
            "error: |-",
            "  not ok 999 - diagnostic data",
            "  # tests 999",
            "  1..999",
        )
    )
    assert verifier.verify_node_tap(report).tests == 1


@pytest.mark.parametrize(
    "outside_line",
    [
        "not ok 999 - swallowed failure",
        "Bail out! swallowed bailout",
        "# fail 99",
        "  not ok 999 - field-depth plain scalar",
    ],
)
def test_node_tap_rejects_out_of_scope_lines_inside_diagnostic_envelope(
    outside_line: str,
) -> None:
    report = _flat_tap().replace(b"  ---\n", f"  ---\n{outside_line}\n".encode(), 1)
    with pytest.raises(verifier.ReportValidationError, match="diagnostic contains"):
        verifier.verify_node_tap(report)


def test_node_tap_rejects_parent_stream_record_inside_nested_diagnostic() -> None:
    report = _fixture("desktop_test_reports_node_pass.tap").replace(
        b"      ---\n",
        b"      ---\n    not ok 999 - swallowed parent assertion\n",
        1,
    )
    with pytest.raises(verifier.ReportValidationError, match="diagnostic contains"):
        verifier.verify_node_tap(report)


def test_node_tap_requires_node_escaped_hashes_in_test_names() -> None:
    with pytest.raises(verifier.ReportValidationError, match="unescaped hash"):
        verifier.verify_node_tap(_flat_tap(("literal # skip",)))
    assert verifier.verify_node_tap(_flat_tap((r"literal \# skip",))).tests == 1


def test_node_tap_rejects_bailout_and_wrong_or_missing_version() -> None:
    bailout = _flat_tap().replace(b"# Subtest: passes", b"Bail out! hidden")
    with pytest.raises(verifier.ReportValidationError, match="bailout"):
        verifier.verify_node_tap(bailout)
    with pytest.raises(verifier.ReportValidationError, match="TAP version 13"):
        verifier.verify_node_tap(_flat_tap().replace(b"version 13", b"version 14"))
    with pytest.raises(verifier.ReportValidationError, match="TAP version 13"):
        verifier.verify_node_tap(_flat_tap().split(b"\n", 1)[1])


def test_node_tap_rejects_duplicate_out_of_order_ids_and_plan_mismatch() -> None:
    with pytest.raises(
        verifier.ReportValidationError, match="duplicate or out of order"
    ):
        verifier.verify_node_tap(_flat_tap(("one", "two"), identifiers=(1, 1)))
    with pytest.raises(
        verifier.ReportValidationError, match="duplicate or out of order"
    ):
        verifier.verify_node_tap(_flat_tap(("one",), identifiers=(2,)))
    with pytest.raises(verifier.ReportValidationError, match="plan does not match"):
        verifier.verify_node_tap(_flat_tap(plan=2))


def test_node_tap_rejects_missing_plan_summary_and_inconsistent_summary() -> None:
    without_plan = _flat_tap().replace(b"1..1\n# tests", b"# tests")
    with pytest.raises(verifier.ReportValidationError, match="unsupported content"):
        verifier.verify_node_tap(without_plan)
    truncated_summary = _flat_tap().split(b"# cancelled", 1)[0]
    with pytest.raises(verifier.ReportValidationError, match="missing"):
        verifier.verify_node_tap(truncated_summary)
    with pytest.raises(verifier.ReportValidationError, match="counters disagree"):
        verifier.verify_node_tap(_flat_tap(summary={"tests": 2}))


def test_node_tap_rejects_truncation_and_content_after_summary() -> None:
    with pytest.raises(verifier.ReportValidationError, match="final newline"):
        verifier.verify_node_tap(_flat_tap(final_newline=False))
    with pytest.raises(
        verifier.ReportValidationError, match="diagnostic block is truncated"
    ):
        verifier.verify_node_tap(_flat_tap().split(b"  ...", 1)[0])
    with pytest.raises(verifier.ReportValidationError, match="content after"):
        verifier.verify_node_tap(_flat_tap() + b"# forged success\n")


def test_node_tap_rejects_missing_or_ambiguous_diagnostic_fields() -> None:
    missing_type = _flat_tap().replace(b"  type: 'test'\n", b"")
    with pytest.raises(verifier.ReportValidationError, match="one test type"):
        verifier.verify_node_tap(missing_type)
    duplicate_type = _flat_tap().replace(
        b"  type: 'test'\n", b"  type: 'test'\n  type: 'suite'\n"
    )
    with pytest.raises(verifier.ReportValidationError, match="repeats 'type'"):
        verifier.verify_node_tap(duplicate_type)
    missing_duration = _flat_tap().replace(b"  duration_ms: 0.125\n", b"")
    with pytest.raises(verifier.ReportValidationError, match="one duration"):
        verifier.verify_node_tap(missing_duration)


def test_node_tap_rejects_name_and_test_suite_type_mismatches() -> None:
    renamed = _flat_tap().replace(b"ok 1 - passes", b"ok 1 - renamed")
    with pytest.raises(verifier.ReportValidationError, match="names do not match"):
        verifier.verify_node_tap(renamed)
    wrong_type = _flat_tap().replace(b"type: 'test'", b"type: 'suite'")
    with pytest.raises(verifier.ReportValidationError, match="not declared as a test"):
        verifier.verify_node_tap(wrong_type)
    nested = _fixture("desktop_test_reports_node_pass.tap").replace(
        b"type: 'suite'", b"type: 'test'", 1
    )
    with pytest.raises(verifier.ReportValidationError, match="not declared as a suite"):
        verifier.verify_node_tap(nested)


def test_node_tap_rejects_empty_report_and_malformed_final_values() -> None:
    empty = _flat_tap((), summary={"tests": 0, "pass": 0})
    with pytest.raises(verifier.ReportValidationError, match="contains no tests"):
        verifier.verify_node_tap(empty)
    malformed = _flat_tap().replace(b"# tests 1", b"# tests -1")
    with pytest.raises(verifier.ReportValidationError, match="malformed"):
        verifier.verify_node_tap(malformed)
    duration = _flat_tap().replace(b"# duration_ms 1.25", b"# duration_ms NaN")
    with pytest.raises(verifier.ReportValidationError, match="duration is malformed"):
        verifier.verify_node_tap(duration)


def test_node_tap_enforces_byte_line_record_and_nesting_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(verifier.ReportValidationError, match="exceeds"):
        verifier.verify_node_tap(b"x" * (verifier.MAX_REPORT_BYTES + 1))
    with pytest.raises(verifier.ReportValidationError, match="not UTF-8"):
        verifier.verify_node_tap(b"\xff")

    monkeypatch.setattr(verifier, "MAX_LINES", 3)
    with pytest.raises(verifier.ReportValidationError, match="line limit"):
        verifier.verify_node_tap(_flat_tap())
    monkeypatch.setattr(verifier, "MAX_LINES", 100_000)
    monkeypatch.setattr(verifier, "MAX_LINE_BYTES", 8)
    with pytest.raises(verifier.ReportValidationError, match="overlong line"):
        verifier.verify_node_tap(_flat_tap())
    monkeypatch.setattr(verifier, "MAX_LINE_BYTES", 256 * 1024)
    monkeypatch.setattr(verifier, "MAX_RECORDS", 1)
    with pytest.raises(verifier.ReportValidationError, match="record limit"):
        verifier.verify_node_tap(_fixture("desktop_test_reports_node_pass.tap"))


def test_node_tap_enforces_suite_nesting_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verifier, "MAX_NESTING", 1)
    with pytest.raises(verifier.ReportValidationError, match="nesting limit"):
        verifier.verify_node_tap(_fixture("desktop_test_reports_node_pass.tap"))
