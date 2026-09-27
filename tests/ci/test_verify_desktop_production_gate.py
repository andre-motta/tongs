"""Prove the production gate rejects every incomplete or untrustworthy set.

The fixtures here are synthetic on purpose.  A passing case proves only that
the consumer accepts a well-formed complete set; the negative cases carry the
value, because each one reproduces a way a real workflow could otherwise report
a false green: a failed job, a skipped job, a cancelled job, a missing result,
a stale receipt from another run, and an injected receipt or report.  The
plan-aware cases prove that a skip passes only for a lane the effective plan
deselected, including the partial desktop selection without packaging and
the desktop source selection that runs the archive jobs without the RPM
lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.ci.verify_desktop_production_gate import (
    ARTIFACT_LIFECYCLE,
    CI_PLAN,
    GPU_GATE_CHECK_ID,
    NODE_TAP,
    PYTEST_JUNIT,
    REQUIRED_CHECKS,
    REQUIRED_CI_JOBS,
    REQUIRED_PRODUCTION_JOBS,
    GateIdentity,
    GateVerificationError,
    load_plan,
    main,
    verify_check_set,
    verify_production_gate,
    verify_production_results,
)

COMMIT = "1" * 40
TREE = "2" * 40
PULL_REQUEST_HEAD = "a" * 40
PULL_REQUEST_BASE = "b" * 40
EVENT = "pull_request"
REPOSITORY = "andre-motta/tongs"
RUN_ID = "34274245440"
ATTEMPT = 1
ENVIRONMENT = "github-hosted-ubuntu-24.04"
PROVENANCE = "hosted"

FULL = CI_PLAN.full_plan(COMMIT, "event 'push' is not pull_request")


def _plan(*lanes: str) -> Any:
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

IDENTITY = GateIdentity(
    commit=COMMIT,
    tree=TREE,
    repository=REPOSITORY,
    run_id=RUN_ID,
    attempt=ATTEMPT,
    environment=ENVIRONMENT,
    provenance=PROVENANCE,
    event=EVENT,
    pull_request_head=PULL_REQUEST_HEAD,
    pull_request_base=PULL_REQUEST_BASE,
)


def _junit(classname: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="1" failures="0" '
        'errors="0" skipped="0">'
        f'<testcase classname="{classname}" name="test_case" time="0.01"/>'
        "</testsuite></testsuites>"
    ).encode()


def _tap(status: str = "ok", directive: str = "") -> bytes:
    """Build one Node test-runner TAP report in the shape the parser accepts."""

    name = "production shell case"
    lines = [
        "TAP version 13",
        f"# Subtest: {name}",
        f"{status} 1 - {name}{directive}",
        "  ---",
        "  duration_ms: 0.125",
        "  type: 'test'",
        "  ...",
        "1..1",
        "# tests 1",
        "# suites 0",
        "# pass 1",
        "# fail 0",
        "# cancelled 0",
        "# skipped 0",
        "# todo 0",
        "# duration_ms 1.25",
    ]
    return ("\n".join(lines) + "\n").encode()


def _lifecycle(check_id: str, *, result: str = "pass") -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "check_id": check_id,
        "result": result,
        "stages": [
            {"name": name, "result": "pass"} for name in _check(check_id).stages
        ],
    }
    if _check(check_id).requires_source_context:
        document["source_context"] = {
            "event": EVENT,
            "pull_request_head": PULL_REQUEST_HEAD,
            "pull_request_base": PULL_REQUEST_BASE,
        }
    return (json.dumps(document, sort_keys=True) + "\n").encode()


def _report_bytes(check_id: str, path: str, report_format: str) -> bytes:
    if report_format == NODE_TAP:
        return _tap()
    if report_format == ARTIFACT_LIFECYCLE:
        return _lifecycle(check_id)
    if "mcp" in path:
        return _junit("tests.test_mcp.test_server")
    if "plugin-example" in path:
        return _junit("examples.desktop-plugin.tests.test_provider")
    if "integration-contracts" in path:
        return _junit("tests.integration.desktop.test_sbom_evidence")
    if "packaging-contracts" in path:
        return _junit("tests.packaging.rpm.desktop.test_contract")
    if "native-payload" in path:
        return _junit(
            "tests.integration.desktop.test_native_payload_acceptance.TestPayload"
        )
    return _junit("tests.test_example")


def _prepared_inputs(commit: str = COMMIT, mode: str = "exact") -> bytes:
    """Mirror the shape ``packaging/rpm/desktop/prepare_sources.py`` writes."""

    document = {
        "schema_version": 1,
        "source": {"commit": commit, "pep440_version": "0.4.2.dev327"},
        "desktop": {
            "accepted_source_commit": commit,
            "file_count": 80,
            "pairing_mode": mode,
        },
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _write(path: Path, payload: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _build_evidence(root: Path) -> Path:
    """Create one complete, well-formed evidence root for every check."""

    root.mkdir(parents=True, exist_ok=True)
    for check in REQUIRED_CHECKS:
        directory = root / check.evidence_directory
        reports = []
        for report in check.reports:
            payload = _report_bytes(check.check_id, report.path, report.report_format)
            identity = _write(directory / report.path, payload)
            reports.append(
                {
                    "path": report.path,
                    "size": identity["size"],
                    "sha256": identity["sha256"],
                    "format": report.report_format,
                }
            )
        inputs = []
        if check.exact_pairing_input is not None:
            payload = _prepared_inputs()
            identity = _write(directory / check.exact_pairing_input, payload)
            inputs.append(
                {
                    "path": check.exact_pairing_input,
                    "sha256": identity["sha256"],
                }
            )
        receipt = {
            "schema_version": 1,
            "check_id": check.check_id,
            "source": {"commit": COMMIT, "tree": TREE},
            "execution": {
                "repository": REPOSITORY,
                "run_id": RUN_ID,
                "attempt": ATTEMPT,
                "environment": ENVIRONMENT,
                "provenance": PROVENANCE,
            },
            "result": "success",
            "reports": reports,
            "artifacts": [],
            "inputs": inputs,
        }
        _write_receipt(directory / check.receipt_name, receipt)
    return root


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(receipt, sort_keys=True) + "\n").encode())


def _read_receipt(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes().decode())


def _results(names: frozenset[str], result: str = "success") -> str:
    return json.dumps({name: {"result": result, "outputs": {}} for name in names})


def _ci_results(**overrides: Any) -> str:
    payload = {name: {"result": "success", "outputs": {}} for name in REQUIRED_CI_JOBS}
    payload.update(overrides)
    return json.dumps(payload)


def _production_results(**overrides: Any) -> str:
    payload = {
        name: {"result": "success", "outputs": {}} for name in REQUIRED_PRODUCTION_JOBS
    }
    payload.update(overrides)
    return json.dumps(payload)


def _check(check_id: str):
    return next(item for item in REQUIRED_CHECKS if item.check_id == check_id)


@pytest.fixture()
def evidence(tmp_path: Path) -> Path:
    return _build_evidence(tmp_path / "gate-evidence")


def test_configuration_covers_every_required_production_job() -> None:
    produced = {
        check.job for check in REQUIRED_CHECKS if check.workflow == "desktop-production"
    }
    # source-identity and archive publish inputs consumed by later jobs and are
    # therefore required as job results without owning a receipt of their own.
    assert produced | {"source-identity", "archive"} == set(REQUIRED_PRODUCTION_JOBS)
    assert GPU_GATE_CHECK_ID not in {check.check_id for check in REQUIRED_CHECKS}


def test_gate_accepts_a_complete_successful_run(evidence: Path) -> None:
    verified = verify_production_gate(
        ci_results=_ci_results(),
        production_results=_production_results(),
        evidence_root=evidence,
        identity=IDENTITY,
        plan=FULL,
    )
    assert set(verified) == {check.check_id for check in REQUIRED_CHECKS}


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled", "neutral", None])
def test_gate_rejects_every_non_success_ci_result(evidence: Path, result: Any) -> None:
    with pytest.raises(GateVerificationError, match="desktop-production"):
        verify_production_gate(
            ci_results=_ci_results(**{"desktop-production": {"result": result}}),
            production_results=_production_results(),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


# The gate judges every production job alike, so one job carries every result
# and a second job the skip that no receipt-bearing job may report.
@pytest.mark.parametrize(
    ("job", "result"),
    [
        *(
            ("rpm-lifecycle", result)
            for result in ("failure", "skipped", "cancelled", "neutral", None)
        ),
        ("archive", "skipped"),
    ],
)
def test_gate_rejects_every_non_success_production_result(
    evidence: Path, job: str, result: Any
) -> None:
    with pytest.raises(GateVerificationError, match=job):
        verify_production_gate(
            ci_results=_ci_results(),
            production_results=_production_results(**{job: {"result": result}}),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


def test_gate_rejects_a_missing_production_job_result(evidence: Path) -> None:
    payload = json.loads(_production_results())
    payload.pop("rpm-lifecycle")
    with pytest.raises(GateVerificationError, match="rpm-lifecycle"):
        verify_production_gate(
            ci_results=_ci_results(),
            production_results=json.dumps(payload),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


def test_gate_rejects_an_injected_production_job_result(evidence: Path) -> None:
    with pytest.raises(GateVerificationError, match="always-green"):
        verify_production_gate(
            ci_results=_ci_results(),
            production_results=_production_results(
                **{"always-green": {"result": "success"}}
            ),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


@pytest.mark.parametrize("raw", ["{broken", "null"])
def test_gate_rejects_malformed_result_payloads(evidence: Path, raw: str) -> None:
    with pytest.raises(GateVerificationError):
        verify_production_gate(
            ci_results=raw,
            production_results=_production_results(),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


def test_gate_rejects_a_malformed_job_entry(evidence: Path) -> None:
    with pytest.raises(GateVerificationError, match="core=malformed"):
        verify_production_gate(
            ci_results=_ci_results(core=None),
            production_results=_production_results(),
            evidence_root=evidence,
            identity=IDENTITY,
            plan=FULL,
        )


def test_gate_rejects_a_missing_check_directory(evidence: Path) -> None:
    target = evidence / _check("desktop-rpm-lifecycle").evidence_directory
    for path in sorted(target.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    target.rmdir()
    with pytest.raises(GateVerificationError, match="missing=..desktop-rpm-lifecycle"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_an_injected_extra_check_directory(evidence: Path) -> None:
    (evidence / "desktop-bonus-green").mkdir()
    with pytest.raises(GateVerificationError, match="injected=..desktop-bonus-green"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_receipt_claiming_the_reserved_gpu_gate(
    evidence: Path,
) -> None:
    (evidence / GPU_GATE_CHECK_ID).mkdir()
    with pytest.raises(GateVerificationError, match="reserved physical GPU gate"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "3" * 40),
        ("tree", "4" * 40),
        ("run_id", "34274245441"),
        ("repository", "someone-else/tongs"),
        ("environment", "self-hosted"),
    ],
)
def test_gate_rejects_stale_or_foreign_receipt_identity(
    evidence: Path, field: str, value: Any
) -> None:
    identity = replace(IDENTITY, **{field: value})
    with pytest.raises(GateVerificationError, match="rejected"):
        verify_check_set(evidence, identity)


def _set_receipt_attempt(evidence: Path, check_id: str, attempt: Any) -> None:
    check = _check(check_id)
    path = evidence / check.evidence_directory / check.receipt_name
    receipt = _read_receipt(path)
    receipt["execution"]["attempt"] = attempt
    _write_receipt(path, receipt)


def test_gate_accepts_receipts_from_every_attempt_after_a_partial_rerun(
    evidence: Path,
) -> None:
    """Attempt 3 reran only the RPM lane; attempt 2 reran only the TAP lane.

    Every other lane keeps the receipt its attempt 1 produced, because gate
    artifact names omit the attempt and nothing reran them.
    """

    _set_receipt_attempt(evidence, "desktop-production-tap", 2)
    _set_receipt_attempt(evidence, "desktop-rpm-lifecycle", 3)
    verified = verify_production_gate(
        ci_results=_ci_results(),
        production_results=_production_results(),
        evidence_root=evidence,
        identity=replace(IDENTITY, attempt=3),
        plan=FULL,
    )
    assert set(verified) == {check.check_id for check in REQUIRED_CHECKS}


def test_gate_rejects_a_receipt_from_a_later_attempt_than_its_own(
    evidence: Path,
) -> None:
    _set_receipt_attempt(evidence, "core-python-3.13", 2)
    with pytest.raises(GateVerificationError, match="later than this run's attempt 1"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize("attempt", [0, -1, True, "1", 1.0, None])
def test_gate_rejects_a_malformed_receipt_attempt(evidence: Path, attempt: Any) -> None:
    _set_receipt_attempt(evidence, "desktop-installed-core", attempt)
    with pytest.raises(GateVerificationError, match="desktop-installed-core"):
        verify_check_set(evidence, replace(IDENTITY, attempt=2))


@pytest.mark.parametrize(
    ("ci_job", "production_job"),
    [
        ("core", None),
        ("desktop-production", "rpm-lifecycle"),
        ("desktop-production", "desktop-tap"),
    ],
)
def test_a_lane_still_failing_after_a_partial_rerun_fails_the_gate(
    evidence: Path, ci_job: str, production_job: str | None
) -> None:
    """A passing receipt from attempt 1 never masks the lane's latest failure.

    The lane uploaded its evidence in attempt 1 and then failed; its rerun in
    attempt 2 failed before uploading, so the attempt 1 receipt is still the
    one under the lane's artifact name.  The job result is the latest attempt's
    and rejects the gate.
    """

    production = {production_job: {"result": "failure"}} if production_job else {}
    with pytest.raises(GateVerificationError, match="did not match the effective plan"):
        verify_production_gate(
            ci_results=_ci_results(**{ci_job: {"result": "failure"}}),
            production_results=_production_results(**production),
            evidence_root=evidence,
            identity=replace(IDENTITY, attempt=2),
            plan=FULL,
        )


def test_gate_rejects_a_receipt_whose_check_id_was_swapped(evidence: Path) -> None:
    check = _check("desktop-archive-sbom")
    path = evidence / check.evidence_directory / check.receipt_name
    receipt = _read_receipt(path)
    receipt["check_id"] = "desktop-archive-lifecycle"
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="desktop-archive-sbom"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_failed_receipt_result(evidence: Path) -> None:
    check = _check("desktop-installed-core")
    path = evidence / check.evidence_directory / check.receipt_name
    receipt = _read_receipt(path)
    receipt["result"] = "failure"
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="rather than success"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_an_injected_report_added_to_a_receipt(evidence: Path) -> None:
    check = _check("desktop-production-tap")
    directory = evidence / check.evidence_directory
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    payload = _junit("tests.test_example")
    identity = _write(directory / "reports/extra.junit.xml", payload)
    receipt["reports"].append(
        {
            "path": "reports/extra.junit.xml",
            "size": identity["size"],
            "sha256": identity["sha256"],
            "format": PYTEST_JUNIT,
        }
    )
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="unexpected=..reports/extra"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_dropped_report(evidence: Path) -> None:
    check = _check("desktop-production-tap")
    directory = evidence / check.evidence_directory
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    receipt["reports"] = [
        entry
        for entry in receipt["reports"]
        if entry["path"] != "reports/plugin-example.junit.xml"
    ]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="missing=..reports/plugin-example"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    ("report", "classname"),
    [
        ("reports/integration-contracts.junit.xml", "tests.packaging.rpm.x"),
        ("reports/packaging-contracts.junit.xml", "tests.integration.desktop.x"),
        ("reports/plugin-example.junit.xml", "tests.plugins.test_desktop_contract"),
    ],
)
def test_gate_rejects_a_rehomed_suite_report_from_another_suite(
    evidence: Path, report: str, classname: str
) -> None:
    """Each re-homed suite's report is bound to its own directory, so a
    passing report of a different suite cannot stand in for it."""

    check = next(
        item
        for item in REQUIRED_CHECKS
        if any(expected.path == report for expected in item.reports)
    )
    directory = evidence / check.evidence_directory
    identity = _write(directory / report, _junit(classname))
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == report:
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="did not pass"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    ("counter", "outcome"),
    [
        ("failures", '<failure message="boom"/>'),
    ],
)
def test_gate_rejects_a_skipped_or_failed_python_report(
    evidence: Path, counter: str, outcome: str
) -> None:
    check = _check("core-python-3.13")
    directory = evidence / check.evidence_directory
    payload = (
        (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<testsuites><testsuite name="pytest" tests="1" failures="0" '
            f'errors="0" skipped="0">'
            '<testcase classname="tests.test_mcp.test_server" name="test_case">'
            f"{outcome}</testcase></testsuite></testsuites>"
        )
        .replace(f'{counter}="0"', f'{counter}="1"')
        .encode()
    )
    identity = _write(directory / "reports/mcp.junit.xml", payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/mcp.junit.xml":
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="did not pass"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_an_mcp_report_from_outside_the_mcp_suite(evidence: Path) -> None:
    check = _check("core-python-3.12")
    directory = evidence / check.evidence_directory
    payload = _junit("tests.test_cache.test_cache")
    identity = _write(directory / "reports/mcp.junit.xml", payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/mcp.junit.xml":
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="did not pass"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    ("status", "directive"),
    [
        ("not ok", ""),
    ],
)
def test_gate_rejects_a_failed_skipped_or_todo_tap_report(
    evidence: Path, status: str, directive: str
) -> None:
    check = _check("desktop-production-tap")
    directory = evidence / check.evidence_directory
    payload = _tap(status, directive)
    identity = _write(directory / "reports/desktop-shell.tap", payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/desktop-shell.tap":
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="did not pass"):
        verify_check_set(evidence, IDENTITY)


def _replace_lifecycle(evidence: Path, check_id: str, document: dict[str, Any]) -> None:
    check = _check(check_id)
    if check.requires_source_context and "source_context" not in document:
        document = {
            **document,
            "source_context": {
                "event": EVENT,
                "pull_request_head": PULL_REQUEST_HEAD,
                "pull_request_base": PULL_REQUEST_BASE,
            },
        }
    directory = evidence / check.evidence_directory
    report_path = check.reports[0].path
    payload = (json.dumps(document, sort_keys=True) + "\n").encode()
    identity = _write(directory / report_path, payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == report_path:
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)


def test_gate_rejects_a_failed_lifecycle_result(evidence: Path) -> None:
    _replace_lifecycle(
        evidence,
        "desktop-archive-lifecycle",
        {
            "check_id": "desktop-archive-lifecycle",
            "result": "fail",
            "stages": [{"name": "source-admission", "result": "pass"}],
        },
    )
    with pytest.raises(GateVerificationError, match="rather than pass"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_failed_lifecycle_stage(evidence: Path) -> None:
    # Every configured stage is present, so only the failed stage can reject.
    stages = [
        {"name": name, "result": "fail" if name == "upgrade-byte-parity" else "pass"}
        for name in _check("desktop-rpm-lifecycle").stages
    ]
    _replace_lifecycle(
        evidence,
        "desktop-rpm-lifecycle",
        {"check_id": "desktop-rpm-lifecycle", "result": "pass", "stages": stages},
    )
    with pytest.raises(
        GateVerificationError,
        match="'upgrade-byte-parity' reports 'fail' rather than pass",
    ):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_lifecycle_report_without_stages(evidence: Path) -> None:
    _replace_lifecycle(
        evidence,
        "desktop-archive-sbom",
        {"check_id": "desktop-archive-sbom", "result": "pass"},
    )
    with pytest.raises(GateVerificationError, match="records no stages"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_lifecycle_report_for_another_check(evidence: Path) -> None:
    # A valid report in every other respect, so only the check id can reject.
    stages = [
        {"name": name, "result": "pass"}
        for name in _check("desktop-archive-sbom").stages
    ]
    _replace_lifecycle(
        evidence,
        "desktop-archive-sbom",
        {"check_id": "another-check", "result": "pass", "stages": stages},
    )
    with pytest.raises(GateVerificationError, match="lifecycle report names"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_repeated_lifecycle_stage_name(evidence: Path) -> None:
    _replace_lifecycle(
        evidence,
        "desktop-installed-core",
        {
            "check_id": "desktop-installed-core",
            "result": "pass",
            "stages": [
                {"name": "source-admission", "result": "pass"},
                {"name": "source-admission", "result": "pass"},
            ],
        },
    )
    with pytest.raises(GateVerificationError, match="repeats a stage name"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_declared_format_that_hides_the_real_parser(
    evidence: Path,
) -> None:
    check = _check("desktop-native-payload-fixture")
    directory = evidence / check.evidence_directory
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/native-payload.junit.xml":
            entry["format"] = ARTIFACT_LIFECYCLE
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="rejected|declares format"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_an_evidence_root_that_is_not_a_directory(tmp_path: Path) -> None:
    plain = tmp_path / "evidence"
    plain.write_text("not a directory\n")
    with pytest.raises(GateVerificationError, match="real directory"):
        verify_check_set(plain, IDENTITY)


def test_gate_rejects_a_loose_file_beside_the_check_directories(
    evidence: Path,
) -> None:
    (evidence / "summary.txt").write_text("all green\n")
    with pytest.raises(GateVerificationError, match="non-directory entry"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_report_that_relabels_a_synthetic_merge_as_the_head(
    evidence: Path,
) -> None:
    """A stage may not claim the pull request head as what it checked out."""

    _replace_lifecycle(
        evidence,
        "desktop-rpm-lifecycle",
        {
            "check_id": "desktop-rpm-lifecycle",
            "result": "pass",
            "stages": [{"name": "clean-install", "result": "pass"}],
            "source_context": {
                "event": EVENT,
                "pull_request_head": COMMIT,
                "pull_request_base": PULL_REQUEST_BASE,
            },
        },
    )
    with pytest.raises(GateVerificationError, match="source context"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_pull_request_identity_bound_to_its_own_head(
    evidence: Path,
) -> None:
    identity = replace(IDENTITY, pull_request_head=COMMIT)
    with pytest.raises(GateVerificationError, match="synthetic merge commit"):
        verify_check_set(evidence, identity)


@pytest.mark.parametrize("field", ["event", "pull_request_base"])
def test_gate_rejects_a_source_context_from_another_run(
    evidence: Path, field: str
) -> None:
    replacement = {
        "event": "push",
        "pull_request_head": "c" * 40,
        "pull_request_base": "d" * 40,
    }
    identity = replace(IDENTITY, **{field: replacement[field]})
    with pytest.raises(GateVerificationError, match="source context|synthetic"):
        verify_check_set(evidence, identity)


def test_gate_rejects_a_missing_source_context(evidence: Path) -> None:
    _replace_lifecycle(
        evidence,
        "desktop-installed-core",
        {
            "check_id": "desktop-installed-core",
            "result": "pass",
            "stages": [{"name": "source-admission", "result": "pass"}],
            "source_context": None,
        },
    )
    with pytest.raises(GateVerificationError, match="source context"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_bespoke_adapter_that_claims_a_source_context(
    evidence: Path,
) -> None:
    """Only the issue #53 binder records this block, so #135 and #139 may not."""

    assert not _check("desktop-archive-lifecycle").requires_source_context
    _replace_lifecycle(
        evidence,
        "desktop-archive-lifecycle",
        {
            "check_id": "desktop-archive-lifecycle",
            "result": "pass",
            "stages": [{"name": "source-admission", "result": "pass"}],
            "source_context": {
                "event": EVENT,
                "pull_request_head": PULL_REQUEST_HEAD,
                "pull_request_base": PULL_REQUEST_BASE,
            },
        },
    )
    with pytest.raises(GateVerificationError, match="must not claim one"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_lifecycle_report_with_an_extra_unknown_stage(
    evidence: Path,
) -> None:
    """Reviewer case D: an added stage must not pass unnoticed."""

    check = _check("desktop-rpm-lifecycle")
    stages = [{"name": name, "result": "pass"} for name in check.stages]
    stages.append({"name": "surprise-stage", "result": "pass"})
    _replace_lifecycle(
        evidence,
        check.check_id,
        {"check_id": check.check_id, "result": "pass", "stages": stages},
    )
    with pytest.raises(GateVerificationError, match="unexpected=..surprise-stage"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_lifecycle_report_reduced_to_one_stage(
    evidence: Path,
) -> None:
    """Reviewer case E: dropping the rejection and parity stages must fail."""

    check = _check("desktop-rpm-lifecycle")
    _replace_lifecycle(
        evidence,
        check.check_id,
        {
            "check_id": check.check_id,
            "result": "pass",
            "stages": [{"name": check.stages[0], "result": "pass"}],
        },
    )
    with pytest.raises(GateVerificationError, match="corrupt-and-dependency-rejection"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_reordered_lifecycle_stages(evidence: Path) -> None:
    check = _check("desktop-installed-core")
    reordered = list(reversed(check.stages))
    _replace_lifecycle(
        evidence,
        check.check_id,
        {
            "check_id": check.check_id,
            "result": "pass",
            "stages": [{"name": name, "result": "pass"} for name in reordered],
        },
    )
    with pytest.raises(GateVerificationError, match="stage set mismatch"):
        verify_check_set(evidence, IDENTITY)


def _rewrite_prepared_inputs(evidence: Path, payload: bytes) -> None:
    check = _check("desktop-rpm-lifecycle")
    directory = evidence / check.evidence_directory
    identity = _write(directory / check.exact_pairing_input, payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["inputs"]:
        if entry["path"] == check.exact_pairing_input:
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)


def test_gate_rejects_a_reviewed_fixture_pairing(evidence: Path) -> None:
    """The recorded diagnostic payload must never satisfy the final gate."""

    _rewrite_prepared_inputs(evidence, _prepared_inputs(mode="reviewed-fixture"))
    with pytest.raises(GateVerificationError, match="rather than 'exact'"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_pairing_against_another_source_commit(
    evidence: Path,
) -> None:
    _rewrite_prepared_inputs(
        evidence, _prepared_inputs(commit="825a4217c5b5e64fc8908c3444f1bd89fd469b2e")
    )
    with pytest.raises(GateVerificationError, match="not the checked-out commit"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json\n",
        b"[]\n",
        b'{"schema_version": 1}\n',
        b'{"desktop": []}\n',
        b'{"desktop": {"accepted_source_commit": "' + COMMIT.encode() + b'"}}\n',
    ],
)
def test_gate_rejects_malformed_prepared_inputs(evidence: Path, payload: bytes) -> None:
    _rewrite_prepared_inputs(evidence, payload)
    with pytest.raises(GateVerificationError):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_a_dropped_prepared_inputs_binding(evidence: Path) -> None:
    check = _check("desktop-rpm-lifecycle")
    path = evidence / check.evidence_directory / check.receipt_name
    receipt = _read_receipt(path)
    receipt["inputs"] = []
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="source pairing is unproven"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize("check_id", ["core-python-3.12", "core-python-3.13"])
def test_gate_rejects_a_core_report_from_outside_the_test_suite(
    evidence: Path, check_id: str
) -> None:
    """Reviewer case F2: a passing report from an unrelated module must fail.

    Every classname the real core run emits begins with ``tests.``, so a report
    whose cases come from somewhere else is not the core suite even when it
    passes.
    """

    check = _check(check_id)
    directory = evidence / check.evidence_directory
    payload = _junit("examples.desktop-plugin.tests.test_provider")
    identity = _write(directory / "reports/core.junit.xml", payload)
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/core.junit.xml":
            entry["size"] = identity["size"]
            entry["sha256"] = identity["sha256"]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="did not pass"):
        verify_check_set(evidence, IDENTITY)


def test_every_classname_prefix_matches_what_pytest_actually_emits() -> None:
    """A trailing dot means a package; without one it must name a module.

    pytest sets ``classname`` to the module's dotted path for a module-level
    test, and appends ``.<Class>`` only for a test inside a class.  A prefix
    written with a trailing dot therefore matches nothing in a suite of plain
    functions, which silently rejects the real report.
    """

    root = Path(__file__).resolve().parents[2]
    for check in REQUIRED_CHECKS:
        for report in check.reports:
            prefix = report.classname_prefix
            if prefix is None:
                continue
            relative = prefix.rstrip(".").replace(".", "/")
            if prefix.endswith("."):
                assert (root / relative).is_dir(), (
                    f"{check.check_id} prefix {prefix!r} ends with a separator, "
                    f"so {relative} must be a package directory"
                )
            else:
                assert (root / f"{relative}.py").is_file(), (
                    f"{check.check_id} prefix {prefix!r} has no trailing "
                    f"separator, so {relative}.py must be the emitting module"
                )


def _plan_ci_results(plan: Any, **overrides: Any) -> str:
    payload: dict[str, Any] = {"changes": {"result": "success", "outputs": {}}}
    for lane, job in CI_PLAN.LANE_CI_JOBS.items():
        result = "success" if lane in plan.lanes else "skipped"
        payload[job] = {"result": result, "outputs": {}}
    payload.update(overrides)
    return json.dumps(payload)


def _plan_production_results(plan: Any, **overrides: Any) -> str:
    if "desktop" not in plan.lanes:
        return ""
    payload = {}
    for lane, jobs in CI_PLAN.LANE_PRODUCTION_JOBS.items():
        result = "success" if lane in plan.lanes else "skipped"
        for job in jobs:
            payload[job] = {"result": result, "outputs": {}}
    payload.update(overrides)
    return json.dumps(payload)


def _keep_only(evidence: Path, plan: Any) -> Path:
    selected = CI_PLAN.selected_checks(plan)
    for check in REQUIRED_CHECKS:
        if check.check_id not in selected:
            shutil.rmtree(evidence / check.evidence_directory)
    return evidence


def _gate(evidence: Path, plan: Any, **kwargs: Any) -> dict[str, str]:
    arguments = {
        "ci_results": _plan_ci_results(plan),
        "production_results": _plan_production_results(plan),
        "evidence_root": evidence,
        "identity": IDENTITY,
        "plan": plan,
    }
    arguments.update(kwargs)
    return verify_production_gate(**arguments)


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (DOCS_ONLY, set()),
        (TUI_ONLY, {"core-python-3.12", "core-python-3.13"}),
        (
            DESKTOP_WITHOUT_PACKAGING,
            {
                "core-python-3.12",
                "core-python-3.13",
                "desktop-production-tap",
                "desktop-installed-core",
                "desktop-native-payload-fixture",
            },
        ),
        (
            DESKTOP_SOURCE,
            {
                "core-python-3.12",
                "core-python-3.13",
                "desktop-production-tap",
                "desktop-installed-core",
                "desktop-native-payload-fixture",
                "desktop-archive-lifecycle",
                "desktop-archive-sbom",
            },
        ),
    ],
    ids=["docs", "tui", "desktop-without-packaging", "desktop-source"],
)
def test_the_gate_accepts_exactly_the_checks_a_reduced_plan_selects(
    evidence: Path, plan: Any, expected: set[str]
) -> None:
    verified = _gate(_keep_only(evidence, plan), plan)
    assert set(verified) == expected


