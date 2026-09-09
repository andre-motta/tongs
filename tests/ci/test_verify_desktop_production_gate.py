"""Prove the production gate rejects every incomplete or untrustworthy set.

The fixtures here are synthetic on purpose.  A passing case proves only that
the consumer accepts a well-formed complete set; the negative cases carry the
value, because each one reproduces a way a real workflow could otherwise report
a false green: a failed job, a skipped job, a cancelled job, a missing result,
a stale receipt from another run, and an injected receipt or report.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.ci.verify_desktop_production_gate import (
    ARTIFACT_LIFECYCLE,
    GPU_GATE_CHECK_ID,
    NODE_TAP,
    PYTEST_JUNIT,
    REQUIRED_CHECKS,
    REQUIRED_CI_JOBS,
    REQUIRED_PRODUCTION_JOBS,
    GateIdentity,
    GateVerificationError,
    verify_check_set,
    verify_job_results,
    verify_production_gate,
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
    if "draft-process" in path:
        return _junit(
            "tests.integration.desktop.test_draft_process_acceptance.TestDrafts"
        )
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
        )


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled", "neutral", None])
@pytest.mark.parametrize("job", sorted(REQUIRED_PRODUCTION_JOBS))
def test_gate_rejects_every_non_success_production_result(
    evidence: Path, job: str, result: Any
) -> None:
    with pytest.raises(GateVerificationError, match=job):
        verify_production_gate(
            ci_results=_ci_results(),
            production_results=_production_results(**{job: {"result": result}}),
            evidence_root=evidence,
            identity=IDENTITY,
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
        )


@pytest.mark.parametrize("raw", ["", "   ", "[]", "{broken", "null", '"success"'])
def test_gate_rejects_malformed_result_payloads(evidence: Path, raw: str) -> None:
    with pytest.raises(GateVerificationError):
        verify_production_gate(
            ci_results=raw,
            production_results=_production_results(),
            evidence_root=evidence,
            identity=IDENTITY,
        )


def test_gate_rejects_a_malformed_job_entry(evidence: Path) -> None:
    with pytest.raises(GateVerificationError, match="core=malformed"):
        verify_production_gate(
            ci_results=_ci_results(core=None),
            production_results=_production_results(),
            evidence_root=evidence,
            identity=IDENTITY,
        )


def test_job_results_helper_requires_the_exact_set() -> None:
    verify_job_results(_results(REQUIRED_CI_JOBS), REQUIRED_CI_JOBS, "ordinary CI")
    with pytest.raises(GateVerificationError, match="mismatch"):
        verify_job_results(
            _results(REQUIRED_PRODUCTION_JOBS), REQUIRED_CI_JOBS, "ordinary CI"
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
        ("attempt", 2),
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
        if entry["path"] != "reports/draft-process.junit.xml"
    ]
    _write_receipt(path, receipt)
    with pytest.raises(GateVerificationError, match="missing=..reports/draft-process"):
        verify_check_set(evidence, IDENTITY)


def test_gate_rejects_report_bytes_replaced_after_the_receipt(evidence: Path) -> None:
    check = _check("core-python-3.12")
    directory = evidence / check.evidence_directory
    (directory / "reports/mcp.junit.xml").write_bytes(_junit("tests.test_mcp.other"))
    with pytest.raises(GateVerificationError, match="rejected"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    ("counter", "outcome"),
    [
        ("failures", '<failure message="boom"/>'),
        ("errors", '<error message="import"/>'),
        ("skipped", '<skipped message="mcp missing"/>'),
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
        ("ok", " # SKIP unsupported"),
        ("ok", " # TODO later"),
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
    _replace_lifecycle(
        evidence,
        "desktop-rpm-lifecycle",
        {
            "check_id": "desktop-rpm-lifecycle",
            "result": "pass",
            "stages": [
                {"name": "clean-install", "result": "pass"},
                {"name": "upgraded-final-parity", "result": "fail"},
            ],
        },
    )
    with pytest.raises(GateVerificationError, match="upgraded-final-parity"):
        verify_check_set(evidence, IDENTITY)


@pytest.mark.parametrize(
    "document",
    [
        {"check_id": "desktop-archive-sbom", "result": "pass", "stages": []},
        {"check_id": "desktop-archive-sbom", "result": "pass"},
        {"check_id": "another-check", "result": "pass", "stages": [{"name": "a"}]},
    ],
)
def test_gate_rejects_an_empty_absent_or_foreign_lifecycle_report(
    evidence: Path, document: dict[str, Any]
) -> None:
    _replace_lifecycle(evidence, "desktop-archive-sbom", document)
    with pytest.raises(GateVerificationError):
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


def test_gate_rejects_a_lifecycle_report_relabelled_as_a_test_report(
    evidence: Path,
) -> None:
    check = _check("desktop-native-payload-fixture")
    directory = evidence / check.evidence_directory
    path = directory / check.receipt_name
    receipt = _read_receipt(path)
    for entry in receipt["reports"]:
        if entry["path"] == "reports/native-payload-evidence.json":
            entry["format"] = PYTEST_JUNIT
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


@pytest.mark.parametrize("field", ["event", "pull_request_head", "pull_request_base"])
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


def test_every_configured_check_declares_a_nonempty_stage_set() -> None:
    for check in REQUIRED_CHECKS:
        assert check.stages, check.check_id
        assert len(set(check.stages)) == len(check.stages), check.check_id


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
    payload = _junit("spikes.desktop.tests.test_backend")
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


def test_both_core_reports_are_bound_to_their_suites() -> None:
    for check_id in ("core-python-3.12", "core-python-3.13"):
        prefixes = {
            report.path: report.classname_prefix
            for report in _check(check_id).reports
            if report.report_format == PYTEST_JUNIT
        }
        assert prefixes == {
            "reports/core.junit.xml": "tests.",
            "reports/mcp.junit.xml": "tests.test_mcp.",
        }


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
