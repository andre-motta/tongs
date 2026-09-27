"""Fail-closed aggregate over the complete desktop production check set.

This module owns the consumer side of issue #53.  It never reads policy from
the evidence it inspects.  The complete required job set, the complete required
check set, each check's staged report set and each report's semantic format are
declared here as constants.  A receipt, a lifecycle report or a workflow output
may only ever satisfy an expectation that this file already declares.

Three independent classes of evidence must all agree before the gate passes:

1. Workflow job results, judged against the effective lane plan the aggregate
   wrote (``tests/ci/ci_plan.py``).  Every job of the ordinary CI workflow and
   every job of the called production workflow whose lane the plan selected
   must report ``success``.  A skip passes only for a lane the effective plan
   deselected, and such a lane must report exactly ``skipped``: a deselected
   lane that ran anyway proves the wiring drifted and is rejected.  Failure,
   cancellation, a missing entry, an unexpected entry and a malformed entry are
   rejected for every lane, and the production results must be empty when the
   plan deselected desktop.  Only the ``changes`` planning job may report
   something other than success, and only when the effective plan is the full
   graph.
2. Receipts.  The required check set is the union of the checks of every
   selected lane, and the evidence directories must equal it exactly.  Each
   required check contributes exactly one issue #110 receipt validated against
   a consumer-owned :class:`ReceiptPolicy` carrying this run's repository, run
   ID, environment, provenance, source commit and source tree, and the attempt
   the receipt records.  That attempt may be any attempt of this run up to the
   aggregate's own: after "Re-run failed jobs" a lane that passed earlier keeps
   the receipt of the attempt that produced it, and each gate artifact name
   omits the attempt so a rerun lane overwrites its own.  A receipt from
   another run, a later attempt, another commit or another check is stale and
   rejected.  A receipt left behind by a lane that is failing now never
   satisfies the gate, because class 1 already requires that lane's latest
   job result to be ``success``.
3. Report outcomes.  Every staged report is read back through issue #130
   ``read_bound_bytes`` and parsed by the issue #115 semantic parsers, so a
   receipt whose ``result`` says ``success`` while its report records a
   failure, an error, a skip, a cancellation, a TODO or a truncation is
   rejected.

The physical GPU gate is deliberately unreachable from here.  Its check ID is
reserved and any receipt bearing it is rejected, because no hosted headless
runner can produce it.  Issue #55 owns that evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RECEIPT_READER_PROGRAM = ".github/scripts/verify_desktop_production.py"
REPORT_PARSER_PROGRAM = ".github/scripts/desktop_test_reports.py"
CI_PLAN_PROGRAM = "tests/ci/ci_plan.py"
#: Shared with ``verify_desktop_ci`` so both verifiers see one ``Plan`` class.
CI_PLAN_MODULE = "tongs_ci_plan"


class GateVerificationError(ValueError):
    """Raised when the complete production evidence set is not trustworthy."""


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise GateVerificationError(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


RECEIPTS = _load_module("desktop_gate_receipts", ROOT / RECEIPT_READER_PROGRAM)
REPORTS = _load_module("desktop_gate_reports", ROOT / REPORT_PARSER_PROGRAM)
CI_PLAN = sys.modules.get(CI_PLAN_MODULE) or _load_module(
    CI_PLAN_MODULE, ROOT / CI_PLAN_PROGRAM
)


@dataclass(frozen=True, slots=True)
class ExpectedReport:
    """One staged report and the semantic parser that owns its outcome."""

    path: str
    report_format: str
    classname_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """One consumer-configured production check and its evidence layout."""

    check_id: str
    workflow: str
    job: str
    evidence_directory: str
    receipt_name: str
    reports: tuple[ExpectedReport, ...]
    #: Exact ordered lifecycle stage names this consumer requires.  Owning the
    #: set here, rather than accepting whatever a report lists, is what stops a
    #: later edit to a stage plan from quietly dropping a stage while the gate
    #: stays green.  It mirrors the reviewed #139 adapter's ``STAGE_NAMES``.
    stages: tuple[str, ...]
    #: Receipt-bound input this consumer parses to prove the RPM leg paired
    #: with the fresh same-source archive rather than a recorded fixture.
    exact_pairing_input: str | None = None
    #: True for the checks published by the issue #53 stage binder, which
    #: records the checked-out commit alongside the pull request head, base and
    #: event.  The bespoke issue #135 and #139 adapters own their own report
    #: shape and must not carry the block, so requiring it per check keeps a
    #: producer from skipping the label check by omitting the field.
    requires_source_context: bool = True

    @property
    def report_formats(self) -> tuple[str, ...]:
        seen: list[str] = []
        for report in self.reports:
            if report.report_format not in seen:
                seen.append(report.report_format)
        return tuple(seen)


PYTEST_JUNIT = "pytest-junit"
NODE_TAP = "node-tap"
ARTIFACT_LIFECYCLE = "artifact-lifecycle-v1"

#: Jobs of the ordinary CI workflow the aggregate needs: every lane job plus
#: ``changes``.  Which of them must succeed is decided by the effective plan.
REQUIRED_CI_JOBS: frozenset[str] = frozenset(CI_PLAN.LANE_CI_JOBS.values()) | {
    CI_PLAN.CHANGES_JOB
}

#: Jobs of the called production workflow, owned by the desktop and packaging
#: lanes.  The reusable workflow reports one aggregated result to its caller,
#: so the consumer must inspect this inner set separately or a skipped inner
#: job would be invisible.
REQUIRED_PRODUCTION_JOBS: frozenset[str] = frozenset(
    job for jobs in CI_PLAN.LANE_PRODUCTION_JOBS.values() for job in jobs
)

#: Reserved identifier for the physical GPU acceptance owned by issue #55.  No
#: hosted headless runner may emit it, so a receipt carrying it is an injection.
GPU_GATE_CHECK_ID = "desktop-native-physical-gpu"

REQUIRED_CHECKS: tuple[RequiredCheck, ...] = (
    RequiredCheck(
        check_id="core-python-3.12",
        stages=(
            "core-suite",
            "mcp-suite",
        ),
        workflow="ci",
        job="core",
        evidence_directory="core-python-3.12",
        receipt_name="core-receipt.json",
        reports=(
            ExpectedReport("reports/core-evidence.json", ARTIFACT_LIFECYCLE),
            ExpectedReport("reports/core.junit.xml", PYTEST_JUNIT, "tests."),
            ExpectedReport("reports/mcp.junit.xml", PYTEST_JUNIT, "tests.test_mcp."),
        ),
    ),
    RequiredCheck(
        check_id="core-python-3.13",
        stages=(
            "core-suite",
            "mcp-suite",
        ),
        workflow="ci",
        job="core",
        evidence_directory="core-python-3.13",
        receipt_name="core-receipt.json",
        reports=(
            ExpectedReport("reports/core-evidence.json", ARTIFACT_LIFECYCLE),
            ExpectedReport("reports/core.junit.xml", PYTEST_JUNIT, "tests."),
            ExpectedReport("reports/mcp.junit.xml", PYTEST_JUNIT, "tests.test_mcp."),
        ),
    ),
    RequiredCheck(
        check_id="desktop-production-tap",
        stages=(
            "exact-lock-shell-build",
            "production-shell-and-renderer-tap",
            "plugin-example-compatibility",
            "draft-and-process-acceptance",
        ),
        workflow="desktop-production",
        job="desktop-tap",
        evidence_directory="desktop-production-tap",
        receipt_name="desktop-tap-receipt.json",
        reports=(
            ExpectedReport("reports/desktop-tap-evidence.json", ARTIFACT_LIFECYCLE),
            ExpectedReport("reports/desktop-shell.tap", NODE_TAP),
            ExpectedReport(
                "reports/plugin-example.junit.xml",
                PYTEST_JUNIT,
            ),
            ExpectedReport(
                "reports/draft-process.junit.xml",
                PYTEST_JUNIT,
                "tests.integration.desktop.test_draft_process_acceptance",
            ),
        ),
    ),
    RequiredCheck(
        check_id="desktop-archive-lifecycle",
        stages=(
            "source-admission",
            "producer-transfer-validation",
            "archive-contract-license-validation",
            "reproducibility-output-binding",
            "source-tool-metadata-validation",
        ),
        requires_source_context=False,
        workflow="desktop-production",
        job="archive-evidence",
        evidence_directory="desktop-archive-lifecycle",
        receipt_name="archive-receipt.json",
        reports=(ExpectedReport("reports/archive-evidence.json", ARTIFACT_LIFECYCLE),),
    ),
    RequiredCheck(
        check_id="desktop-installed-core",
        stages=(
            "source-admission",
            "wheel-record-install-identity",
            "offline-startup-audit",
            "terminal-evidence",
        ),
        workflow="desktop-production",
        job="installed-core",
        evidence_directory="desktop-installed-core",
        receipt_name="installed-core-receipt.json",
        reports=(
            ExpectedReport("reports/installed-core-evidence.json", ARTIFACT_LIFECYCLE),
        ),
    ),
    RequiredCheck(
        check_id="desktop-archive-sbom",
        stages=(
            "source-admission",
            "producer-transfer-validation",
            "semantic-generation",
            "determinism",
        ),
        requires_source_context=False,
        workflow="desktop-production",
        job="archive-sbom",
        evidence_directory="desktop-archive-sbom",
        receipt_name="sbom-receipt.json",
        reports=(ExpectedReport("reports/sbom-evidence.json", ARTIFACT_LIFECYCLE),),
    ),
    RequiredCheck(
        check_id="desktop-rpm-lifecycle",
        stages=(
            "exact-payload-pairing",
            "companion-closure-and-source-rebuild",
            "clean-install-and-installed-payload",
            "optional-mcp-install-and-removal",
            "reinstall-stability",
            "corrupt-and-dependency-rejection",
            "upgrade-byte-parity",
            "complete-uninstall",
        ),
        exact_pairing_input="inputs/prepared-inputs.json",
        workflow="desktop-production",
        job="rpm-lifecycle",
        evidence_directory="desktop-rpm-lifecycle",
        receipt_name="rpm-lifecycle-receipt.json",
        reports=(
            ExpectedReport("reports/rpm-lifecycle-evidence.json", ARTIFACT_LIFECYCLE),
        ),
    ),
    RequiredCheck(
        check_id="desktop-native-payload-fixture",
        stages=("verifier-fixture-structure",),
        workflow="desktop-production",
        job="native-payload",
        evidence_directory="desktop-native-payload-fixture",
        receipt_name="native-payload-receipt.json",
        reports=(
            ExpectedReport("reports/native-payload-evidence.json", ARTIFACT_LIFECYCLE),
            ExpectedReport(
                "reports/native-payload.junit.xml",
                PYTEST_JUNIT,
                "tests.integration.desktop.test_native_payload_acceptance",
            ),
        ),
    ),
)

REQUIRED_CHECK_IDS: frozenset[str] = frozenset(
    check.check_id for check in REQUIRED_CHECKS
)
if REQUIRED_CHECK_IDS != frozenset(
    check for checks in CI_PLAN.LANE_CHECKS.values() for check in checks
):
    raise GateVerificationError("LANE_CHECKS does not cover exactly REQUIRED_CHECKS")


@dataclass(frozen=True, slots=True)
class GateIdentity:
    """Consumer-owned identity for every receipt validated by this gate."""

    commit: str
    tree: str
    repository: str
    run_id: str
    #: The aggregate's own run attempt: the latest attempt a receipt may record.
    attempt: int
    environment: str
    provenance: str
    event: str
    pull_request_head: str
    pull_request_base: str


def _decode_results(raw_results: str, label: str) -> dict[str, Any]:
    if not isinstance(raw_results, str) or not raw_results.strip():
        raise GateVerificationError(f"{label} results are absent")
    try:
        results = json.loads(raw_results)
    except json.JSONDecodeError as error:
        raise GateVerificationError(f"{label} results are malformed JSON") from error
    if not isinstance(results, dict):
        raise GateVerificationError(f"{label} results must be a JSON object")
    return results


def verify_job_results(
    raw_results: str,
    required_jobs: frozenset[str] | Mapping[str, frozenset[str]],
    label: str,
) -> None:
    """Require the exact configured job set with an allowed result each.

    ``required_jobs`` maps each job to the results it may report.  A plain set
    requires ``success`` from every job.
    """

    if isinstance(required_jobs, Mapping):
        allowed = {name: frozenset(results) for name, results in required_jobs.items()}
    else:
        allowed = {name: frozenset({"success"}) for name in required_jobs}
    results = _decode_results(raw_results, label)
    actual = set(results)
    if actual != set(allowed):
        missing = sorted(set(allowed) - actual)
        unexpected = sorted(actual - set(allowed))
        raise GateVerificationError(
            f"{label} job set mismatch: missing={missing}, unexpected={unexpected}"
        )
    rejected: list[str] = []
    for name in sorted(allowed):
        job = results[name]
        if not isinstance(job, dict):
            rejected.append(f"{name}=malformed")
            continue
        result = job.get("result")
        if result not in allowed[name]:
            expected = "|".join(sorted(allowed[name]))
            rejected.append(f"{name}={result!r} (expected {expected})")
    if rejected:
        raise GateVerificationError(
            f"{label} jobs did not match the effective plan: " + ", ".join(rejected)
        )


def _required_checks(plan: Any) -> tuple[RequiredCheck, ...]:
    if plan is None:
        return REQUIRED_CHECKS
    try:
        selected = CI_PLAN.selected_checks(plan)
    except (AttributeError, ValueError) as error:
        raise GateVerificationError(f"effective plan is invalid: {error}") from error
    return tuple(check for check in REQUIRED_CHECKS if check.check_id in selected)


def verify_production_results(raw_results: str, plan: Any) -> None:
    """Require the inner production results the effective plan implies."""

    try:
        expected = CI_PLAN.expected_production_results(plan)
    except (AttributeError, ValueError) as error:
        raise GateVerificationError(f"effective plan is invalid: {error}") from error
    if expected is None:
        if isinstance(raw_results, str) and not raw_results.strip():
            return
        raise GateVerificationError(
            "desktop production results must be empty when the effective plan "
            "deselected desktop"
        )
    verify_job_results(raw_results, expected, "desktop production")


def _discovered_check_directories(evidence_root: Path) -> dict[str, Path]:
    root = Path(evidence_root)
    if not root.is_dir() or root.is_symlink():
        raise GateVerificationError("gate evidence root must be a real directory")
    discovered: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            raise GateVerificationError(
                f"gate evidence contains a non-directory entry: {entry.name}"
            )
        discovered[entry.name] = entry
    return discovered


def receipt_attempt(receipt_path: Path, identity: GateIdentity) -> int:
    """Return the run attempt a receipt records, bounded by this run's attempts.

    The receipt reader requires one exact attempt, so the gate reads the
    recorded attempt first and passes it on as the expectation when it lies in
    ``1..identity.attempt``.  A receipt from a later attempt than the
    aggregate's own is rejected here.  An unreadable or malformed receipt, or a
    malformed attempt, falls back to the aggregate's attempt, so the receipt
    reader rejects it with its own precise message.
    """

    path = Path(receipt_path)
    if path.is_symlink() or not path.is_file():
        return identity.attempt
    try:
        with path.open("rb") as stream:
            payload = stream.read(RECEIPTS.MAX_RECEIPT_BYTES + 1)
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return identity.attempt
    if len(payload) > RECEIPTS.MAX_RECEIPT_BYTES or not isinstance(document, dict):
        return identity.attempt
    execution = document.get("execution")
    if not isinstance(execution, dict):
        return identity.attempt
    recorded = execution.get("attempt")
    if type(recorded) is not int or recorded < 1:
        return identity.attempt
    if recorded > identity.attempt:
        raise GateVerificationError(
            f"receipt {path.name!r} records attempt {recorded}, later than this "
            f"run's attempt {identity.attempt}"
        )
    return recorded


def _verify_one_check(
    check: RequiredCheck, directory: Path, identity: GateIdentity
) -> str:
    receipt_path = directory / check.receipt_name
    policy = RECEIPTS.ReceiptPolicy(
        expected_commit=identity.commit,
        expected_tree=identity.tree,
        expected_repository=identity.repository,
        expected_run_id=identity.run_id,
        expected_attempt=receipt_attempt(receipt_path, identity),
        expected_environment=identity.environment,
        expected_provenance=identity.provenance,
        expected_check_id=check.check_id,
        allowed_report_formats=check.report_formats,
    )
    validation = RECEIPTS.validate_receipt_file(
        receipt_path, evidence_root=directory, policy=policy
    )
    receipt = validation.receipt
    if receipt.get("result") != "success":
        raise GateVerificationError(
            f"check {check.check_id!r} receipt result is "
            f"{receipt.get('result')!r} rather than success"
        )

    bound = {item.path: item for item in validation.bound_files}
    expected_reports = {report.path: report for report in check.reports}
    staged_reports = {
        item.path: item for item in validation.bound_files if item.kind == "report"
    }
    if set(staged_reports) != set(expected_reports):
        missing = sorted(set(expected_reports) - set(staged_reports))
        unexpected = sorted(set(staged_reports) - set(expected_reports))
        raise GateVerificationError(
            f"check {check.check_id!r} report set mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )

    declared_formats = {
        entry["path"]: entry["format"] for entry in receipt.get("reports", [])
    }
    for path, expected in expected_reports.items():
        if declared_formats.get(path) != expected.report_format:
            raise GateVerificationError(
                f"check {check.check_id!r} report {path!r} declares format "
                f"{declared_formats.get(path)!r} rather than "
                f"{expected.report_format!r}"
            )
        _verify_report_outcome(check, expected, directory, bound[path], identity)
    _verify_exact_pairing(check, directory, bound, identity)
    return str(receipt_path)


def _verify_exact_pairing(
    check: RequiredCheck,
    directory: Path,
    bound: dict[str, Any],
    identity: GateIdentity,
) -> None:
    """Require the RPM leg to have paired with this run's own fresh archive.

    The producer chain already refuses the historical fixture path, but the
    claim the issue makes by name is that the lifecycle ran against the fresh
    same-source archive.  Reading the producer's own ``prepared-inputs.json``
    is what turns that from a workflow argument into receipt-bound evidence: a
    fixture-mode run records ``reviewed-fixture`` and an accepted source commit
    that differs from the core commit, and both are rejected here.
    """

    if check.exact_pairing_input is None:
        return
    reference = bound.get(check.exact_pairing_input)
    if reference is None or reference.kind != "input":
        raise GateVerificationError(
            f"check {check.check_id!r} does not bind {check.exact_pairing_input!r} "
            "as an input, so its source pairing is unproven"
        )
    payload = RECEIPTS.read_bound_bytes(
        directory, reference, RECEIPTS.MAX_RECEIPT_BYTES
    )
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateVerificationError(
            f"check {check.check_id!r} prepared inputs are not decodable JSON"
        ) from error
    if not isinstance(document, dict):
        raise GateVerificationError(
            f"check {check.check_id!r} prepared inputs must be a JSON object"
        )
    desktop = document.get("desktop")
    if not isinstance(desktop, dict):
        raise GateVerificationError(
            f"check {check.check_id!r} prepared inputs record no desktop payload"
        )
    mode = desktop.get("pairing_mode")
    if mode != "exact":
        raise GateVerificationError(
            f"check {check.check_id!r} paired with mode {mode!r} rather than "
            "'exact', so the lifecycle did not run against the fresh archive"
        )
    accepted = desktop.get("accepted_source_commit")
    if accepted != identity.commit:
        raise GateVerificationError(
            f"check {check.check_id!r} accepted source commit {accepted!r} is "
            f"not the checked-out commit {identity.commit!r}"
        )


def _verify_report_outcome(
    check: RequiredCheck,
    expected: ExpectedReport,
    directory: Path,
    bound_file: Any,
    identity: GateIdentity,
) -> None:
    if expected.report_format == ARTIFACT_LIFECYCLE:
        maximum = RECEIPTS.MAX_JSON_REPORT_BYTES
    else:
        maximum = REPORTS.MAX_REPORT_BYTES
    report_bytes = RECEIPTS.read_bound_bytes(directory, bound_file, maximum)
    try:
        if expected.report_format == PYTEST_JUNIT:
            REPORTS.verify_pytest_junit(
                report_bytes, expected_classname_prefix=expected.classname_prefix
            )
        elif expected.report_format == NODE_TAP:
            REPORTS.verify_node_tap(report_bytes)
        else:
            _verify_lifecycle_outcome(check, expected, report_bytes, identity)
    except REPORTS.ReportValidationError as error:
        raise GateVerificationError(
            f"check {check.check_id!r} report {expected.path!r} did not pass: {error}"
        ) from error


def _verify_lifecycle_outcome(
    check: RequiredCheck,
    expected: ExpectedReport,
    report_bytes: bytes,
    identity: GateIdentity,
) -> None:
    """Require a passing lifecycle result with ordered, named, passing stages.

    The stage names themselves belong to the producing adapter, which has
    already compared them against its own closed list.  This consumer requires
    only that the report identifies the configured check, records an overall
    pass, and carries a nonempty ordered stage list in which no stage reports
    anything other than a pass.
    """

    try:
        document = json.loads(report_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle report {expected.path!r} "
            "is not decodable JSON"
        ) from error
    if not isinstance(document, dict):
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle report must be a JSON object"
        )
    if document.get("check_id") != check.check_id:
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle report names "
            f"{document.get('check_id')!r}"
        )
    if document.get("result") != "pass":
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle result is "
            f"{document.get('result')!r} rather than pass"
        )
    _verify_source_context(check, document, identity)
    stages = document.get("stages")
    if not isinstance(stages, list) or not stages:
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle report records no stages"
        )
    names: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict):
            raise GateVerificationError(
                f"check {check.check_id!r} lifecycle stage is malformed"
            )
        name = stage.get("name")
        if not isinstance(name, str) or not name:
            raise GateVerificationError(
                f"check {check.check_id!r} lifecycle stage is unnamed"
            )
        if stage.get("result") != "pass":
            raise GateVerificationError(
                f"check {check.check_id!r} lifecycle stage {name!r} reports "
                f"{stage.get('result')!r} rather than pass"
            )
        names.append(name)
    if len(set(names)) != len(names):
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle report repeats a stage name"
        )
    if tuple(names) != check.stages:
        missing = [name for name in check.stages if name not in names]
        unexpected = [name for name in names if name not in check.stages]
        raise GateVerificationError(
            f"check {check.check_id!r} lifecycle stage set mismatch: "
            f"missing={missing}, unexpected={unexpected}, "
            f"observed={names}, required={list(check.stages)}"
        )


def _verify_source_context(
    check: RequiredCheck, document: dict[str, Any], identity: GateIdentity
) -> None:
    """Require honest labelling of the checked-out commit and its origin.

    Ordinary CI deliberately tests GitHub's synthetic merge commit for a pull
    request, so the receipt binds that commit while the pull request head and
    base travel beside it as metadata.  This consumer owns the expected values
    and never reads them from the report.
    """

    context = document.get("source_context")
    if not check.requires_source_context:
        if context is not None:
            raise GateVerificationError(
                f"check {check.check_id!r} is not expected to record a source "
                "context and must not claim one"
            )
        return
    if identity.event == "pull_request" and (
        identity.pull_request_head == identity.commit
    ):
        raise GateVerificationError(
            "a pull request gate must bind the synthetic merge commit, not the "
            "pull request head"
        )
    expected = {
        "event": identity.event,
        "pull_request_head": identity.pull_request_head,
        "pull_request_base": identity.pull_request_base,
    }
    if context != expected:
        raise GateVerificationError(
            f"check {check.check_id!r} source context {context!r} does not "
            f"match the consumer expectation {expected!r}"
        )


def verify_check_set(
    evidence_root: Path, identity: GateIdentity, plan: Any = None
) -> dict[str, str]:
    """Validate every required receipt and reject anything else present.

    The required checks are those of the lanes ``plan`` selects; without a
    plan every configured check is required.
    """

    required = _required_checks(plan)
    required_ids = frozenset(check.check_id for check in required)
    discovered = _discovered_check_directories(evidence_root)
    configured = {check.evidence_directory: check for check in required}
    if GPU_GATE_CHECK_ID in discovered:
        raise GateVerificationError(
            f"evidence claims the reserved physical GPU gate {GPU_GATE_CHECK_ID!r}; "
            "no headless runner may satisfy it"
        )
    missing = sorted(set(configured) - set(discovered))
    injected = sorted(set(discovered) - set(configured))
    if missing or injected:
        raise GateVerificationError(
            f"production check set mismatch: missing={missing}, injected={injected}"
        )
    verified: dict[str, str] = {}
    for name in sorted(configured):
        check = configured[name]
        try:
            verified[check.check_id] = _verify_one_check(
                check, discovered[name], identity
            )
        except RECEIPTS.ReceiptValidationError as error:
            raise GateVerificationError(
                f"check {check.check_id!r} receipt was rejected: {error}"
            ) from error
    if set(verified) != required_ids:
        raise GateVerificationError("verified check identifiers are incomplete")
    return verified


def verify_production_gate(
    *,
    ci_results: str,
    production_results: str,
    evidence_root: Path,
    identity: GateIdentity,
    plan: Any,
) -> dict[str, str]:
    """Run every independent class of check and fail closed on the first gap."""

    try:
        ci_expected = CI_PLAN.expected_ci_results(plan)
    except (AttributeError, ValueError) as error:
        raise GateVerificationError(f"effective plan is invalid: {error}") from error
    if set(ci_expected) != REQUIRED_CI_JOBS:
        raise GateVerificationError("lane policy does not cover the CI job set")
    verify_job_results(ci_results, ci_expected, "ordinary CI")
    verify_production_results(production_results, plan)
    return verify_check_set(evidence_root, identity, plan)


def load_plan(path: Path) -> Any:
    """Read the effective plan file strictly."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GateVerificationError(f"effective plan is unreadable: {path}") from error
    try:
        return CI_PLAN.Plan.from_json(text)
    except ValueError as error:
        raise GateVerificationError(f"effective plan is malformed: {error}") from error