def test_an_empty_evidence_root_is_valid_only_when_no_check_is_selected(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "gate-evidence"
    empty.mkdir()
    assert _gate(empty, DOCS_ONLY) == {}
    with pytest.raises(GateVerificationError, match="missing="):
        _gate(empty, TUI_ONLY)
    with pytest.raises(GateVerificationError, match="missing="):
        _gate(empty, FULL)


def test_an_absent_evidence_root_is_rejected_even_when_nothing_is_selected(
    tmp_path: Path,
) -> None:
    with pytest.raises(GateVerificationError, match="real directory"):
        _gate(tmp_path / "absent", DOCS_ONLY)


def test_evidence_for_a_deselected_lane_is_rejected_as_injected(
    evidence: Path,
) -> None:
    """Receipts the plan did not ask for mean the wiring drifted."""

    with pytest.raises(GateVerificationError, match="injected="):
        _gate(evidence, DOCS_ONLY)


def test_packaging_evidence_is_rejected_when_packaging_was_deselected(
    evidence: Path,
) -> None:
    _keep_only(evidence, FULL)
    with pytest.raises(GateVerificationError, match="injected=.*desktop-rpm"):
        _gate(evidence, DESKTOP_WITHOUT_PACKAGING)


def test_desktop_without_packaging_requires_the_packaging_jobs_to_skip(
    evidence: Path,
) -> None:
    _keep_only(evidence, DESKTOP_WITHOUT_PACKAGING)
    skipped = (
        CI_PLAN.LANE_PRODUCTION_JOBS["archive"]
        | CI_PLAN.LANE_PRODUCTION_JOBS["packaging"]
    )
    for job in sorted(skipped):
        with pytest.raises(GateVerificationError, match=job):
            _gate(
                evidence,
                DESKTOP_WITHOUT_PACKAGING,
                production_results=_plan_production_results(
                    DESKTOP_WITHOUT_PACKAGING, **{job: {"result": "success"}}
                ),
            )


def test_desktop_without_packaging_still_requires_every_desktop_job(
    evidence: Path,
) -> None:
    _keep_only(evidence, DESKTOP_WITHOUT_PACKAGING)
    for job in sorted(CI_PLAN.LANE_PRODUCTION_JOBS["desktop"]):
        with pytest.raises(GateVerificationError, match=job):
            _gate(
                evidence,
                DESKTOP_WITHOUT_PACKAGING,
                production_results=_plan_production_results(
                    DESKTOP_WITHOUT_PACKAGING, **{job: {"result": "skipped"}}
                ),
            )


def test_a_desktop_source_plan_requires_the_rpm_lifecycle_to_skip(
    evidence: Path,
) -> None:
    """Decisions 9 and 19: renderer, shared and stylesheet source run the
    archive and SBOM jobs, never the RPM lifecycle, so a run of it or a stray
    RPM receipt is drift."""

    _keep_only(evidence, DESKTOP_SOURCE)
    assert set(_gate(evidence, DESKTOP_SOURCE)) == CI_PLAN.selected_checks(
        DESKTOP_SOURCE
    )
    with pytest.raises(GateVerificationError, match="rpm-lifecycle"):
        _gate(
            evidence,
            DESKTOP_SOURCE,
            production_results=_plan_production_results(
                DESKTOP_SOURCE, **{"rpm-lifecycle": {"result": "success"}}
            ),
        )
    for job in sorted(CI_PLAN.LANE_PRODUCTION_JOBS["archive"]):
        with pytest.raises(GateVerificationError, match=job):
            _gate(
                evidence,
                DESKTOP_SOURCE,
                production_results=_plan_production_results(
                    DESKTOP_SOURCE, **{job: {"result": "skipped"}}
                ),
            )


def test_a_stray_rpm_receipt_fails_a_desktop_source_plan(tmp_path: Path) -> None:
    evidence = _build_evidence(tmp_path / "gate-evidence")
    _keep_only(evidence, DESKTOP_SOURCE)
    rpm = _build_evidence(tmp_path / "all") / "desktop-rpm-lifecycle"
    shutil.copytree(rpm, evidence / "desktop-rpm-lifecycle")
    with pytest.raises(GateVerificationError, match="injected=.*desktop-rpm"):
        _gate(evidence, DESKTOP_SOURCE)


def test_a_full_plan_without_the_python_3_12_receipt_fails(evidence: Path) -> None:
    shutil.rmtree(evidence / "core-python-3.12")
    with pytest.raises(GateVerificationError, match="missing=..core-python-3.12"):
        _gate(evidence, FULL)


def test_a_reduced_plan_without_the_python_3_12_receipt_fails(
    evidence: Path,
) -> None:
    """Core runs Python 3.12 and 3.13 on every plan, so a reduced plan that
    selects core still requires the 3.12 receipt."""

    _keep_only(evidence, TUI_ONLY)
    shutil.rmtree(evidence / "core-python-3.12")
    with pytest.raises(GateVerificationError, match="missing=..core-python-3.12"):
        _gate(evidence, TUI_ONLY)


@pytest.mark.parametrize(
    ("check_id", "stage"),
    [
        ("desktop-native-payload-fixture", "integration-contract-suite"),
        ("desktop-native-payload-fixture", "packaging-contract-suite"),
        ("desktop-production-tap", "plugin-example-compatibility"),
    ],
)
def test_a_missing_or_extra_stage_fails(
    evidence: Path, check_id: str, stage: str
) -> None:
    check = _check(check_id)
    directory = evidence / check.evidence_directory
    lifecycle = next(
        report.path
        for report in check.reports
        if report.report_format == ARTIFACT_LIFECYCLE
    )
    original = json.loads((directory / lifecycle).read_bytes())
    for stages in (
        [item for item in original["stages"] if item["name"] != stage],
        [
            *original["stages"],
            {"name": "draft-and-process-acceptance", "result": "pass"},
        ],
    ):
        document = {**original, "stages": stages}
        payload = (json.dumps(document, sort_keys=True) + "\n").encode()
        identity = _write(directory / lifecycle, payload)
        path = directory / check.receipt_name
        receipt = _read_receipt(path)
        for entry in receipt["reports"]:
            if entry["path"] == lifecycle:
                entry["size"] = identity["size"]
                entry["sha256"] = identity["sha256"]
        _write_receipt(path, receipt)
        with pytest.raises(GateVerificationError, match="stage set mismatch"):
            verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize("raw", ["{}", "null"])
def test_production_results_must_be_empty_when_desktop_is_deselected(
    evidence: Path, raw: str
) -> None:
    _keep_only(evidence, TUI_ONLY)
    with pytest.raises(GateVerificationError, match="must be empty"):
        _gate(evidence, TUI_ONLY, production_results=raw)
    verify_production_results("", TUI_ONLY)
    verify_production_results("  \n", TUI_ONLY)


def test_production_results_must_be_present_when_desktop_is_selected(
    evidence: Path,
) -> None:
    with pytest.raises(GateVerificationError, match="absent"):
        _gate(evidence, FULL, production_results="")


def test_a_deselected_ci_lane_must_report_exactly_skipped(evidence: Path) -> None:
    _keep_only(evidence, TUI_ONLY)
    with pytest.raises(GateVerificationError, match="desktop-production"):
        _gate(
            evidence,
            TUI_ONLY,
            ci_results=_plan_ci_results(
                TUI_ONLY, **{"desktop-production": {"result": "success"}}
            ),
        )


def test_changes_must_succeed_unless_the_plan_is_full(evidence: Path) -> None:
    _keep_only(evidence, TUI_ONLY)
    with pytest.raises(GateVerificationError, match="changes"):
        _gate(
            evidence,
            TUI_ONLY,
            ci_results=_plan_ci_results(TUI_ONLY, changes={"result": "failure"}),
        )


def test_a_full_plan_accepts_a_failed_changes_job_when_every_lane_passed(
    tmp_path: Path,
) -> None:
    evidence = _build_evidence(tmp_path / "gate-evidence")
    verified = _gate(
        evidence,
        FULL,
        ci_results=_plan_ci_results(FULL, changes={"result": "failure"}),
    )
    assert set(verified) == {check.check_id for check in REQUIRED_CHECKS}


def test_the_gate_rejects_an_invalid_plan(evidence: Path) -> None:
    unclosed = _plan("packaging")
    with pytest.raises(GateVerificationError, match="effective plan is invalid"):
        _gate(evidence, unclosed, ci_results=_plan_ci_results(FULL))


def test_load_plan_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    with pytest.raises(GateVerificationError, match="unreadable"):
        load_plan(path)
    path.write_text("[]")
    with pytest.raises(GateVerificationError, match="malformed"):
        load_plan(path)
    path.write_text(DESKTOP_WITHOUT_PACKAGING.to_json())
    assert load_plan(path) == DESKTOP_WITHOUT_PACKAGING


def _cli_arguments(evidence: Path, plan_path: Path) -> list[str]:
    return [
        "--evidence-root",
        str(evidence),
        "--plan",
        str(plan_path),
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
        "--event",
        EVENT,
        "--pull-request-head",
        PULL_REQUEST_HEAD,
        "--pull-request-base",
        PULL_REQUEST_BASE,
    ]


def test_the_cli_requires_a_plan(evidence: Path, tmp_path: Path) -> None:
    arguments = _cli_arguments(evidence, tmp_path / "plan.json")
    index = arguments.index("--plan")
    with pytest.raises(SystemExit):
        main(arguments[:index] + arguments[index + 2 :])


def test_the_cli_verifies_against_the_plan_file(
    evidence: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(TUI_ONLY.to_json())
    _keep_only(evidence, TUI_ONLY)
    monkeypatch.setenv("DESKTOP_GATE_RESULTS", _plan_ci_results(TUI_ONLY))
    monkeypatch.setenv("DESKTOP_PRODUCTION_RESULTS", "")
    assert main(_cli_arguments(evidence, plan_path)) == 0
    assert "verified the 2 checks" in capsys.readouterr().out

    plan_path.write_text(FULL.to_json())
    assert main(_cli_arguments(evidence, plan_path)) == 1
    assert "gate failed" in capsys.readouterr().err