def _identity(arguments: argparse.Namespace) -> GateIdentity:
    return GateIdentity(
        commit=arguments.commit,
        tree=arguments.tree,
        repository=arguments.repository,
        run_id=arguments.run_id,
        attempt=arguments.attempt,
        environment=arguments.environment,
        provenance=arguments.provenance,
        event=arguments.event,
        pull_request_head=arguments.pull_request_head,
        pull_request_base=arguments.pull_request_base,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--provenance", required=True, choices=("hosted", "local", "controlled-fixture")
    )
    parser.add_argument("--event", required=True)
    parser.add_argument("--pull-request-head", required=True)
    parser.add_argument("--pull-request-base", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the complete production gate, printing one honest summary line."""

    arguments = _parser().parse_args(argv)
    try:
        plan = load_plan(arguments.plan)
        verified = verify_production_gate(
            ci_results=os.environ.get("DESKTOP_GATE_RESULTS", ""),
            production_results=os.environ.get("DESKTOP_PRODUCTION_RESULTS", ""),
            evidence_root=arguments.evidence_root,
            identity=_identity(arguments),
            plan=plan,
        )
    except (
        GateVerificationError,
        RECEIPTS.ReceiptValidationError,
        REPORTS.ReportValidationError,
        OSError,
    ) as error:
        print(f"Desktop production gate failed: {error}", file=sys.stderr)
        return 1
    graph = "full graph" if plan.full else "reduced graph"
    print(
        f"Gate verified the {len(verified)} checks the effective plan ({graph}) "
        "requires; the physical GPU gate "
        f"{GPU_GATE_CHECK_ID!r} remains unsatisfied and is owned by issue #55"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
